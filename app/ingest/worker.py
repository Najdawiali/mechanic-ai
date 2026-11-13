"""The background worker: takes jobs from the queue and turns files into draft records.

Run it beside the API:      python -m app.ingest.worker
Process what is waiting:    python -m app.ingest.worker --once

Several workers can run at once. A job that fails for a reason that may pass (a model timeout,
storage briefly unreachable) is tried again later; one that cannot pass is marked failed at once.
Everything a job produces is stored as a draft: nothing reaches a mechanic until it is approved.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import signal
import socket

import psycopg

from app.db import connect
from app.ingest import jobs
from app.ingest.extractors import EXTRACTORS, Classifier, Extractor, SourceDocument
from app.ingest.jobs import PermanentError
from app.kb.index import record_key, record_type
from app.kb.store import KnowledgeStore
from app.storage import ObjectNotFound, S3Store, get_store

log = logging.getLogger("ingest.worker")


class Worker:
    def __init__(
        self,
        database_url: str | None = None,
        storage: S3Store | None = None,
        *,
        extractors: dict[str, Extractor] | None = None,
        classify: Classifier | None = None,
        worker_id: str | None = None,
        stale_after: dt.timedelta = dt.timedelta(minutes=30),
    ):
        self.database_url = database_url
        self.storage = storage or get_store()
        self.extractors = EXTRACTORS if extractors is None else extractors
        self.classify = classify
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.stale_after = stale_after
        self._stop = False

    def _connect(self) -> psycopg.Connection:
        conn = connect(self.database_url)
        conn.autocommit = True
        return conn

    def run_once(self) -> bool:
        """Process one job. Returns False when nothing was waiting."""
        with self._connect() as conn:
            jobs.requeue_stale(conn, self.stale_after)
            job = jobs.claim(conn, self.worker_id)
            if job is None:
                return False
            try:
                self._process(conn, job)
            except PermanentError as exc:
                jobs.fail(conn, job.id, str(exc), retryable=False)
                log.warning("job %s failed: %s", job.id, exc)
            except Exception as exc:  # noqa: BLE001  a job must never take the worker down
                state = jobs.fail(conn, job.id, f"{type(exc).__name__}: {exc}", retryable=True)
                log.warning("job %s attempt %s failed (%s): %s", job.id, job.attempts, state, exc)
            else:
                log.info("job %s succeeded", job.id)
            return True

    def _process(self, conn: psycopg.Connection, job: jobs.Job) -> None:
        file = conn.execute("SELECT * FROM kb.source_file WHERE id = %s", (job.source_file_id,)).fetchone()
        if file["withdrawn_at"] is not None:
            raise PermanentError("the file was withdrawn")
        try:
            data = self.storage.get(file["storage_key"])
        except ObjectNotFound:
            raise PermanentError(f"the file is no longer in storage at {file['storage_key']!r}") from None
        document = SourceDocument(file["id"], file["storage_key"], file["original_name"], file["kind"], file["declared"], data)

        kind = file["kind"] or (self.classify(document) if self.classify else None)
        if kind is None:
            raise PermanentError("could not tell what kind of file this is; register it with its kind")
        extractor = self.extractors.get(kind)
        if extractor is None:
            raise PermanentError(f"no extractor for {kind} files yet")
        extracted = extractor(document)

        # The records and the job's success are stored together, or not at all.
        with conn.transaction():
            if file["kind"] is None:
                conn.execute("UPDATE kb.source_file SET kind = %s, updated_at = now() WHERE id = %s", (kind, file["id"]))
            store, saved = KnowledgeStore(conn), []
            for item in extracted:
                done = store.save(item.record, extracted_by=item.extracted_by, source_file_id=file["id"])
                saved.append({"record_id": done.record_id, "type": record_type(item.record), "key": record_key(item.record),
                              "version": done.version, "action": done.action})
            jobs.succeed(conn, job.id, {"records": saved}, file_version=file["version"])

    def run_forever(self, poll_seconds: float = 10.0) -> None:
        """Work until stopped. Wakes at once when a job is queued, and polls as a fallback."""
        with self._connect() as listener:
            listener.execute(f"LISTEN {jobs.CHANNEL}")
            log.info("worker %s started", self.worker_id)
            while not self._stop:
                while not self._stop and self.run_once():
                    pass
                for _ in listener.notifies(timeout=poll_seconds, stop_after=1):
                    pass
        log.info("worker %s stopped", self.worker_id)

    def stop(self, *_) -> None:
        self._stop = True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="process the jobs that are waiting, then exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    worker = Worker()
    if args.once:
        count = 0
        while worker.run_once():
            count += 1
        print(f"processed {count} job(s)")
        return 0
    signal.signal(signal.SIGINT, worker.stop)
    signal.signal(signal.SIGTERM, worker.stop)
    worker.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

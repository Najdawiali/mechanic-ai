"""Registering source files and the background job queue (in PostgreSQL).

A file is registered by its storage key. Registering creates a job; a worker picks the job up
and turns the file into draft records. The queue is a table, so every file's state is visible
and a failed file stays on record until it is fixed.

All functions take a connection in autocommit mode and run their own transactions.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from typing import Literal

import psycopg
from psycopg.types.json import Jsonb

from app.config import get_settings
from app.storage import S3Store

CHANNEL = "kb_ingest_job"  # workers LISTEN here, so a new job starts at once instead of at the next poll
FileKind = Literal["licensed_procedure", "licensed_diagram", "client_case_file", "expert_transcript"]


class PermanentError(Exception):
    """The job cannot succeed by trying again (wrong file, no extractor, failed checks)."""


@dataclass(frozen=True)
class Registered:
    file_id: int
    job_id: int | None  # the job that will process, or last processed, the file
    action: Literal["created", "unchanged", "requeued", "new_version", "restored"]


@dataclass(frozen=True)
class Job:
    id: int
    source_file_id: int
    attempts: int
    max_attempts: int


def register_file(
    conn: psycopg.Connection,
    storage: S3Store,
    storage_key: str,
    *,
    original_name: str | None = None,
    kind: FileKind | None = None,
    declared: dict | None = None,
    again: bool = False,
) -> Registered:
    """Record a file that is already in storage and queue it for processing.

    The file is fingerprinted. The same file registered again changes nothing, unless its last
    job failed or `again` is set (an extractor was added or improved): then it is queued again.
    Changed content becomes a new version and is processed again; a withdrawn file is restored.
    """
    info = storage.head(storage_key)
    if info is None:
        raise FileNotFoundError(f"no object in storage at {storage_key!r}")
    limit_mb = get_settings().ingest_max_file_mb
    if info.size > limit_mb * 1024 * 1024:
        raise ValueError(f"{storage_key!r} is {info.size / 1e6:.1f} MB; the limit is {limit_mb} MB")
    digest = hashlib.sha256(storage.get(storage_key)).hexdigest()
    name = original_name or storage_key.rsplit("/", 1)[-1]

    with conn.transaction():
        row = conn.execute("SELECT * FROM kb.source_file WHERE storage_key = %s FOR UPDATE", (storage_key,)).fetchone()
        if row is None:
            file_id = conn.execute(
                "INSERT INTO kb.source_file (storage_key, original_name, kind, sha256, size_bytes, content_type, declared) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (storage_key, name, kind, digest, info.size, info.content_type, Jsonb(declared or {})),
            ).fetchone()["id"]
            return Registered(file_id, _enqueue(conn, file_id), "created")

        file_id = row["id"]
        same_content, withdrawn = row["sha256"] == digest, row["withdrawn_at"] is not None
        if same_content and not withdrawn:
            last = conn.execute(
                "SELECT id, status FROM kb.ingest_job WHERE source_file_id = %s ORDER BY id DESC LIMIT 1", (file_id,)
            ).fetchone()
            if last and last["status"] in ("queued", "running"):
                return Registered(file_id, last["id"], "unchanged")
            if again or last is None or last["status"] in ("failed", "cancelled"):
                if kind and kind != row["kind"]:
                    conn.execute("UPDATE kb.source_file SET kind = %s, updated_at = now() WHERE id = %s", (kind, file_id))
                return Registered(file_id, _enqueue(conn, file_id), "requeued")
            return Registered(file_id, last["id"], "unchanged")

        conn.execute(
            """
            UPDATE kb.source_file
            SET sha256 = %s, size_bytes = %s, content_type = %s, version = version + %s, withdrawn_at = NULL,
                original_name = %s, kind = COALESCE(%s, kind),
                declared = CASE WHEN %s::jsonb = '{}'::jsonb THEN declared ELSE %s::jsonb END, updated_at = now()
            WHERE id = %s
            """,
            (digest, info.size, info.content_type, 0 if same_content else 1, name, kind,
             Jsonb(declared or {}), Jsonb(declared or {}), file_id),
        )
        if withdrawn:
            conn.execute("UPDATE kb.record SET withdrawn_at = NULL WHERE source_file_id = %s", (file_id,))
        return Registered(file_id, _enqueue(conn, file_id), "restored" if same_content else "new_version")


def _enqueue(conn: psycopg.Connection, file_id: int) -> int:
    """Queue a job for the file, or return the one already waiting."""
    row = conn.execute(
        "INSERT INTO kb.ingest_job (source_file_id) VALUES (%s) "
        "ON CONFLICT (source_file_id) WHERE status = 'queued' DO NOTHING RETURNING id",
        (file_id,),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT id FROM kb.ingest_job WHERE source_file_id = %s AND status = 'queued'", (file_id,)
        ).fetchone()
    conn.execute("SELECT pg_notify(%s, %s)", (CHANNEL, str(row["id"])))
    return row["id"]


def withdraw_file(conn: psycopg.Connection, file_id: int) -> int:
    """Stop serving everything extracted from a file. Returns how many records were withdrawn."""
    with conn.transaction():
        done = conn.execute(
            "UPDATE kb.source_file SET withdrawn_at = now(), updated_at = now() WHERE id = %s AND withdrawn_at IS NULL",
            (file_id,),
        )
        if done.rowcount == 0 and conn.execute("SELECT 1 FROM kb.source_file WHERE id = %s", (file_id,)).fetchone() is None:
            raise LookupError(f"no source file with id {file_id}")
        conn.execute(
            "UPDATE kb.ingest_job SET status = 'cancelled', finished_at = now(), error = 'the file was withdrawn' "
            "WHERE source_file_id = %s AND status = 'queued'",
            (file_id,),
        )
        return conn.execute(
            "UPDATE kb.record SET withdrawn_at = now() WHERE source_file_id = %s AND withdrawn_at IS NULL", (file_id,)
        ).rowcount


def claim(conn: psycopg.Connection, worker_id: str) -> Job | None:
    """Take the next job that is due, or None. Two workers never get the same job, and a file
    that is being processed is not started a second time."""
    row = conn.execute(
        """
        UPDATE kb.ingest_job j
        SET status = 'running', attempts = attempts + 1, locked_by = %s, locked_at = now(),
            started_at = COALESCE(started_at, now()), error = NULL
        WHERE j.id = (
            SELECT q.id FROM kb.ingest_job q
            WHERE q.status = 'queued' AND q.run_after <= now()
              AND NOT EXISTS (SELECT 1 FROM kb.ingest_job r
                              WHERE r.source_file_id = q.source_file_id AND r.status = 'running')
            ORDER BY q.run_after, q.id
            FOR UPDATE SKIP LOCKED
            LIMIT 1)
        RETURNING j.id, j.source_file_id, j.attempts, j.max_attempts
        """,
        (worker_id,),
    ).fetchone()
    return Job(**row) if row else None


def succeed(conn: psycopg.Connection, job_id: int, result: dict, *, file_version: int | None = None) -> None:
    conn.execute(
        "UPDATE kb.ingest_job SET status = 'succeeded', result = %s, file_version = %s, finished_at = now(), "
        "locked_by = NULL, locked_at = NULL WHERE id = %s",
        (Jsonb(result), file_version, job_id),
    )


def retry_delay(attempts: int) -> dt.timedelta:
    """30 seconds after the first failure, then 2 minutes, then 8."""
    return dt.timedelta(seconds=30 * 4 ** (attempts - 1))


def fail(conn: psycopg.Connection, job_id: int, error: str, *, retryable: bool) -> Literal["queued", "failed"]:
    """Record a failure. A retryable failure goes back in the queue until the attempts run out."""
    with conn.transaction():
        job = conn.execute(
            "SELECT attempts, max_attempts FROM kb.ingest_job WHERE id = %s FOR UPDATE", (job_id,)
        ).fetchone()
        again = retryable and job["attempts"] < job["max_attempts"]
        if again:
            conn.execute(
                "UPDATE kb.ingest_job SET status = 'queued', run_after = now() + %s, error = %s, "
                "locked_by = NULL, locked_at = NULL WHERE id = %s",
                (retry_delay(job["attempts"]), error[:4000], job_id),
            )
        else:
            conn.execute(
                "UPDATE kb.ingest_job SET status = 'failed', error = %s, finished_at = now(), "
                "locked_by = NULL, locked_at = NULL WHERE id = %s",
                (error[:4000], job_id),
            )
    return "queued" if again else "failed"


def requeue_stale(conn: psycopg.Connection, older_than: dt.timedelta) -> int:
    """Jobs whose worker stopped without finishing go back in the queue (or fail when their
    attempts are used up). Returns how many jobs were touched."""
    with conn.transaction():
        stale = conn.execute(
            "SELECT id FROM kb.ingest_job WHERE status = 'running' AND locked_at < now() - %s FOR UPDATE SKIP LOCKED",
            (older_than,),
        ).fetchall()
        for row in stale:
            fail(conn, row["id"], "the worker stopped before finishing", retryable=True)
    return len(stale)


def job_status(conn: psycopg.Connection, job_id: int) -> dict:
    row = conn.execute(
        "SELECT j.id, j.status, j.attempts, j.max_attempts, j.error, j.result, j.created_at, j.finished_at, "
        "       f.id AS file_id, f.storage_key, f.kind, f.version AS file_version "
        "FROM kb.ingest_job j JOIN kb.source_file f ON f.id = j.source_file_id WHERE j.id = %s",
        (job_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"no job with id {job_id}")
    return row

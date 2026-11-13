"""Source files, the job queue and the worker. These tests need a database (see conftest.py)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from app.db import connect
from app.ingest import jobs
from app.ingest.add import storage_key_for
from app.ingest.extractors import Extracted, SourceDocument
from app.ingest.jobs import PermanentError
from app.ingest.worker import Worker
from app.kb.schemas import Case, Powertrain, Vehicle
from app.kb.store import KnowledgeStore
from app.storage import ObjectNotFound

REPO = Path(__file__).resolve().parents[1]
KEY = "kb/client_case_file/byd/case.txt"
BYD = Vehicle(make="BYD", model="Qin Plus DM-i", year=2024, powertrain=Powertrain.HYBRID)


def byd_case() -> Case:
    return Case(**json.loads((REPO / "seed/cases/byd-qin-plus-dmi-2024-p2b5313-p2b5411.json").read_text(encoding="utf-8")))


def job_row(conn, job_id: int) -> dict:
    return conn.execute("SELECT * FROM kb.ingest_job WHERE id = %s", (job_id,)).fetchone()


def case_extractor(document: SourceDocument) -> list[Extracted]:
    """Stands in for the real case extractor: the file's first line becomes the diagnosis."""
    first_line = document.data.decode("utf-8").splitlines()[0]
    return [Extracted(byd_case().model_copy(update={"diagnosis": first_line}), "model")]


@pytest.fixture
def worker(database_url, storage) -> Worker:
    return Worker(database_url, storage, extractors={"client_case_file": case_extractor}, worker_id="test-worker")


# -- storage ------------------------------------------------------------------------
def test_storage_writes_reads_links_and_deletes(storage):
    storage.put("a/b.txt", b"hello", content_type="text/plain")
    assert storage.get("a/b.txt") == b"hello"
    assert storage.head("a/b.txt").size == 5 and storage.head("a/b.txt").content_type == "text/plain"
    assert "a/b.txt" in storage.presigned_url("a/b.txt", expires_s=60)
    storage.delete("a/b.txt")
    assert storage.head("a/b.txt") is None
    with pytest.raises(ObjectNotFound):
        storage.get("a/b.txt")


def test_the_same_file_name_under_another_car_gets_another_key(tmp_path):
    one = tmp_path / "2025" / "acura" / "Integra" / "1.5 turbo" / "diagrams" / "ABS.pdf"
    two = tmp_path / "2025" / "honda" / "Civic" / "1.5 turbo" / "diagrams" / "ABS.pdf"
    assert storage_key_for("licensed_diagram", one) != storage_key_for("licensed_diagram", two)
    assert storage_key_for("licensed_diagram", one) == storage_key_for("licensed_diagram", one)
    assert storage_key_for("licensed_diagram", one).endswith("/2025/ACURA/INTEGRA/15TURBO/DIAGRAMS/ABS.pdf")


# -- registering a file ---------------------------------------------------------------
def test_registering_a_file_queues_one_job(auto_conn, storage):
    storage.put(KEY, b"first version")
    done = jobs.register_file(auto_conn, storage, KEY, kind="client_case_file", declared={"make": "BYD"})
    assert done.action == "created" and job_row(auto_conn, done.job_id)["status"] == "queued"
    file = auto_conn.execute("SELECT * FROM kb.source_file WHERE id = %s", (done.file_id,)).fetchone()
    assert (file["original_name"], file["version"], file["size_bytes"], file["declared"]) == ("case.txt", 1, 13, {"make": "BYD"})


def test_registering_the_same_file_again_changes_nothing(auto_conn, storage):
    storage.put(KEY, b"first version")
    first = jobs.register_file(auto_conn, storage, KEY, kind="client_case_file")
    again = jobs.register_file(auto_conn, storage, KEY, kind="client_case_file")
    assert again.action == "unchanged" and again.job_id == first.job_id
    assert auto_conn.execute("SELECT count(*) AS n FROM kb.ingest_job").fetchone()["n"] == 1


def test_a_file_whose_job_failed_is_queued_again_when_registered_again(auto_conn, storage, database_url, worker):
    storage.put("kb/diagram.pdf", b"%PDF")
    first = jobs.register_file(auto_conn, storage, "kb/diagram.pdf", kind="licensed_diagram")
    worker.run_once()  # fails: this worker has no diagram extractor
    again = jobs.register_file(auto_conn, storage, "kb/diagram.pdf")
    assert again.action == "requeued" and again.job_id != first.job_id


def test_an_unchanged_file_can_be_processed_again_on_request(auto_conn, storage, worker):
    storage.put(KEY, b"first version")
    first = jobs.register_file(auto_conn, storage, KEY, kind="client_case_file")
    worker.run_once()
    assert jobs.register_file(auto_conn, storage, KEY).action == "unchanged"
    forced = jobs.register_file(auto_conn, storage, KEY, again=True)
    assert forced.action == "requeued" and forced.job_id != first.job_id
    worker.run_once()
    assert jobs.job_status(auto_conn, forced.job_id)["result"]["records"][0]["action"] == "unchanged"


def test_changed_content_is_a_new_version(auto_conn, storage, worker):
    storage.put(KEY, b"first version")
    first = jobs.register_file(auto_conn, storage, KEY, kind="client_case_file")
    assert worker.run_once()
    storage.put(KEY, b"second version")
    second = jobs.register_file(auto_conn, storage, KEY)
    assert second.action == "new_version" and second.job_id != first.job_id
    file = auto_conn.execute("SELECT version, kind FROM kb.source_file WHERE id = %s", (first.file_id,)).fetchone()
    assert file == {"version": 2, "kind": "client_case_file"}  # the kind given the first time is kept


def test_a_file_that_is_not_in_storage_cannot_be_registered(auto_conn, storage):
    with pytest.raises(FileNotFoundError):
        jobs.register_file(auto_conn, storage, "kb/missing.pdf")


def test_workers_are_told_at_once_when_a_job_is_queued(auto_conn, storage, database_url):
    storage.put(KEY, b"first version")
    with connect(database_url) as listener:
        listener.autocommit = True
        listener.execute(f"LISTEN {jobs.CHANNEL}")
        done = jobs.register_file(auto_conn, storage, KEY, kind="client_case_file")
        heard = [n.payload for n in listener.notifies(timeout=5, stop_after=1)]
    assert heard == [str(done.job_id)]


# -- the queue --------------------------------------------------------------------------
@pytest.fixture
def queued(auto_conn, storage) -> jobs.Registered:
    storage.put(KEY, b"first version")
    return jobs.register_file(auto_conn, storage, KEY, kind="client_case_file")


def test_a_job_is_given_to_one_worker_only(auto_conn, queued, database_url):
    with connect(database_url) as other:
        other.autocommit = True
        first, second = jobs.claim(auto_conn, "worker-a"), jobs.claim(other, "worker-b")
    assert first.id == queued.job_id and first.attempts == 1 and second is None
    assert job_row(auto_conn, queued.job_id)["locked_by"] == "worker-a"


def test_a_file_being_processed_is_not_started_twice(auto_conn, queued, storage):
    assert jobs.claim(auto_conn, "worker-a") is not None
    storage.put(KEY, b"second version")  # the file changes while the first job is running
    jobs.register_file(auto_conn, storage, KEY)
    assert jobs.claim(auto_conn, "worker-b") is None  # the new job waits for the running one
    jobs.succeed(auto_conn, queued.job_id, {"records": []})
    assert jobs.claim(auto_conn, "worker-b") is not None


def test_a_failure_that_may_pass_is_tried_again_later(auto_conn, queued):
    job = jobs.claim(auto_conn, "worker-a")
    assert jobs.fail(auto_conn, job.id, "TimeoutError: model timed out", retryable=True) == "queued"
    row = job_row(auto_conn, job.id)
    assert row["status"] == "queued" and row["error"].startswith("TimeoutError") and row["run_after"] > row["created_at"]
    assert jobs.claim(auto_conn, "worker-a") is None  # not due yet


def test_a_job_fails_for_good_when_its_attempts_run_out(auto_conn, queued):
    for attempt in (1, 2, 3):
        auto_conn.execute("UPDATE kb.ingest_job SET run_after = now() WHERE id = %s", (queued.job_id,))
        job = jobs.claim(auto_conn, "worker-a")
        assert job.attempts == attempt
        state = jobs.fail(auto_conn, job.id, "still failing", retryable=True)
    assert state == "failed" and job_row(auto_conn, queued.job_id)["status"] == "failed"


def test_a_failure_that_cannot_pass_is_not_retried(auto_conn, queued):
    job = jobs.claim(auto_conn, "worker-a")
    assert jobs.fail(auto_conn, job.id, "no extractor", retryable=False) == "failed"


def test_a_job_left_by_a_stopped_worker_goes_back_in_the_queue(auto_conn, queued):
    jobs.claim(auto_conn, "worker-that-died")
    assert jobs.requeue_stale(auto_conn, dt.timedelta(minutes=30)) == 0  # still fresh
    auto_conn.execute("UPDATE kb.ingest_job SET locked_at = now() - interval '2 hours'")
    assert jobs.requeue_stale(auto_conn, dt.timedelta(minutes=30)) == 1
    row = job_row(auto_conn, queued.job_id)
    assert row["status"] == "queued" and "stopped" in row["error"]


def test_retries_wait_longer_each_time():
    assert [jobs.retry_delay(n).total_seconds() for n in (1, 2, 3)] == [30, 120, 480]


# -- the worker ---------------------------------------------------------------------------
def test_the_worker_turns_a_file_into_draft_records(auto_conn, queued, worker):
    assert worker.run_once() is True and worker.run_once() is False
    status = jobs.job_status(auto_conn, queued.job_id)
    assert status["status"] == "succeeded" and status["file_version"] == 1
    saved = status["result"]["records"]
    assert [(r["type"], r["action"]) for r in saved] == [("case", "created")]

    store = KnowledgeStore(auto_conn)
    row = auto_conn.execute("SELECT status, source_file_id, extracted_by FROM kb.record WHERE id = %s", (saved[0]["record_id"],)).fetchone()
    assert row == {"status": "draft", "source_file_id": queued.file_id, "extracted_by": "model"}
    assert store.get(saved[0]["record_id"]).diagnosis == "first version"
    assert store.find_by_codes(BYD, ["P2B5313"]) == []  # a draft is not served


def test_reading_a_file_again_does_not_leave_the_old_draft_behind(auto_conn, queued, storage, database_url):
    # Real run: the model named the same report differently on two runs and two drafts were left.
    def named(case_id):
        return lambda document: [Extracted(byd_case().model_copy(update={"case_id": case_id}), "model")]

    Worker(database_url, storage, extractors={"client_case_file": named("case:first-name")}, worker_id="w").run_once()
    again = jobs.register_file(auto_conn, storage, KEY, again=True)
    Worker(database_url, storage, extractors={"client_case_file": named("case:second-name")}, worker_id="w").run_once()
    keys = [r["record_key"] for r in auto_conn.execute("SELECT record_key FROM kb.record").fetchall()]
    assert keys == ["case:second-name"] and jobs.job_status(auto_conn, again.job_id)["result"]["stale_drafts_removed"] == 1


def test_a_file_with_no_extractor_fails_with_a_clear_message(auto_conn, storage, worker):
    storage.put("kb/diagram.pdf", b"%PDF")
    done = jobs.register_file(auto_conn, storage, "kb/diagram.pdf", kind="licensed_diagram")
    worker.run_once()
    row = job_row(auto_conn, done.job_id)
    assert (row["status"], row["attempts"], row["error"]) == ("failed", 1, "no extractor for licensed_diagram files yet")


def test_a_file_of_unknown_kind_fails_unless_it_can_be_classified(auto_conn, storage, database_url):
    storage.put(KEY, b"first version")
    done = jobs.register_file(auto_conn, storage, KEY)
    Worker(database_url, storage, extractors={}, worker_id="w").run_once()
    assert "could not tell what kind" in job_row(auto_conn, done.job_id)["error"]

    storage.put(KEY, b"second version")
    again = jobs.register_file(auto_conn, storage, KEY)
    Worker(database_url, storage, extractors={"client_case_file": case_extractor},
           classify=lambda document: "client_case_file", worker_id="w").run_once()
    assert job_row(auto_conn, again.job_id)["status"] == "succeeded"
    assert auto_conn.execute("SELECT kind FROM kb.source_file WHERE id = %s", (done.file_id,)).fetchone()["kind"] == "client_case_file"


def test_an_extractor_error_is_recorded_and_stores_nothing(auto_conn, queued, database_url, storage):
    def broken(document):
        raise RuntimeError("the model returned text that is not a case")

    Worker(database_url, storage, extractors={"client_case_file": broken}, worker_id="w").run_once()
    row = job_row(auto_conn, queued.job_id)
    assert row["status"] == "queued" and row["error"] == "RuntimeError: the model returned text that is not a case"
    assert auto_conn.execute("SELECT count(*) AS n FROM kb.record").fetchone()["n"] == 0


def test_an_extractor_can_say_that_retrying_is_pointless(auto_conn, queued, database_url, storage):
    def refuses(document):
        raise PermanentError("the pin is outside the connector's pin count")

    Worker(database_url, storage, extractors={"client_case_file": refuses}, worker_id="w").run_once()
    assert job_row(auto_conn, queued.job_id)["status"] == "failed"


def test_a_file_deleted_from_storage_fails_its_job(auto_conn, queued, worker, storage):
    storage.delete(KEY)
    worker.run_once()
    row = job_row(auto_conn, queued.job_id)
    assert row["status"] == "failed" and "no longer in storage" in row["error"]


def test_a_changed_file_never_overwrites_an_approved_record(auto_conn, queued, worker, storage):
    worker.run_once()
    store = KnowledgeStore(auto_conn)
    v1 = jobs.job_status(auto_conn, queued.job_id)["result"]["records"][0]["record_id"]
    store.set_review(v1, "approved", reviewer="test reviewer")

    storage.put(KEY, b"second version")
    second = jobs.register_file(auto_conn, storage, KEY)
    worker.run_once()
    assert jobs.job_status(auto_conn, second.job_id)["result"]["records"][0]["action"] == "new_version"
    served = store.find_by_codes(BYD, ["P2B5313"])
    assert [h.record.diagnosis for h in served] == ["first version"]  # until the new draft is approved


# -- withdrawing a file ----------------------------------------------------------------------
def test_withdrawing_a_file_withdraws_its_records_and_registering_it_restores_them(auto_conn, queued, worker, storage):
    worker.run_once()
    store = KnowledgeStore(auto_conn)
    record_id = jobs.job_status(auto_conn, queued.job_id)["result"]["records"][0]["record_id"]
    store.set_review(record_id, "approved", reviewer="test reviewer")
    assert len(store.find_by_codes(BYD, ["P2B5313"])) == 1

    assert jobs.withdraw_file(auto_conn, queued.file_id) == 1
    assert store.find_by_codes(BYD, ["P2B5313"]) == []
    assert store.search_text(BYD, "codes return immediately at ignition") == []

    back = jobs.register_file(auto_conn, storage, KEY)
    assert back.action == "restored" and len(store.find_by_codes(BYD, ["P2B5313"])) == 1
    worker.run_once()
    assert jobs.job_status(auto_conn, back.job_id)["result"]["records"][0]["action"] == "unchanged"


def test_withdrawing_cancels_a_waiting_job(auto_conn, queued, worker):
    jobs.withdraw_file(auto_conn, queued.file_id)
    assert job_row(auto_conn, queued.job_id)["status"] == "cancelled" and worker.run_once() is False
    with pytest.raises(LookupError):
        jobs.withdraw_file(auto_conn, 999_999)

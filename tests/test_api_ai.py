"""The assistant's API through HTTP: real database, scripted model."""

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api import ai
from app.api.main import app
from app.config import get_settings
from app.kb.store import KnowledgeStore
from tests.test_engine import ACURA, BYD, Script, abs_diagram, byd_case, p0010

KEY = {"Authorization": "Bearer test-key"}


@pytest.fixture
def script():
    return Script(intakes={"Replaced the cooling pump, codes cleared and it works": {"intent": "fix_confirmed", "fix": "hybrid coolant pump replacement", "result_words": "codes cleared and it works"}},
                  advise=[{"use": ["case:byd-qin-plus-dmi-2024:p2b5313-p2b5411"],
                           "message": "An earlier repair on a 2024 BYD Qin Plus DM-i with P2B5313: check the hybrid coolant pump first. Did you?"}])


@pytest.fixture
def client(conn, monkeypatch, script):
    conn.execute("TRUNCATE ai.session, ai.gap RESTART IDENTITY CASCADE")
    kb = KnowledgeStore(conn)
    for record in (p0010(), abs_diagram(), byd_case()):
        saved = kb.save(record, extracted_by="hand")
        kb.set_review(saved.record_id, "approved", reviewer="test")
    conn.commit()
    monkeypatch.setattr(get_settings(), "ai_service_key", SecretStr("test-key"))

    def one_transaction():
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    app.dependency_overrides[ai.get_conn] = one_transaction
    app.dependency_overrides[ai.get_model] = lambda: script.model
    yield TestClient(app)
    app.dependency_overrides.clear()


def open_session(client, session_id="s1", vehicle=ACURA, language="en"):
    return client.post("/ai/sessions", json={"session_id": session_id, "vehicle": vehicle, "language": language}, headers=KEY)


def test_every_call_needs_the_service_key_and_none_works_until_it_is_set(client, monkeypatch):
    assert client.post("/ai/sessions", json={"session_id": "s", "vehicle": ACURA}).status_code == 401
    assert client.post("/ai/sessions", json={"session_id": "s", "vehicle": ACURA}, headers={"Authorization": "Bearer nope"}).status_code == 401
    monkeypatch.setattr(get_settings(), "ai_service_key", None)
    assert open_session(client).status_code == 503


def test_a_session_walks_the_procedure_and_a_retried_message_gets_the_same_reply(client, script):
    assert open_session(client).status_code == 201
    assert open_session(client).status_code == 200  # a retried create
    assert client.post("/ai/sessions", json={"session_id": "s1", "vehicle": BYD}, headers=KEY).status_code == 409

    first = client.post("/ai/sessions/s1/turns", json={"message_id": "m1", "text": "fault code P0010"}, headers=KEY).json()
    assert first["type"] == "step" and first["step"]["step_id"] == "1" and first["tier"] == "verified"
    calls = len(script.model.trace)
    again = client.post("/ai/sessions/s1/turns", json={"message_id": "m1", "text": "fault code P0010"}, headers=KEY).json()
    assert again == first and len(script.model.trace) == calls  # stored reply, no new work
    assert client.post("/ai/sessions/s1/turns", json={"message_id": "m1", "text": "something else"}, headers=KEY).status_code == 409

    second = client.post("/ai/sessions/s1/turns", json={"message_id": "m2", "text": "7.5 ohm"}, headers=KEY).json()
    assert second["step"]["step_id"] == "2"


def test_close_gives_a_summary_and_a_draft_case_only_after_a_confirmed_fix_then_refuses_turns(client):
    open_session(client, "s2", BYD, "en")
    client.post("/ai/sessions/s2/turns", json={"message_id": "m1", "text": "BYD with P2B5313 and P2B5411"}, headers=KEY)
    client.post("/ai/sessions/s2/turns", json={"message_id": "m2", "text": "Replaced the cooling pump, codes cleared and it works"}, headers=KEY)
    summary = client.post("/ai/sessions/s2/close", headers=KEY).json()
    assert summary["record"]["codes"] == ["P2B5313", "P2B5411"] and summary["record"]["confirmed_fix"] == "hybrid coolant pump replacement"
    assert summary["pending_case"]["repair_steps"] == ["hybrid coolant pump replacement"] and summary["summary"]
    assert "case:byd-qin-plus-dmi-2024:p2b5313-p2b5411" in summary["record"]["evidence_ids"]
    assert client.post("/ai/sessions/s2/close", headers=KEY).json() == summary  # closing twice
    assert client.post("/ai/sessions/s2/turns", json={"message_id": "m3", "text": "one more"}, headers=KEY).status_code == 409

    # the admin approves the draft case: it is stored as approved and served by code
    case = summary["pending_case"] | {"case_id": "case:byd-qin-plus-dmi-2024:pump-session"}
    stored = client.post("/ai/cases", json={"case": case, "reviewer": "workshop admin"}, headers=KEY)
    assert stored.status_code == 201 and stored.json()["status"] == "approved"


def test_delete_removes_the_session_and_keeps_only_anonymous_gaps(client, conn):
    open_session(client, "s3", {"make": "Toyota", "model": "Camry", "year": 2018})
    client.post("/ai/sessions/s3/turns", json={"message_id": "m1", "text": "Camry 2018 with P0420, what do I check?"}, headers=KEY)
    assert conn.execute("SELECT count(*) AS n FROM ai.gap WHERE question IS NOT NULL").fetchone()["n"] == 1
    assert client.delete("/ai/sessions/s3", headers=KEY).status_code == 204
    assert client.post("/ai/sessions/s3/turns", json={"message_id": "m2", "text": "hello"}, headers=KEY).status_code == 404
    gap = conn.execute("SELECT session_id, question, codes FROM ai.gap").fetchone()
    assert gap == {"session_id": None, "question": None, "codes": ["P0420"]}
    assert conn.execute("SELECT count(*) AS n FROM ai.trace").fetchone()["n"] == 0


def test_an_unknown_session_and_an_empty_message_are_refused(client):
    assert client.post("/ai/sessions/none/turns", json={"message_id": "m", "text": "hi"}, headers=KEY).status_code == 404
    open_session(client, "s4")
    assert client.post("/ai/sessions/s4/turns", json={"message_id": "m", "text": " "}, headers=KEY).status_code == 422


def test_a_file_in_the_shared_storage_is_queued_once_and_a_missing_one_is_refused(client, storage):
    app.dependency_overrides[ai.get_storage] = lambda: storage
    storage.put("uploads/acura/integra/2025/abs.pdf", b"%PDF-1.7 test", content_type="application/pdf")
    body = {"storage_key": "uploads/acura/integra/2025/abs.pdf", "kind": "licensed_diagram",
            "vehicle": {"make": "Acura", "model": "Integra", "year": 2025}}
    first = client.post("/ai/ingest", json=body, headers=KEY)
    assert first.status_code == 202 and first.json()["action"] == "created" and first.json()["job_id"]
    assert client.post("/ai/ingest", json=body, headers=KEY).json()["action"] == "unchanged"
    assert client.post("/ai/ingest", json=body | {"storage_key": "uploads/none.pdf"}, headers=KEY).status_code == 404

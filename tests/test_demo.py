"""TEMPORARY demo page (app/demo): delete this file with it."""

import pytest

from tests.test_api_ai import client, script  # noqa: F401  the same app, database and scripted model

AUTH = ("anyone", "demo-pass")


@pytest.fixture
def demo(client, monkeypatch):  # noqa: F811
    monkeypatch.setenv("DEMO_PASSWORD", "demo-pass")
    return client


def test_the_demo_does_not_exist_until_its_password_is_set(client, monkeypatch):  # noqa: F811
    monkeypatch.delenv("DEMO_PASSWORD", raising=False)
    assert client.get("/demo", auth=AUTH).status_code == 404
    assert client.post("/demo/api/sessions", json={}, auth=AUTH).status_code == 404


def test_the_page_needs_the_password(demo):
    assert demo.get("/demo").status_code == 401
    assert demo.get("/demo", auth=("anyone", "wrong")).status_code == 401
    page = demo.get("/demo", auth=AUTH)
    assert page.status_code == 200 and "Mechanic AI · demo" in page.text


def test_the_page_talks_to_the_ai_api_with_the_key_kept_on_the_server(demo):
    vehicle = {"make": "Acura", "model": "Integra", "year": 2025, "engine_code": "L15CA", "powertrain": "ICE"}
    assert demo.post("/demo/api/sessions", json={"session_id": "d1", "vehicle": vehicle, "language": "en"}, auth=AUTH).status_code == 201
    step = demo.post("/demo/api/sessions/d1/turns", json={"message_id": "m1", "text": "fault code P0010"}, auth=AUTH).json()
    assert step["type"] == "step" and step["step"]["step_id"] == "1"
    done = demo.post("/demo/api/sessions/d1/turns", json={"message_id": "m2", "text": "15 ohm"}, auth=AUTH).json()
    assert done["type"] == "conclusion"
    assert demo.get("/demo/api/sessions/d1/trace", auth=AUTH).status_code == 200
    assert demo.post("/demo/api/sessions/d1/close", auth=AUTH).json()["record"]["codes"] == ["P0010"]


def test_only_a_sessions_calls_and_known_files_can_be_reached(demo):
    assert demo.post("/demo/api/cases", json={}, auth=AUTH).status_code == 404  # nothing that changes the knowledge base
    assert demo.post("/demo/api/ingest", json={}, auth=AUTH).status_code == 404
    assert demo.get("/demo/asset", params={"path": "../.env"}, auth=AUTH).status_code == 404
    assert demo.get("/demo/asset", params={"path": "eval/scenarios/assets/../../../.env"}, auth=AUTH).status_code == 404
    assert demo.get("/demo/diagram", params={"key": "secrets/x.png"}, auth=AUTH).status_code == 404


def test_the_evaluation_scenarios_are_offered_as_demo_conversations(demo):
    scenarios = demo.get("/demo/scenarios", auth=AUTH).json()
    assert len(scenarios) == 30 and all(s["turns"] and s["vehicle"]["make"] for s in scenarios)
    photo = next(t["attachments"][0] for s in scenarios for t in s["turns"] if t["attachments"])
    assert demo.get("/demo/asset", params={"path": photo}, auth=AUTH).status_code == 200

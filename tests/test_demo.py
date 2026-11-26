"""TEMPORARY demo page (app/demo): delete this file with it."""

import pytest

from app.demo import router as demo_router
from tests.test_api_ai import (  # noqa: F401  the same app, database and scripted model
    client,
    script,
)


@pytest.fixture
def demo(client, monkeypatch):  # noqa: F811
    monkeypatch.setenv("DEMO_ENABLED", "true")
    monkeypatch.setattr(demo_router, "LOCAL", demo_router.LOCAL | {"testclient"})  # the test client counts as this computer
    return client


def test_the_demo_does_not_exist_until_it_is_turned_on(client, monkeypatch):  # noqa: F811
    monkeypatch.setenv("DEMO_ENABLED", "false")
    monkeypatch.setattr(demo_router, "LOCAL", demo_router.LOCAL | {"testclient"})
    assert client.get("/demo").status_code == 404
    assert client.post("/demo/api/sessions", json={}).status_code == 404


def test_the_page_opens_without_sign_in_but_only_on_this_computer(client, monkeypatch):  # noqa: F811
    monkeypatch.setenv("DEMO_ENABLED", "true")
    assert client.get("/demo").status_code == 404  # another computer (the test client's address is not local)
    monkeypatch.setattr(demo_router, "LOCAL", demo_router.LOCAL | {"testclient"})
    page = client.get("/demo")
    assert page.status_code == 200 and "Mechanic AI · demo" in page.text
    assert "End conversation" in page.text


def test_the_page_talks_to_the_ai_api_with_the_key_kept_on_the_server(demo):
    vehicle = {"make": "Acura", "model": "Integra", "year": 2025, "engine_code": "L15CA", "powertrain": "ICE"}
    assert demo.post("/demo/api/sessions", json={"session_id": "d1", "vehicle": vehicle, "language": "en"}).status_code == 201
    step = demo.post("/demo/api/sessions/d1/turns", json={"message_id": "m1", "text": "fault code P0010"}).json()
    assert step["type"] == "step" and step["step"]["step_id"] == "1"
    done = demo.post("/demo/api/sessions/d1/turns", json={"message_id": "m2", "text": "15 ohm"}).json()
    assert done["type"] == "conclusion"
    assert demo.get("/demo/api/sessions/d1/trace").status_code == 200
    assert demo.post("/demo/api/sessions/d1/close").json()["record"]["codes"] == ["P0010"]


def test_the_page_streams_a_turn_in_this_service(demo):
    from tests.test_api_ai import streamed

    demo.post("/demo/api/sessions", json={"session_id": "d2", "vehicle": {"make": "Toyota", "model": "Camry", "year": 2018},
                                          "language": "en"})
    events = streamed(demo, "/demo/api/sessions/d2/turns/stream", {"message_id": "m1", "text": "Camry 2018 with P0420"}, headers={})
    assert events[-1][0] == "reply" and events[-1][1]["tier"] == "general"
    assert "".join(d["text"] for n, d in events if n == "delta") == events[-1][1]["message"]
    page = demo.get("/demo").text
    assert "turns/stream" in page and "No verified data" not in page


def test_only_a_sessions_calls_and_known_files_can_be_reached(demo):
    assert demo.post("/demo/api/cases", json={}).status_code == 404  # nothing that changes the knowledge base
    assert demo.post("/demo/api/ingest", json={}).status_code == 404
    assert demo.get("/demo/asset", params={"path": "../.env"}).status_code == 404
    assert demo.get("/demo/asset", params={"path": "eval/scenarios/assets/../../../.env"}).status_code == 404
    assert demo.get("/demo/diagram", params={"key": "secrets/x.png"}).status_code == 404


def test_the_evaluation_scenarios_are_offered_as_demo_conversations(demo):
    scenarios = demo.get("/demo/scenarios").json()
    assert len(scenarios) == 30 and all(s["turns"] and s["vehicle"]["make"] for s in scenarios)
    photo = next(t["attachments"][0] for s in scenarios for t in s["turns"] if t["attachments"])
    assert demo.get("/demo/asset", params={"path": photo}).status_code == 200


def test_demo_page_has_end_conversation_button_offline(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("DEMO_ENABLED", "true")
    monkeypatch.setattr(demo_router, "LOCAL", demo_router.LOCAL | {"testclient"})
    demo_router.viewer(SimpleNamespace(client=SimpleNamespace(host="testclient")))
    page = demo_router.page()
    html = page.body.decode("utf-8")
    assert page.status_code == 200
    assert 'id="close" disabled>End conversation</button>' in html
    assert "sessions/${session}/close" in html

"""The review screen. These tests need a database (see conftest.py)."""

from __future__ import annotations

import html
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api import review
from app.api.main import app
from app.config import get_settings
from app.ingest import jobs
from app.ingest.extractors import Extracted
from app.ingest.worker import Worker
from app.kb.schemas import Powertrain, Procedure, Review, Vehicle
from app.kb.store import KnowledgeStore

REPO = Path(__file__).resolve().parents[1]
INTEGRA = Vehicle(make="Acura", model="Integra", year=2025, engine_code="L15CA", powertrain=Powertrain.ICE)
NASOH = ("Nasoh", "let-me-in")


def p0010(notes: list[str]) -> Procedure:
    data = json.loads((REPO / "eval/golden/procedures/acura-integra-2025-l15ca-p0010.json").read_text(encoding="utf-8"))
    return Procedure(**data).model_copy(update={"review": Review(open_questions=notes)})


@pytest.fixture
def client(auto_conn, storage, monkeypatch) -> TestClient:
    monkeypatch.setattr(get_settings(), "review_password", SecretStr("let-me-in"))
    app.dependency_overrides[review.get_conn] = lambda: auto_conn
    app.dependency_overrides[review.get_storage] = lambda: storage
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def record_id(auto_conn, storage, database_url) -> int:
    """A procedure read from a file by the worker, with one failed check and one note."""
    storage.put("kb/p0010.txt", b"DTC P0010 source text, as the reviewer would see it", content_type="text/plain")
    done = jobs.register_file(auto_conn, storage, "kb/p0010.txt", kind="licensed_procedure")
    notes = ["Pins were read from images by a model.", "CHECK FAILED: step 3: pin 84 does not exist on PCM connector E (80P)"]
    Worker(database_url, storage, worker_id="w", extractors={"licensed_procedure": lambda d: [Extracted(p0010(notes), "model")]}).run_once()
    return jobs.job_status(auto_conn, done.job_id)["result"]["records"][0]["record_id"]


def test_the_screen_does_not_exist_until_a_password_is_set(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "review_password", None)
    assert client.get("/review", auth=NASOH).status_code == 404


def test_signing_in_needs_a_name_and_the_password(client):
    assert client.get("/review").status_code == 401
    assert client.get("/review", auth=("Nasoh", "wrong")).status_code == 401
    assert client.get("/review", auth=("  ", "let-me-in")).status_code == 401
    assert client.get("/review", auth=NASOH).status_code == 200


def test_drafts_are_listed_with_their_failed_checks(client, record_id):
    page = client.get("/review", auth=NASOH).text
    assert "proc:acura-integra-2025-l15ca:P0010" in page and "Acura Integra 2025 L15CA" in page and f"/review/{record_id}" in page
    assert "Nothing here" in client.get("/review?status=approved", auth=NASOH).text


def test_a_record_is_shown_beside_its_source_with_failed_checks_first(client, record_id):
    page = client.get(f"/review/{record_id}", auth=NASOH).text
    assert "DTC P0010 source text, as the reviewer would see it" in page  # the source, on the left
    assert "<b>pin 64</b>" in page and "7.14 to 7.9 ohm" in page and "more than 1 Mohm" in page  # what was read, on the right
    assert page.index("CHECK FAILED: step 3") < page.index("Pins were read from images")  # failed checks come first
    assert "There is a problem with the PCM." in page


def test_approving_a_record_with_failed_checks_needs_a_confirmation_and_records_the_name(client, record_id, auto_conn):
    store = KnowledgeStore(auto_conn)
    assert store.get_procedure(INTEGRA, "P0010") is None  # a draft is not served
    assert client.post(f"/review/{record_id}/approve", auth=NASOH).status_code == 409
    assert store.get_procedure(INTEGRA, "P0010") is None
    done = client.post(f"/review/{record_id}/approve?acknowledge_failed=true", auth=NASOH)
    assert done.json() == {"record_id": record_id, "status": "approved", "reviewer": "Nasoh"}
    assert store.get_procedure(INTEGRA, "P0010").status == "approved"
    row = auto_conn.execute("SELECT reviewer, reviewed_on FROM kb.record WHERE id = %s", (record_id,)).fetchone()
    assert row["reviewer"] == "Nasoh" and row["reviewed_on"] is not None
    assert "approved by Nasoh" in client.get(f"/review/{record_id}", auth=NASOH).text


def test_a_rejected_record_is_not_served(client, record_id, auto_conn):
    assert client.post(f"/review/{record_id}/reject", auth=NASOH).json()["status"] == "rejected"
    assert KnowledgeStore(auto_conn).get_procedure(INTEGRA, "P0010") is None
    assert "proc:acura-integra" in client.get("/review?status=rejected", auth=NASOH).text


def test_the_source_file_is_served_only_to_a_signed_in_reviewer(client, record_id):
    assert client.get(f"/review/{record_id}/source").status_code == 401
    got = client.get(f"/review/{record_id}/source", auth=NASOH)
    assert got.status_code == 200 and got.content.startswith(b"DTC P0010 source text")
    assert client.get("/review/999999", auth=NASOH).status_code == 404


def test_text_from_a_file_cannot_inject_markup_into_the_page(client, auto_conn, storage, database_url):
    storage.put("kb/x.txt", b"<script>alert(1)</script>", content_type="text/plain")
    done = jobs.register_file(auto_conn, storage, "kb/x.txt", kind="licensed_procedure")
    evil = p0010(["<img src=x onerror=alert(1)>"])
    Worker(database_url, storage, worker_id="w", extractors={"licensed_procedure": lambda d: [Extracted(evil, "model")]}).run_once()
    rid = jobs.job_status(auto_conn, done.job_id)["result"]["records"][0]["record_id"]
    page = client.get(f"/review/{rid}", auth=NASOH).text
    assert "<script>alert(1)</script>" not in page and "<img src=x" not in page and "&lt;script&gt;" in page


# -- nothing a mechanic would be told may be hidden from the reviewer ------------------------
def text_fields(value, path=""):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield from text_fields(inner, f"{path}.{key}")
    elif isinstance(value, list):
        for inner in value:
            yield from text_fields(inner, path)
    elif isinstance(value, str) and value.strip():
        yield path, value


# Shown in another form: the id is the page's heading, a test point is shown by its connector, pin
# and side, and a comparison by its words ("less than").
SHOWN_DIFFERENTLY = (".source", ".review", ".procedure_id", ".case_id", ".note_id", ".steps.measure.from.label",
                     ".steps.measure.to.label", ".steps.normal.op")


@pytest.mark.parametrize("path", sorted((REPO / "eval/golden/procedures").glob("*.json")) + sorted((REPO / "seed").glob("*/*.json")),
                         ids=lambda p: p.stem)
def test_every_text_field_of_a_record_is_on_its_page(path):
    from app.api.review import render_record

    body = json.loads(path.read_text(encoding="utf-8"))
    kind = {"procedures": "procedure", "cases": "case", "expert_notes": "expert_note"}[path.parent.name]
    page = render_record(kind, body, 1)
    hidden = [where for where, text in text_fields(body) if not where.startswith(SHOWN_DIFFERENTLY) and html.escape(text) not in page]
    assert hidden == []


def test_a_diagram_page_shows_its_car_pages_components_and_pins_in_order():
    from app.api.review import render_record
    from app.ingest.diagram_extractor import extract_diagram
    from app.ingest.extractors import SourceDocument
    from tests.test_diagrams import build

    document = SourceDocument(file_id=1, storage_key="k", original_name="Test Diagram.pdf", kind="licensed_diagram",
                              declared={}, data=build())
    body = json.loads(extract_diagram(document)[0].record.model_dump_json())
    page = render_record("diagram", body, 1)
    shown_differently = (".source", ".review", ".diagram_key", ".sheets.image_key", ".pins.routes.hops", ".verdict")
    hidden = [where for where, text in text_fields(body) if not where.startswith(shown_differently) and html.escape(text) not in page]
    assert hidden == []
    assert page.index("<b>1</b>") < page.index("<b>2</b>") < page.index("<b>3</b>")  # TEST UNIT's pins, as numbered

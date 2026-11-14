"""From a diagram PDF to stored pins and routes. The end-to-end tests need a database (see conftest.py)."""

from __future__ import annotations

import pymupdf
import pytest
from tests.test_diagrams import build, new_page

from app.ingest import jobs
from app.ingest.diagram_extractor import extract_diagram, vehicle_from
from app.ingest.extractors import SourceDocument
from app.ingest.jobs import PermanentError
from app.ingest.worker import Worker
from app.kb.schemas import Vehicle, WiringDiagram
from app.kb.store import APPROVED_AND_DRAFT, KnowledgeStore

KEY = "kb/licensed_diagram/test/diagram.pdf"
CAR = Vehicle(make="Test", model="Car", year=2025, engine_code="T15")


def document(data: bytes, declared: dict | None = None) -> SourceDocument:
    return SourceDocument(7, KEY, "Test Diagram.pdf", "licensed_diagram", declared or {}, data)


# -- the vehicle ----------------------------------------------------------------------
def test_the_vehicle_is_read_from_the_header():
    vehicle, questions = vehicle_from("2025 Acura Integra L4-1.5L Turbo (L15CA)", {})
    assert (vehicle.make, vehicle.model, vehicle.year, vehicle.engine_code, questions) == ("Acura", "Integra", 2025, "L15CA", [])


def test_a_declared_make_and_model_win_because_the_header_cannot_split_two_word_makes():
    vehicle, questions = vehicle_from("2021 Land Rover Defender L4-2.0L Turbo (PT204)", {"make": "Land Rover", "model": "Defender"})
    assert (vehicle.make, vehicle.model, vehicle.year, vehicle.engine_code) == ("Land Rover", "Defender", 2021, "PT204")
    assert len(questions) == 1  # the difference is put to the reviewer


def test_a_file_with_no_vehicle_at_all_is_refused():
    with pytest.raises(PermanentError):
        vehicle_from("", {})


# -- the record -------------------------------------------------------------------------
def test_a_diagram_becomes_one_record_with_pins_routes_and_page_images():
    [extracted] = extract_diagram(document(build()))
    record = extracted.record
    assert isinstance(record, WiringDiagram) and extracted.extracted_by == "code"
    assert (record.vehicle, record.title, record.verdict) == (CAR, "Test Diagram", "traced")
    assert [s.page for s in record.sheets] == ["1 of 2", "2 of 2"]
    assert [a.key for a in extracted.assets] == [s.image_key for s in record.sheets]
    assert all(a.data.startswith(b"\x89PNG") for a in extracted.assets)
    pin = next(p for p in record.pins if (p.component, p.pin) == ("TEST UNIT", "1"))
    assert pin.wire_colour == "RED" and sorted(r.text for r in pin.routes) == [
        "TEST UNIT pin 1 -RED-> C224 pin 8 -BLU-> -BLU-> SENSOR A pin 2",
        "TEST UNIT pin 1 -RED-> C224 pin 8 -BLU-> -BLU-> ground G101",
    ] and all(r.complete for r in pin.routes)
    assert {c.name for c in record.components} == {"TEST UNIT", "SENSOR A", "HORN", "LAMP"}
    assert record.source.licensed and record.review.status == "draft"


def test_a_file_the_reader_does_not_understand_is_stored_without_pins():
    doc = pymupdf.open()
    page = new_page(doc, 1, 1)
    page.draw_line((100, 300), (400, 300), color=(0, 0, 0), width=3)
    [extracted] = extract_diagram(document(doc.tobytes(), {"make": "Test", "model": "Car", "year": 2025}))
    record = extracted.record
    assert record.verdict == "show_only" and record.pins == [] and record.verdict_reasons
    assert any("could not attach" in q for q in record.review.open_questions)


def test_a_file_that_is_not_a_pdf_is_refused():
    with pytest.raises(PermanentError):
        extract_diagram(document(b"this is not a pdf"))


# -- end to end: add the file, the worker reads it, the pins can be looked up ---------------
@pytest.fixture
def stored(auto_conn, storage, database_url) -> KnowledgeStore:
    storage.put(KEY, build(), content_type="application/pdf")
    done = jobs.register_file(auto_conn, storage, KEY, kind="licensed_diagram")
    assert Worker(database_url, storage, worker_id="test-worker").run_once()
    status = jobs.job_status(auto_conn, done.job_id)
    assert status["status"] == "succeeded", status["error"]
    assert storage.head(f"derived/diagrams/{done.file_id}/1-of-2.png") is not None
    store = KnowledgeStore(auto_conn)
    store.set_review(status["result"]["records"][0]["record_id"], "approved", reviewer="test reviewer")
    return store


def test_a_pin_is_looked_up_by_car_component_and_pin(stored):
    [hit] = stored.get_pins(CAR, "test unit", "2")
    assert (hit.component, hit.pin, hit.wire_colour, hit.page, hit.verdict) == ("TEST UNIT", "2", "GRN", "1 of 2", "traced")
    assert [r["text"] for r in hit.routes] == ["TEST UNIT pin 2 -GRN-> (page 2 of 2) -GRN-> LAMP pin 1"]
    assert [h.pin for h in stored.get_pins(CAR, "TEST UNIT")] == ["1", "2", "3"]


@pytest.mark.parametrize(
    "vehicle",
    [CAR.model_copy(update={"year": 2024}), CAR.model_copy(update={"model": "Van"}), CAR.model_copy(update={"engine_code": "X99"}),
     CAR.model_copy(update={"year": None})],
    ids=["other year", "other model", "other engine", "no year"],
)
def test_another_car_never_gets_these_pins(stored, vehicle):
    assert stored.get_pins(vehicle, "TEST UNIT", "1") == []


def test_a_component_is_found_by_words_and_then_its_pins_by_name(stored):
    hits = stored.search_text(CAR, "which pins does the sensor have", record_types=("diagram",))
    assert hits and hits[0].matched_units[0].text == "SENSOR A"
    assert [h.pin for h in stored.get_pins(CAR, hits[0].matched_units[0].text)] == ["2"]


def test_a_diagram_that_is_not_approved_is_not_served(auto_conn, storage, database_url):
    storage.put(KEY, build(), content_type="application/pdf")
    jobs.register_file(auto_conn, storage, KEY, kind="licensed_diagram")
    Worker(database_url, storage, worker_id="test-worker").run_once()
    store = KnowledgeStore(auto_conn)
    assert store.get_pins(CAR, "TEST UNIT", "1") == []
    assert len(store.get_pins(CAR, "TEST UNIT", "1", statuses=APPROVED_AND_DRAFT)) == 1


# -- one component drawn once per version of the car ------------------------------------------
def test_a_component_drawn_twice_is_stored_once_per_version_with_its_own_wire():
    import pymupdf

    from tests.test_diagrams import two_versions_page

    doc = pymupdf.open()
    two_versions_page(doc, 1, 2, "EXCEPT CVT", "CVT")
    two_versions_page(doc, 2, 2, "TYPE-S", "EXCEPT TYPE-S")
    document = SourceDocument(file_id=1, storage_key="k", original_name="Versions.pdf", kind="licensed_diagram", declared={},
                              data=doc.tobytes())
    record = extract_diagram(document)[0].record
    sensors = sorted((p.page, p.pin, p.wire_colour, p.version) for p in record.pins if p.component == "SPEED SENSOR")
    assert sensors == [("1 of 2", "1", "BLU", "2 = CVT"), ("1 of 2", "1", "RED", "1 = EXCEPT CVT"),
                       ("2 of 2", "1", "BLU", "2 = EXCEPT TYPE-S"), ("2 of 2", "1", "RED", "1 = TYPE-S")]
    # the unit the two drawings share is not tied to a version
    assert {p.version for p in record.pins if p.component == "TEST UNIT"} == {None}
    # the markers mean different cars on the two pages: no single legend for the whole diagram
    assert record.versions == {} and [sheet.versions for sheet in record.sheets] == [
        {"1": "EXCEPT CVT", "2": "CVT"}, {"1": "TYPE-S", "2": "EXCEPT TYPE-S"}]

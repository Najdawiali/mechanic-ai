"""From a DTC procedure PDF to a Procedure record, with a stand-in for the model.

The licensed P0010 file is not in the repository. These tests build a small PDF with the same
ingredients (text, and the test points printed as small images) and use the hand-written P0010
record as what a correct model would answer. `scripts/score_procedure.py` runs the real models
on the real file.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from app.ingest.extractors import SourceDocument
from app.ingest.jobs import PermanentError
from app.ingest.pdf_text import read_pdf_text
from app.ingest.procedure_extractor import ImageReading, check_procedure, extract_procedure, text_with_readings
from app.kb.schemas import Procedure

REPO = Path(__file__).resolve().parents[1]
GOLDEN = json.loads((REPO / "eval/golden/procedures/acura-integra-2025-l15ca-p0010.json").read_text(encoding="utf-8"))
POINTS = [("1", "2"), ("2", "ground"), ("1", "64"), ("64", "ground")]  # what the four images show, in order
VALVE, PCM = "VTC oil control solenoid valve A connector (2P)", "PCM connector E (80P)"


def golden() -> Procedure:
    return Procedure(**GOLDEN)


def build_pdf(images: int = 4) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    lines = ["2025 Acura Integra L4-1.5L Turbo (L15CA)", "Vehicle > ALL Diagnostic Trouble Codes ( DTC ) > P Code Charts > P0010 >",
             "DTC Troubleshooting: P0010", "DTC P0010: Variable Valve Timing Control (VTC) Oil Control Solenoid Valve A Malfunction",
             f"{VALVE}: disconnected", f"{PCM}: disconnected", "Valve internal circuit inspection OFF About 7.14 - 7.90 Ω",
             "PG open wire inspection OFF There is less than 1 Ω", "VTC(IN) shorted to ground inspection OFF There is more than 1 MΩ",
             "If abnormal, there is a problem in the circuit. There is a problem with the PCM." + " filler" * 20]
    for i, line in enumerate(lines):
        page.insert_text((40, 60 + 22 * i), line, fontsize=8, fontname="china-s" if "Ω" in line else "helv")
    icon = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 120, 24), False)
    icon.clear_with(230)
    for i in range(images):
        page.insert_image(pymupdf.Rect(400, 190 + 22 * i, 490, 208 + 22 * i), pixmap=icon)
    return doc.tobytes()


class FakeGateway:
    """Answers like a model would: the test points for each image, then the procedure as JSON."""

    def __init__(self, points=POINTS, answers: list[str] | None = None):
        self.points, self.images_seen, self.writes = list(points), 0, 0
        body = {k: v for k, v in GOLDEN.items() if k not in ("procedure_id", "vehicle", "source", "review")}
        self.answers = answers if answers is not None else [json.dumps(body)]

    def chat(self, messages, *, role, **_):
        if role == "vision":
            start, end = self.points[self.images_seen]
            self.images_seen += 1
            return SimpleNamespace(text=json.dumps({"kind": "measurement", "from": start, "to": end, "quantity": "resistance"}))
        self.writes += 1
        return SimpleNamespace(text=self.answers[min(self.writes, len(self.answers)) - 1])


def document(data: bytes) -> SourceDocument:
    return SourceDocument(3, "kb/p0010.pdf", "P0010.pdf", "licensed_procedure", {}, data)


def readings(points=POINTS) -> list[ImageReading]:
    return [ImageReading(n, "measurement", a, b, "resistance") for n, (a, b) in enumerate(points, start=1)]


# -- text with image markers -------------------------------------------------------------
def test_each_image_leaves_a_marker_where_it_sits():
    pdf = read_pdf_text(build_pdf())
    assert pdf.header[0] == "2025 Acura Integra L4-1.5L Turbo (L15CA)"
    assert [(i.number, i.page, i.pixels) for i in pdf.images] == [(n, 1, (120, 24)) for n in (1, 2, 3, 4)]
    assert all(f"[image {n}]" in pdf.text for n in (1, 2, 3, 4)) and all(i.png.startswith(b"\x89PNG") for i in pdf.images)
    assert pdf.text.index("7.14") < pdf.text.index("[image 1]") < pdf.text.index("[image 4]")  # in reading order
    noted = text_with_readings(pdf, readings())
    assert "[measurement 3: from pin 1 to pin 64, resistance]" in noted and "[image" not in noted


# -- the checks ---------------------------------------------------------------------------
TEXT = read_pdf_text(build_pdf()).text


def test_a_correct_record_passes_every_check():
    assert check_procedure(golden(), TEXT, readings()) == []


def change(step: int, path: list[str], value) -> Procedure:
    data = json.loads(json.dumps(GOLDEN))
    target = data["steps"][step]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return Procedure(**data)


@pytest.mark.parametrize(
    ("procedure", "expected"),
    [
        (change(2, ["measure", "to", "pin"], "46"), "no image in the PDF shows a measurement between 1, 46"),
        (change(0, ["normal", "min"], 7.41), "the value 7.41 is not printed in the PDF"),
        (change(2, ["measure", "to", "connector"], VALVE), "pin 64 does not exist on " + VALVE),
        (change(0, ["measure", "from", "connector"], "Camshaft sensor connector (3P)"), "is not printed in the PDF"),
        (change(3, ["normal", "unit"], "kohm"), "the unit kohm is not printed in the PDF"),
    ],
    ids=["swapped digits in a pin", "wrong value", "pin on the wrong connector", "invented connector", "wrong unit"],
)
def test_a_wrong_record_is_caught_without_an_answer_key(procedure, expected):
    assert any(expected in problem for problem in check_procedure(procedure, TEXT, readings()))


def test_every_measurement_image_must_be_used_once():
    data = json.loads(json.dumps(GOLDEN))
    del data["steps"][3]
    data["steps"][2]["if_normal"] = None
    assert any("each image must be used once" in p for p in check_procedure(Procedure(**data), TEXT, readings()))


def test_an_image_that_could_not_be_read_is_reported():
    unread = readings()[:3] + [ImageReading(4, "measurement", "64", None, "resistance")]
    assert any("image 4 could not be read" in p for p in check_procedure(golden(), TEXT, unread))


# -- the extractor --------------------------------------------------------------------------
def test_a_procedure_is_extracted_with_the_pins_and_values_of_the_reference_record():
    gateway = FakeGateway()
    [extracted] = extract_procedure(document(build_pdf()), gateway)
    record = extracted.record
    assert (gateway.images_seen, gateway.writes, extracted.extracted_by) == (4, 1, "model")
    assert (record.procedure_id, record.code) == (GOLDEN["procedure_id"], "P0010")
    assert (record.vehicle.make, record.vehicle.model, record.vehicle.year, record.vehicle.engine_code) == ("Acura", "Integra", 2025, "L15CA")
    assert record.steps == golden().steps and record.elimination_conclusion == golden().elimination_conclusion
    assert record.review.status == "draft" and record.source.licensed
    assert not any(q.startswith("CHECK FAILED") for q in record.review.open_questions)


def test_a_misread_image_leaves_a_failed_check_on_the_draft():
    wrong = [("1", "2"), ("2", "ground"), ("1", "84"), ("64", "ground")]  # the model misreads 64 as 84
    [extracted] = extract_procedure(document(build_pdf()), FakeGateway(points=wrong))
    failed = [q for q in extracted.record.review.open_questions if q.startswith("CHECK FAILED")]
    assert any("no image in the PDF shows a measurement between 1, 64" in q for q in failed)


def test_an_answer_that_is_not_a_procedure_is_sent_back_once():
    good = FakeGateway().answers[0]
    gateway = FakeGateway(answers=["I could not find any steps.", good])
    [extracted] = extract_procedure(document(build_pdf()), gateway)
    assert gateway.writes == 2 and extracted.record.steps == golden().steps
    with pytest.raises(RuntimeError):
        extract_procedure(document(build_pdf()), FakeGateway(answers=["not JSON", "still not JSON"]))


def test_a_file_without_text_is_refused():
    doc = pymupdf.open()
    doc.new_page()
    with pytest.raises(PermanentError):
        extract_procedure(document(doc.tobytes()), FakeGateway())

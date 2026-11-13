"""From a solved-case report to a Case record, with a stand-in for the model."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pymupdf
import pytest

from app.ingest.case_extractor import check_case, extract_case, report_text
from app.ingest.extractors import SourceDocument
from app.ingest.jobs import PermanentError
from app.kb.schemas import Case

REPORT = """Vehicle: Tesla Model X, 2020, battery electric. Technician: Miqdad.
Alert: TAS_a211_noRideHeightCalib. Automatic leveling was disabled.
Checks: the four ride-height reference points were measured on level ground.
Diagnosis: no valid ride-height calibration values were stored in the TAS ECU.
Repair: ride-height calibration with Tesla Toolbox. The alert cleared after a short drive.
The technician says the same applies to Model S and Model X from 2016 to 2021. Unlike TAS_a025, no part was replaced.
"""
ANSWER = {
    "make": "Tesla", "models": ["Model X"], "year_from": 2020, "year_to": 2020, "powertrain": "EV", "engine": None,
    "system": "Air suspension", "modules": ["TAS"], "scan_tools": ["Tesla Toolbox"],
    "fault_codes": [{"code": "TAS_a211_noRideHeightCalib", "text_on_tool": None, "tool": "Tesla Toolbox", "module": "TAS", "tool_note": None}],
    "symptoms": ["Automatic leveling disabled"], "findings": ["The four ride-height reference points were measured on level ground"],
    "diagnosis": "No valid ride-height calibration values were stored in the TAS ECU.", "root_cause": None,
    "repair_steps": ["Ride-height calibration with Tesla Toolbox"], "parts_replaced": [], "parts_not_replaced": [],
    "outcome": "confirmed_fixed", "occurrences": ["The technician says the same applies to Model S and Model X from 2016 to 2021."],
    "sensitive": False, "technician": "Miqdad", "date": None, "specific_car": True,
}


class FakeGateway:
    def __init__(self, *answers):
        self.answers, self.calls = [a if isinstance(a, str) else json.dumps(a) for a in answers], 0

    def chat(self, messages, *, role, **_):
        self.calls += 1
        return SimpleNamespace(text=self.answers[min(self.calls, len(self.answers)) - 1])


def document(text: str = REPORT, name: str = "Tesla_Model_X_2020_TAS_a211_EN.txt") -> SourceDocument:
    return SourceDocument(5, "kb/case.txt", name, "client_case_file", {}, text.encode("utf-8"))


def extract(answer: dict, text: str = REPORT) -> Case:
    [extracted] = extract_case(document(text), FakeGateway(answer))
    assert extracted.extracted_by == "model" and extracted.record.review.status == "draft"
    return extracted.record


def test_a_report_becomes_a_draft_case():
    case = extract(ANSWER)
    assert case.case_id == "case:tesla-model-x-2020:tas-a211-norideheightcalib"
    assert (case.make, case.models, case.year_from, case.outcome) == ("Tesla", ["Model X"], 2020, "confirmed_fixed")
    assert not case.source.licensed and not any(q.startswith("CHECK FAILED") for q in case.review.open_questions)


def test_a_code_mentioned_in_the_report_but_left_out_is_pointed_out():
    assert any(q == "CHECK: the report mentions TAS_a025, which is not among the record's fault codes" for q in extract(ANSWER).review.open_questions)


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"fault_codes": [{"code": "TAS_a212_noRideHeightCalib"}]}, "CHECK FAILED: the fault code TAS_a212_noRideHeightCalib is not written in the report"),
        ({"year_from": 2019, "year_to": 2019}, "CHECK FAILED: the year 2019 is not written in the report"),
        ({"models": ["Model X", "Model 3"]}, "CHECK FAILED: the model 'Model 3' is not written in the report"),
        ({"make": "BYD"}, "CHECK FAILED: the make 'BYD' is not written in the report"),
        ({"technician": "Nasoh"}, "CHECK FAILED: the technician name 'Nasoh' is not written in the report"),
    ],
    ids=["changed code", "guessed year", "added model", "wrong make", "invented name"],
)
def test_what_the_report_does_not_say_is_caught(change, expected):
    assert expected in extract(ANSWER | change).review.open_questions


def test_a_code_written_with_other_separators_still_counts_as_written():
    case = Case(**{k: v for k, v in ANSWER.items() if k != "specific_car"}, case_id="c",
                source={"kind": "client_case_file", "file": "f"})
    assert not [n for n in check_case(case, REPORT.replace("TAS_a211_noRideHeightCalib", "tas a211 no ride height calib")) if "FAILED" in n]


def test_an_unknown_year_stays_unknown_and_is_said_so():
    report = REPORT.replace("Tesla Model X, 2020", "Tesla Model X")
    case = extract(ANSWER | {"year_from": None, "year_to": None}, report)
    assert case.year_from is None and case.case_id.startswith("case:tesla-model-x-year-unknown:")
    assert "The report does not give: year." in case.review.open_questions


def test_general_advice_and_dangerous_work_are_flagged_for_the_reviewer():
    questions = extract(ANSWER | {"specific_car": False, "sensitive": True}).review.open_questions
    assert any("no particular car" in q for q in questions) and any("owner must approve in writing" in q for q in questions)


def test_a_fault_wording_is_not_stored_as_a_code():
    # First real run, BYD Zero Six: the report gives wordings and no code numbers.
    report = REPORT + "Recorded faults: BIC8: Voltage sampling abnormal.\n"
    case = extract(ANSWER | {"fault_codes": [{"code": "Voltage sampling abnormal", "module": "BIC8"}]}, report)
    assert [(f.code, f.text_on_tool) for f in case.fault_codes] == [(None, "Voltage sampling abnormal")]
    assert "The report does not give: fault code." in case.review.open_questions
    assert not any(q.startswith("CHECK FAILED") for q in case.review.open_questions)


def test_a_powertrain_word_is_not_a_model_and_an_unstated_powertrain_is_flagged():
    # First real run, EXEED: the model wrote models = ["Hybrid"]; BYD DM-i: it worked out PHEV by itself.
    case = extract(ANSWER | {"models": ["Hybrid"], "powertrain": "PHEV"})
    assert case.models == [] and "The report does not give: model." in case.review.open_questions
    assert "CHECK FAILED: the report does not state the powertrain; the record says PHEV" in case.review.open_questions
    assert not any("powertrain" in q for q in extract(ANSWER | {"powertrain": None}).review.open_questions)


def test_a_scan_tool_must_be_written_in_the_report():
    assert "CHECK FAILED: the scan tool 'Autel' is not written in the report" in extract(ANSWER | {"scan_tools": ["Autel"]}).review.open_questions


def test_a_case_without_a_code_is_named_after_its_file():
    case = extract(ANSWER | {"fault_codes": []})
    assert case.case_id == "case:tesla-model-x-2020:tesla-model-x-2020-tas-a211-en"


def test_a_pdf_report_is_read_and_its_screenshots_are_counted():
    doc = pymupdf.open()
    page = doc.new_page()
    for i, line in enumerate(REPORT.splitlines()):
        page.insert_text((40, 60 + 14 * i), line, fontsize=8)
    shot = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 100), False)
    shot.clear_with(200)
    page.insert_image(pymupdf.Rect(40, 300, 240, 400), pixmap=shot)
    pdf = SourceDocument(5, "kb/case.pdf", "case.pdf", "client_case_file", {}, doc.tobytes())
    text, images = report_text(pdf)
    assert images == 1 and "[screenshot]" in text and "TAS_a211_noRideHeightCalib" in text
    [extracted] = extract_case(pdf, FakeGateway(ANSWER))
    assert any("1 screenshot(s) that were not read" in q for q in extracted.record.review.open_questions)


def test_an_answer_that_is_not_a_case_is_sent_back_once():
    gateway = FakeGateway("no JSON here", ANSWER)
    [extracted] = extract_case(document(), gateway)
    assert gateway.calls == 2 and extracted.record.make == "Tesla"
    with pytest.raises(RuntimeError):
        extract_case(document(), FakeGateway({"make": "Tesla"}))


def test_an_empty_file_is_refused():
    with pytest.raises(PermanentError):
        extract_case(document("too short"), FakeGateway(ANSWER))

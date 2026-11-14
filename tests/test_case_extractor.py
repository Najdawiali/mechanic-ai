"""From a solved-case report to a Case record, with a stand-in for the model."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pymupdf
import pytest

from app.ingest.case_extractor import check_case, extract_case, report_text, tidy
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
    "make": "Tesla", "cars": [{"model": "Model X", "year": 2020}], "powertrain": "EV", "engine": None,
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
    assert case.case_id == "case:tesla-model-x-2020-tas-a211"  # named after the file, so it is the same on every run
    assert (case.make, case.models, case.year_from, case.outcome) == ("Tesla", ["Model X"], 2020, "confirmed_fixed")
    assert not case.source.licensed and not any(q.startswith("CHECK FAILED") for q in case.review.open_questions)


def test_a_code_mentioned_in_the_report_but_left_out_is_pointed_out():
    assert any(q == "CHECK: the report mentions TAS_a025, which is not among the record's fault codes" for q in extract(ANSWER).review.open_questions)


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"fault_codes": [{"code": "TAS_a212_noRideHeightCalib"}]}, "CHECK FAILED: the fault code TAS_a212_noRideHeightCalib is not written in the report"),
        ({"cars": [{"model": "Model X", "year": 2019}]}, "CHECK: the model gave 2019 as the year of a repaired car; the report writes that only "
                                                         "as part of a range, or not at all, so the car's year is left unknown."),
        ({"cars": [{"model": "Model X", "year": 2020}, {"model": "Model 3", "year": None}]}, "CHECK FAILED: the model 'Model 3' is not written in the report"),
        ({"make": "BYD"}, "CHECK FAILED: the make 'BYD' is not written in the report"),
        ({"technician": "Nasoh"}, "CHECK FAILED: the technician name 'Nasoh' is not written in the report"),
    ],
    ids=["changed code", "guessed year", "added model", "wrong make", "invented name"],
)
def test_what_the_report_does_not_say_is_caught(change, expected):
    assert expected in extract(ANSWER | change).review.open_questions


def test_a_code_written_with_other_separators_still_counts_as_written():
    data = json.loads(json.dumps({k: v for k, v in ANSWER.items() if k != "specific_car"}))
    tidy(data)
    case = Case(**data, case_id="c", source={"kind": "client_case_file", "file": "f"})
    assert not [n for n in check_case(case, REPORT.replace("TAS_a211_noRideHeightCalib", "tas a211 no ride height calib")) if "FAILED" in n]


def test_an_unknown_year_stays_unknown_and_is_said_so():
    report = REPORT.replace("Tesla Model X, 2020", "Tesla Model X")
    case = extract(ANSWER | {"cars": [{"model": "Model X", "year": None}]}, report)
    assert case.year_from is None
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
    case = extract(ANSWER | {"cars": [{"model": "Hybrid", "year": None}], "powertrain": "PHEV"})
    assert case.models == [] and "The report does not give: model, year." in case.review.open_questions
    assert "CHECK FAILED: the report does not state the powertrain; the record says PHEV" in case.review.open_questions
    assert not any("powertrain" in q for q in extract(ANSWER | {"powertrain": None}).review.open_questions)


def test_a_report_on_two_cars_keeps_each_model_with_its_own_year():
    # Full real run: one report covers a 2024 Model S and a 2022 Model Y; only the first was recorded.
    from app.kb.index import scope_rows

    report = REPORT + "Case 1: Tesla Model S 2024. Case 2: Tesla Model Y 2022.\n"
    case = extract(ANSWER | {"cars": [{"model": "Model S", "year": 2024}, {"model": "Model Y", "year": 2022}]}, report)
    assert (case.models, case.year_from, case.year_to) == (["Model S", "Model Y"], 2022, 2024)
    assert [(r.model_key, r.year_from, r.year_to) for r in scope_rows(case)] == [("MODELS", 2024, 2024), ("MODELY", 2022, 2022)]


def test_a_code_written_inside_a_wording_is_recovered():
    # Full real run: the report writes "TAS_A025_Crash Event" and the code was lost.
    report = REPORT + "Alert: TAS_A025_Crash Event.\n"
    case = extract(ANSWER | {"fault_codes": [{"code": "TAS_A025_Crash Event"}]}, report)
    assert [(f.code, f.text_on_tool) for f in case.fault_codes] == [("TAS_A025_Crash", "TAS_A025_Crash Event")]
    assert not any("TAS_A025" in q for q in case.review.open_questions)


def test_an_arabic_report_is_not_used():
    arabic = "السيارة: Volkswagen ID.4 الموديل: 2021 أكواد الأعطال P0C4400 " + "تم إعادة توصيل الفيش وتثبيتها ومسح الأعطال والتأكد من عمل النظام " * 4
    with pytest.raises(PermanentError, match="not in English"):
        extract_case(document(arabic), FakeGateway(ANSWER))


def test_dangerous_work_is_flagged_from_the_report_even_when_the_model_does_not():
    case = extract(ANSWER | {"sensitive": False}, REPORT + "The high-voltage connector was reseated.\n")
    assert case.sensitive and any("owner must approve in writing" in q for q in case.review.open_questions)
    assert not extract(ANSWER).sensitive


def test_a_general_word_is_not_a_tool_name():
    case = extract(ANSWER | {"scan_tools": ["scan tool", "Tesla Toolbox"]})
    assert case.scan_tools == ["Tesla Toolbox"]


def test_a_scan_tool_must_be_written_in_the_report():
    assert "CHECK FAILED: the scan tool 'Autel' is not written in the report" in extract(ANSWER | {"scan_tools": ["Autel"]}).review.open_questions


def test_copies_of_one_report_share_one_name():
    from app.ingest.case_extractor import case_name

    names = {case_name(n) for n in ("Tesla_TAS_A025_Crash_EN.txt", "Tesla_TAS_A025_Crash_EN (1).txt", "Tesla_TAS_A025_Crash_EN(1).txt",
                                    "Tesla_TAS_A025_Crash_EN.pdf", "Tesla_TAS_A025_Crash.txt")}
    assert names == {"tesla-tas-a025-crash"}
    assert extract(ANSWER | {"fault_codes": []}).case_id == extract(ANSWER | {"cars": []}).case_id  # whatever the model read


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


# -- nothing technical in the report may be left behind ----------------------------------------
ADVICE = ("\nMeasure vertically from a level floor to the underbody reference points near the subframes. "
          "This alert alone does not establish a failed height sensor.\n")


def test_what_the_model_leaves_out_is_kept_as_a_note_in_the_reports_words():
    case = extract(ANSWER, REPORT + ADVICE)
    assert case.notes == ["Measure vertically from a level floor to the underbody reference points near the subframes. "
                          "This alert alone does not establish a failed height sensor."]
    assert any(q.startswith("CHECK: the model left out 1 passage(s)") for q in case.review.open_questions)


def test_when_the_model_keeps_them_nothing_is_added():
    kept = ANSWER | {"notes": ["Measure vertically from a level floor to the underbody reference points near the subframes.",
                               "This alert alone does not establish a failed height sensor."]}
    case = extract(kept, REPORT + ADVICE)
    assert len(case.notes) == 2 and not any("left out" in q for q in case.review.open_questions)


def test_a_note_the_report_does_not_say_is_caught():
    case = extract(ANSWER | {"notes": ["Always replace both rear height sensors together with new brackets."]})
    assert any(q.startswith("CHECK FAILED: this note is not in the report's own words") for q in case.review.open_questions)


def test_the_plain_report_has_nothing_left_out():
    case = extract(ANSWER)
    assert case.notes == [] and not any("left out" in q for q in case.review.open_questions)


def test_headings_labels_keywords_and_what_the_report_lacks_are_not_content():
    from app.ingest.case_extractor import left_out

    report = """# Tesla Model 3 - Reduced Charging Limit

## Diagnostic steps
1. From the vehicle display, navigate to CAN Viewer; do not search for
   CAN Viewer within the Toolbox menus.
2. Calculate the difference between the highest and lowest readings.

## Notes
- Model year, VIN, replacement module identifier, and scan screenshots were not provided.
- Real-world technician case; the fault screenshot is included only in the English PDF.

## Information not recorded
- Exact diagnostic trouble code(s) and wording of the message.
- Replacement module part number and serial number.

## Search keywords
Tesla, Model 3, Model Y, Long Range, Charge level reduced, BMS, CAN Viewer, Brick Voltage Min, Brick Voltage Max, Toolbox
"""
    assert left_out({}, report) == [
        "From the vehicle display, navigate to CAN Viewer; do not search for CAN Viewer within the Toolbox menus.",
        "Calculate the difference between the highest and lowest readings."]
    assert left_out({"notes": ["navigate to CAN Viewer from the vehicle display, not within the Toolbox menus"]}, report) == [
        "Calculate the difference between the highest and lowest readings."]


def test_a_year_written_only_as_the_end_of_a_range_is_not_a_repaired_cars_year():
    report = REPORT.replace("Tesla Model X, 2020,", "Tesla Model X, model years 2019-2021,")
    answer = ANSWER | {"cars": [{"model": "Model X", "year": year} for year in (2019, 2020, 2021)], "occurrences": []}
    case = extract(answer, report)
    assert [(car.model, car.year) for car in case.repaired_cars] == [("Model X", None)] and case.year_from is None
    assert "The report names model years 2019–2021." in case.occurrences
    assert any("2019, 2020, 2021" in q and "left unknown" in q for q in case.review.open_questions)
    assert not any(q.startswith("CHECK FAILED: the year") for q in case.review.open_questions)


def test_bare_headings_and_sections_of_things_to_confirm_are_not_content():
    from app.ingest.case_extractor import left_out

    report = """Case Title: Voltage Sampling Faults After Installation of a Mismatched Battery

Vehicle Information
- Make and model: BYD Zero Six (model name as provided by the technician)

Outcome
The recorded corrective action was installation of a battery with the matching configuration.

## Information to confirm
- Exact Toolbox menu wording and path; update this procedure when provided.
- Software versions before and after the procedure and final scan results.
"""
    assert left_out({}, report) == ["The recorded corrective action was installation of a battery with the matching configuration."]


def test_a_note_that_only_says_what_the_report_lacks_is_dropped_and_a_real_caution_is_kept():
    notes = ["VIN, battery part number, and complete numeric fault codes were not provided.",
             "The alert cleared; the clearing function is not available in every version of Tesla Toolbox."]
    report = REPORT + "The alert cleared; the clearing function is not available in every version of Tesla Toolbox.\n"
    assert extract(ANSWER | {"notes": notes}, report).notes == notes[1:]

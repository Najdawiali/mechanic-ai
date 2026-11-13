"""Schemas behave, and every draft file in seed/ and eval/scenarios/ is valid."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.kb.schemas import Case, ExpertNote, NormalRange, Origin, Procedure, Review, Safety
from eval.scenario_schema import Scenario

REPO = Path(__file__).resolve().parents[1]


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_normal_range_is_checked_in_code():
    between = NormalRange(op="between", min=5, max=10, unit="ohm")
    assert between.contains(5) and between.contains(10) and not between.contains(15)
    assert NormalRange(op="lt", max=1, unit="ohm").contains(0.4)
    assert not NormalRange(op="gt", min=1, unit="Mohm").contains(1)


@pytest.mark.parametrize("bad", [
    {"op": "between", "min": 5, "unit": "ohm"},          # max missing
    {"op": "lt", "min": 1, "unit": "ohm"},               # wrong bound for lt
    {"op": "between", "min": 9, "max": 5, "unit": "ohm"},  # reversed
])
def test_normal_range_rejects_bad_bounds(bad):
    with pytest.raises(ValidationError):
        NormalRange(**bad)


def test_unknown_field_is_an_error():
    with pytest.raises(ValidationError):
        NormalRange(op="lt", max=1, unit="ohm", units="ohm")


def test_approved_needs_a_reviewer():
    with pytest.raises(ValidationError):
        Review(status="approved")
    assert Review(status="approved", reviewer="expert").status == "approved"


def test_high_voltage_note_needs_a_warning():
    with pytest.raises(ValidationError):
        Safety(level="high_voltage")


def test_procedure_steps_must_link_to_real_steps():
    step = {"id": "1", "check": "resistance", "if_abnormal": "part faulty", "if_normal": "9",
            "measure": {"from": {"label": "connector pin 1", "pin": "1"}, "to": {"label": "ground"}},
            "normal": {"op": "lt", "max": 1, "unit": "ohm"}}
    body = {"procedure_id": "p", "vehicle": {"make": "X", "model": "Y", "year": 2020}, "code": "P0000",
            "meaning": "m", "steps": [step],
            "source": {"kind": "oem_procedure", "file": "x.pdf", "licensed": True}}
    with pytest.raises(ValidationError, match="unknown step"):
        Procedure(**body)
    step["if_normal"] = None
    assert Procedure(**body).steps[0].measure.from_point.pin == "1"


CASES = sorted((REPO / "seed" / "cases").glob("*.json"))
NOTES = sorted((REPO / "seed" / "expert_notes").glob("*.json"))
SCENARIOS = sorted((REPO / "eval" / "scenarios").glob("*.json"))


def test_seed_files_exist():
    assert len(CASES) >= 1 and len(NOTES) >= 3 and len(SCENARIOS) >= 1


@pytest.mark.parametrize("path", CASES, ids=lambda p: p.name)
def test_case_files_are_valid(path):
    case = Case(**load(path))
    assert not case.source.licensed, "licensed text must not be committed"
    assert all(fc.code == fc.code.strip() for fc in case.fault_codes)


@pytest.mark.parametrize("path", NOTES, ids=lambda p: p.name)
def test_expert_notes_are_valid(path):
    note = ExpertNote(**load(path))
    assert any(c.origin == Origin.EXPERT_STATED for c in note.claims), "a note needs the expert's own words"
    assert note.review.status == "draft" or note.review.reviewer


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.name)
def test_scenarios_are_valid(path):
    scn = Scenario(**load(path))
    assert set(scn.expected) == {"case_held_out", "case_in_kb"}
    for turn in scn.turns:
        for att in turn.attachments:
            assert (REPO / att).exists(), f"missing attachment {att}"
    if scn.related_case_id:
        ids = {load(p)["case_id"] for p in CASES}
        assert scn.related_case_id in ids


def test_no_seed_file_is_marked_approved_yet():
    """Nothing here has been reviewed by the client's expert. This test must be changed on purpose."""
    for path in CASES + NOTES + SCENARIOS:
        assert load(path)["review"]["status"] == "draft", path.name

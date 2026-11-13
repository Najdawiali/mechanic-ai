"""The P0010 reference record, the procedure walk, and the scenarios built on them.

The reference record was written by hand from the PDF. The scenario expectations were typed
separately. This file checks that the two agree, by walking each scenario through the record.
"""

import json
from pathlib import Path

import pytest

from app.engine.procedure_walk import advance, first_step
from app.engine.readings import Reading
from app.kb.schemas import Procedure
from eval.scenario_schema import Scenario, StepCheck

REPO = Path(__file__).resolve().parents[1]
GOLDEN = REPO / "eval" / "golden" / "procedures"
P0010 = Procedure(**json.loads((GOLDEN / "acura-integra-2025-l15ca-p0010.json").read_text(encoding="utf-8")))
PROCEDURES = {P0010.procedure_id: P0010}
SCENARIOS = [
    Scenario(**json.loads(p.read_text(encoding="utf-8")))
    for p in sorted((REPO / "eval" / "scenarios").glob("*.json"))
]
GUIDED = [s for s in SCENARIOS if s.related_procedure_id]


def test_reference_record_matches_the_pdf_structure():
    assert P0010.code == "P0010" and P0010.vehicle.engine_code == "L15CA"
    assert [s.id for s in P0010.steps] == ["1", "2", "3", "4"]
    pins = [(s.measure.from_point.pin, s.measure.to_point.pin, s.measure.to_point.ground) for s in P0010.steps]
    assert pins == [("1", "2", False), ("2", None, True), ("1", "64", False), ("64", None, True)]
    assert (P0010.steps[0].normal.min, P0010.steps[0].normal.max, P0010.steps[0].normal.unit) == (7.14, 7.90, "ohm")
    assert P0010.steps[3].normal.unit == "Mohm" and P0010.steps[3].normal.min == 1
    assert "SCS" in P0010.steps[1].prepare[0]  # the notice before the PCM connector is disconnected
    assert P0010.source.licensed and P0010.review.status == "draft"


def test_pins_fit_their_connectors():
    """A pin on a 2P connector is 1 or 2; on an 80P, 1 to 80. The check the ingestion will apply."""
    import re
    for step in P0010.steps:
        for point in (step.measure.from_point, step.measure.to_point):
            if point.ground:
                continue
            count = int(re.search(r"\((\d+)P\)", point.connector).group(1))
            assert 1 <= int(point.pin) <= count, (step.id, point.label)


@pytest.mark.parametrize("readings, conclusion, decided_by", [
    ([15], "There is a problem with VTC oil control solenoid valve A.", "1"),
    ([7.5, 50], "There is a problem in the circuit (PG open wire inspection).", "2"),
    ([7.5, 0.3, 120], "There is a problem in the circuit (VTC(IN) open wire inspection).", "3"),
    ([7.5, 0.3, 0.4, 200], "There is a problem in the circuit (VTC(IN) shorted to ground inspection).", "4"),
    ([7.5, 0.3, 0.4, None], "There is a problem with the PCM.", None),  # None = the meter showed OL
])
def test_every_branch_of_p0010(readings, conclusion, decided_by):
    step = first_step(P0010)
    outcome = None
    for value in readings:
        reading = Reading(over_limit=True) if value is None else Reading(value, "ohm")
        outcome = advance(P0010, step.id, reading)
        if outcome.step is not None:
            step = outcome.step
    assert outcome.conclusion == conclusion and outcome.decided_by_step == decided_by


def _assert_step_matches(check: StepCheck, procedure: Procedure) -> None:
    step = next(s for s in procedure.steps if s.id == check.step_id)
    actual = {(p.pin, p.ground) for p in (step.measure.from_point, step.measure.to_point)}
    assert {(c.pin, c.ground) for c in check.test_points} == actual
    for c in check.test_points:
        if c.connector_contains:
            point = next(p for p in (step.measure.from_point, step.measure.to_point) if p.pin == c.pin and not p.ground)
            assert c.connector_contains in point.connector
    assert check.normal == step.normal  # value fidelity: the scenario expects the source's numbers exactly


def test_there_are_nine_guided_scenarios():
    assert len(GUIDED) == 9
    assert sum(s.group == "procedure" for s in GUIDED) == 5 and sum(s.group == "language" for s in GUIDED) == 4


@pytest.mark.parametrize("scenario", GUIDED, ids=lambda s: s.scenario_id)
def test_scenario_agrees_with_the_reference_record(scenario: Scenario):
    procedure = PROCEDURES[scenario.related_procedure_id]
    assert scenario.codes == [procedure.code] and scenario.vehicle == procedure.vehicle

    # Turn 1 is the opening message: the reply must be the first step.
    opening = scenario.turns[0].expect["standard"]
    assert opening.reply_type == "step" and opening.step.step_id == first_step(procedure).id
    _assert_step_matches(opening.step, procedure)

    step = first_step(procedure)
    outcome = None
    for turn in scenario.turns[1:]:
        assert turn.reading is not None, "every later turn reports a reading"
        r = turn.reading
        outcome = advance(procedure, step.id, Reading(r.value, r.unit, r.over_limit))
        check = turn.expect["standard"]
        if outcome.step is not None:
            assert check.reply_type == "step" and check.step.step_id == outcome.step.id
            _assert_step_matches(check.step, procedure)
            for word in check.warnings_must_contain:  # a required warning must exist in the source step
                assert any(word in line for line in outcome.step.prepare + outcome.step.notices)
            step = outcome.step
        else:
            assert check.reply_type == "conclusion" and check.label == "verified"
            for word in check.conclusion_contains:
                assert word.lower() in outcome.conclusion.lower()
    assert outcome is not None and outcome.conclusion == scenario.expected["standard"].final_cause

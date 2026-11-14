"""Checks worked out from a wiring diagram's stored routes: no model, no database."""

import pytest

from app.engine.circuit_plan import BASIS, plan_route, plans_for_pin
from app.engine.procedure_walk import walk
from app.engine.readings import parse_reading


def route(*hops, complete=True):
    made = [dict(zip(("kind", "name", "pin", "detail"), (*hop, None, None, None)[:4])) for hop in hops]
    return {"hops": made, "text": " ".join(str(h["name"]) for h in made), "complete": complete}


SIGNAL = route(("pin", "VSA UNIT", "31"), ("wire", "RED"), ("connector", "C224", "8"), ("wire", "PPL/GRY"), ("connector", "C229", "4"),
               ("wire", None), ("pin", "LEFT REAR WHEEL SPEED SENSOR", "1"))
GROUND = route(("pin", "VSA UNIT", "3"), ("wire", "BLK"), ("ground", "G020"))
FEED = route(("pin", "VSA UNIT", "9"), ("wire", "BLU"), ("junction_connector", "J/C C007", "11"), ("junction_connector", "J/C C007", "12"),
             ("wire", "BLU"), ("pin", "UNDER-DASH FUSE/RELAY BOX", "A24", "FUSE 28 10A"))
ELSEWHERE = route(("pin", "VSA UNIT", "13"), ("wire", "PNK"), ("system", "COMPUTER DATA LINES SYSTEM"))


def test_a_wire_to_another_component_gets_an_open_check_then_a_short_check():
    plan = plan_route("VSA UNIT", "31", SIGNAL)
    assert (plan.role, plan.goes_to) == ("to_component", "LEFT REAR WHEEL SPEED SENSOR pin 1")
    first, second = plan.steps
    assert (first.measure.from_point.label, first.measure.to_point.label) == (
        "VSA UNIT pin 31 (RED wire)", "LEFT REAR WHEEL SPEED SENSOR pin 1 (wire colour not printed)")
    assert (first.normal.op, first.normal.max, first.normal.unit) == ("lt", 1, "ohm")
    assert second.measure.to_point.ground and (second.normal.op, second.normal.min, second.normal.unit) == ("gt", 1, "Mohm")
    # the connectors along the wire are where to look, in the order the wire passes them
    assert "connector C224 pin 8, connector C229 pin 4" in first.if_abnormal
    assert "Ignition OFF" in first.prepare and "LEFT REAR WHEEL SPEED SENSOR connector: disconnected" in first.prepare
    assert plan.basis == BASIS and "not this manufacturer's" in plan.basis


@pytest.mark.parametrize(("readings", "verdict"), [
    (["0.4 ohm", "OL"], "is sound: no open circuit and no short to ground"),
    (["35 ohm"], "open circuit or high resistance"),
    (["0.4 ohm", "200 ohm"], "is shorted to ground"),
])
def test_readings_are_judged_in_code_by_the_same_walker_as_a_procedure(readings, verdict):
    plan = plan_route("VSA UNIT", "31", SIGNAL)
    step, outcome = plan.steps[0], None
    for said in readings:
        outcome = walk(plan.steps, step.id, parse_reading(said), plan.when_all_normal)
        step = outcome.step or step
    assert verdict in outcome.conclusion


def test_a_wire_to_a_ground_point_gets_one_check_and_names_the_point():
    plan = plan_route("VSA UNIT", "3", GROUND)
    (step,) = plan.steps
    assert (plan.role, plan.goes_to) == ("ground", "ground G020") and step.measure.to_point.ground
    assert "ground point G020" in step.if_abnormal and step.normal.max == 1


def test_a_wire_from_a_fuse_is_checked_with_the_fuse_out_and_never_for_a_short():
    plan = plan_route("VSA UNIT", "9", FEED)
    assert (plan.role, plan.goes_to) == ("feed", "FUSE 28 10A at UNDER-DASH FUSE/RELAY BOX pin A24")
    (step,) = plan.steps  # the same fuse may feed other units, so a reading to ground would prove nothing
    assert any("FUSE 28 10A removed" in line and "not blown" in line for line in step.prepare)
    assert "connector J/C C007 pin 11, connector J/C C007 pin 12" in step.if_abnormal
    assert "supply wire" in plan.not_covered and "blown fuse" in plan.not_covered
    assert "short to ground was not checked" in plan.when_all_normal
    # seen from the fuse box, the same wire is the same feed
    back = route(("pin", "UNDER-DASH FUSE/RELAY BOX", "A24", "FUSE 28 10A"), ("wire", "BLU"), ("pin", "VSA UNIT", "9"))
    seen = plan_route("UNDER-DASH FUSE/RELAY BOX", "A24", back)
    assert seen.role == "feed" and len(seen.steps) == 1 and any("FUSE 28 10A removed" in line for line in seen.steps[0].prepare)


def test_no_short_check_for_a_wire_that_a_junction_connector_joins_to_others():
    joined = route(("pin", "GAUGE CONTROL MODULE", "7"), ("wire", "GRY"), ("junction_connector", "J/C C008", "3"),
                   ("junction_connector", "J/C C008", "4"), ("wire", "GRY"), ("pin", "VSA UNIT", "20"))
    plan = plan_route("GAUGE CONTROL MODULE", "7", joined)
    assert len(plan.steps) == 1 and plan.steps[0].if_normal is None and "junction connector J/C C008" in plan.not_covered


def test_a_wire_with_a_branch_is_tested_for_a_short_only_with_every_end_unplugged():
    to_sensor = route(("pin", "PCM", "A5"), ("wire", "YEL"), ("pin", "MAP SENSOR", "1"))
    to_second = route(("pin", "PCM", "A5"), ("wire", "YEL"), ("wire", "YEL/BLK"), ("pin", "TP SENSOR", "3"))
    first, second = plans_for_pin({"component": "PCM", "pin": "A5", "version": None, "routes": [to_sensor, to_second]})
    assert "TP SENSOR connector: disconnected" in first.steps[1].prepare and "TP SENSOR connector: disconnected" not in first.steps[0].prepare
    assert "MAP SENSOR connector: disconnected" in second.steps[1].prepare
    # a branch that ends at a ground point cannot be unplugged: the reading to ground would always be low
    to_ground = route(("pin", "PCM", "A5"), ("wire", "YEL"), ("wire", "BLK"), ("ground", "G101"))
    first, grounded = plans_for_pin({"component": "PCM", "pin": "A5", "version": None, "routes": [to_sensor, to_ground]})
    assert len(first.steps) == 1 and "it also runs to ground G101" in first.not_covered and grounded.role == "ground"


def test_routes_that_part_at_a_version_brace_are_not_branches_of_each_other():
    with_cvt = route(("pin", "VSA UNIT", "31"), ("wire", "RED"), ("version", "2 = CVT"), ("wire", "RED"), ("pin", "LEFT REAR WHEEL SPEED SENSOR", "2"))
    without = route(("pin", "VSA UNIT", "31"), ("wire", "RED"), ("version", "1 = EXCEPT CVT"), ("wire", "RED"), ("ground", "G5"))
    cvt, other = plans_for_pin({"component": "VSA UNIT", "pin": "31", "version": None, "routes": [with_cvt, without]})
    assert len(cvt.steps) == 2 and cvt.not_covered is None and cvt.applies_to == ("2 = CVT",) and other.applies_to == ("1 = EXCEPT CVT",)


def test_a_connector_marked_for_another_version_is_named_as_something_to_confirm():
    drawn = route(("pin", "RIGHT FRONT WHEEL SPEED SENSOR", "1"), ("wire", "GRN"), ("version", "2 = CVT"), ("wire", None),
                  ("connector", "C303 [1 = EXCEPT CVT]", "2"), ("wire", "GRN"), ("pin", "VSA UNIT", "29"))
    plan = plan_route("RIGHT FRONT WHEEL SPEED SENSOR", "1", drawn, version="2 = CVT")
    assert plan.applies_to == ("2 = CVT",)
    assert plan.cautions == ('The diagram marks connector C303 with "1 = EXCEPT CVT". Confirm on the car that it is fitted.',)


def test_a_wire_that_leaves_for_another_system_is_not_given_a_made_up_check():
    plan = plan_route("VSA UNIT", "13", ELSEWHERE)
    assert plan.role == "to_system" and plan.steps == [] and "COMPUTER DATA LINES SYSTEM" in plan.cannot_check


def test_a_route_the_reader_could_not_finish_or_a_far_pin_with_no_number_gets_no_check():
    unfinished = plan_route("VSA UNIT", "19", route(("pin", "VSA UNIT", "19"), ("wire", "PPL"), ("open", None), complete=False))
    no_number = plan_route("VSA UNIT", "33", route(("pin", "VSA UNIT", "33"), ("wire", "BRN"), ("pin", "LEFT FRONT WHEEL SPEED SENSOR", None)))
    assert unfinished.steps == [] and no_number.steps == [] and "no pin number" in no_number.cannot_check


def test_a_route_drawn_for_one_version_says_which():
    versioned = route(("pin", "PCM", "A49"), ("wire", "BRN"), ("version", "1.5L TURBO"), ("wire", "BRN"), ("pin", "BRAKE PEDAL POSITION SWITCH", "2"))
    assert plan_route("PCM", "A49", versioned).applies_to == ("1.5L TURBO",)
    stored = {"component": "BRAKE PEDAL POSITION SWITCH", "pin": "1", "version": "1.5L TURBO",
              "routes": [route(("pin", "BRAKE PEDAL POSITION SWITCH", "1"), ("wire", "BRN"), ("ground", "G023"))]}
    (plan,) = plans_for_pin(stored)
    assert plan.applies_to == ("1.5L TURBO",) and plan.role == "ground"


def test_a_wire_drawn_dashed_is_fitted_on_some_versions_only_and_the_plan_says_so():
    dashed = route(("pin", "BODY CONTROL MODULE", "B12"), ("wire", "TAN", None, "dashed"), ("pin", "RAIN SENSOR", "2"))
    plan = plan_route("BODY CONTROL MODULE", "B12", dashed)
    assert len(plan.steps) == 2 and any("dashed" in caution and "Confirm on the car" in caution for caution in plan.cautions)

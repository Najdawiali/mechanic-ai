"""English text built in code from records. No model touches these facts.

Everything a mechanic acts on (a test point, a normal range, a conclusion, a pin, a wire colour)
is rendered here, word for word from the record. The wording step may translate it, and the
validator then checks that no number was changed, dropped or added.
"""

from __future__ import annotations

import re

from app.engine.readings import Reading
from app.engine.reply import ExpectedInput, StepView
from app.engine.state import Walk
from app.kb.schemas import Case, ExpertNote, NormalRange, Procedure, Step, TestPoint, Vehicle

_STATE_LINE = re.compile(r"^[^:]{2,80}:\s*(?:dis)?connected\b|^[^:]{2,80}:\s*(?:on|off|lock|acc|start|ready|removed|installed)\b|"
                         r"^\s*(?:IG|ignition)\b.{0,20}\b(?:on|off)\b", re.I)
_QUANTITY = {"ohm": "resistance", "kohm": "resistance", "Mohm": "resistance", "V": "voltage", "mV": "voltage",
             "A": "current", "mA": "current"}


def number(value: float) -> str:
    return f"{value:g}"


SIGN = {"lt": "<", "le": "≤", "gt": ">", "ge": "≥"}


def normal_text(normal: NormalRange) -> str:
    """The normal value with its comparison as a sign ("< 1 Ω", "> 1 MΩ", "7.14 – 7.90 Ω").

    A sign reads the same in every language and the translation check keeps it, where a word
    ("less than") could be dropped. `as_printed` holds only the manual's digits and unit, never the
    comparison, so the sign is always added here (first real run: "Normal: 1 Ω" for "less than 1 Ω").
    """
    if normal.as_printed:
        value = normal.as_printed.strip()
    elif normal.op == "between":
        value = f"{number(normal.min)} – {number(normal.max)} {normal.unit}"
    else:
        value = f"{number(normal.max if normal.op in ('lt', 'le') else normal.min)} {normal.unit}"
    text = value if normal.op == "between" else f"{SIGN[normal.op]} {value.lstrip('<>≤≥ ')}"
    if normal.approximate:
        text = f"about {text}"
    return f"{text} {normal.condition}" if normal.condition else text


def point_text(point: TestPoint) -> str:
    """A test point from its fields, never from its label alone: a model-written label may leave out the pin
    (first real run: "VTC oil control solenoid valve A <-> VTC oil control solenoid valve A")."""
    if point.ground:
        return "body ground" if "body" in point.label.lower() else "ground"
    where = point.connector or point.label
    side = f", {point.terminal_side} terminal" if point.terminal_side else ""
    return f"{where}, pin {point.pin}{side}"


def reading_text(reading: Reading) -> str:
    if reading.over_limit:
        return "OL (over the meter's range)"
    return f"{number(reading.value)} {reading.unit}" if reading.unit else number(reading.value)


def vehicle_text(vehicle: Vehicle) -> str:
    parts = [str(vehicle.year) if vehicle.year else None, vehicle.make, vehicle.model, vehicle.engine_code,
             vehicle.powertrain.value if vehicle.powertrain else None]
    return " ".join(p for p in parts if p)


def is_instruction(line: str) -> bool:
    """A preparation line that is something to do ("jump the SCS line with the HDS"), not a state."""
    return not _STATE_LINE.search(line)


def linear(steps: list[Step]) -> bool:
    return all(s.if_normal == (steps[i + 1].id if i + 1 < len(steps) else None) for i, s in enumerate(steps))


def step_view(walk: Walk, step: Step) -> StepView:
    expected = None
    if step.normal and step.normal.unit in _QUANTITY:
        expected = ExpectedInput(kind=_QUANTITY[step.normal.unit], unit=step.normal.unit)
    measure = f"{point_text(step.measure.from_point)} <-> {point_text(step.measure.to_point)}" if step.measure else None
    return StepView(step_id=step.id, number=len(walk.done) + 1, of=len(walk.steps) if linear(walk.steps) else None,
                    instruction=step.check, measure=measure,
                    test_points=[step.measure.from_point, step.measure.to_point] if step.measure else [],
                    conditions=list(step.prepare), normal=step.normal,
                    normal_text=normal_text(step.normal) if step.normal else None, expected_input=expected,
                    requires_tool=step.requires_tool, basis=walk.basis)


def step_text(view: StepView, notices: list[str] = ()) -> str:
    lines = [f"Step {view.number}{f' of {view.of}' if view.of else ''}: {view.instruction}."]
    if view.conditions:
        lines.append("Before measuring:")
        lines += [f"- {c}" for c in view.conditions]
    lines += [f"NOTICE: {n}" for n in notices]
    if view.requires_tool:
        lines.append(f"This step needs the {view.requires_tool}.")
    if view.measure:
        lines.append(f"Measure: {view.measure}")
    if view.normal_text:
        lines.append(f"Normal: {view.normal_text}")
    lines.append("Send me the reading.")
    return "\n".join(lines)


def procedure_intro(procedure: Procedure) -> str:
    return (f"{procedure.code}: {procedure.meaning}.\n"
            f"This is the manufacturer's troubleshooting procedure for the {vehicle_text(procedure.vehicle)}. "
            "I will give you one step at a time; send me each reading.")


def circuit_intro(walk: Walk) -> str:
    return (f"Checks for {walk.title}, worked out from the wiring diagram. "
            "The limits are general electrical practice, not the manufacturer's values. "
            "I will give you one step at a time; send me each reading.")


def verdict_line(step: Step, reading: Reading, verdict: str, *, from_words: bool, number: int) -> str:
    said = f"I read your reading as {reading_text(reading)}. " if from_words else ""
    word = "normal" if verdict == "normal" else "outside the normal range"
    return f"{said}Step {number} reading: {reading_text(reading)}, {word} ({normal_text(step.normal)})."


# -- records given to the model as facts -------------------------------------------------------------
def case_facts(case: Case) -> dict:
    cars = [f"{c.year or 'year not recorded'} {case.make} {c.model or ''}".strip() for c in case.repaired_cars]
    if not cars:
        years = (f"{case.year_from}" if case.year_from == case.year_to else f"{case.year_from}-{case.year_to}") \
            if case.year_from else "years not recorded"
        cars = [f"{case.make} {'/'.join(case.models)} ({years})"]
    return {
        "car(s) repaired": cars, "fault codes": [f.code or f.text_on_tool for f in case.fault_codes],
        "symptoms": case.symptoms, "findings": case.findings, "diagnosis": case.diagnosis, "root cause": case.root_cause,
        "repair steps": case.repair_steps, "parts replaced": [p.part for p in case.parts_replaced],
        "parts NOT replaced (ruled out)": case.parts_not_replaced, "technician's notes": case.notes,
        "outcome": case.outcome.value,
    }


def note_facts(note: ExpertNote) -> dict:
    return {
        "title": note.title, "applies to": note.applies_to.model_dump(exclude_none=True),
        "fault codes": note.fault_codes, "symptoms": note.symptoms,
        "claims": [c.text for c in note.claims if c.origin != "ai_added"],
        "thresholds": [f"{t.quantity}: {normal_text(t.range)} = {t.meaning}" for t in note.thresholds if t.origin != "ai_added"],
        "safety": note.safety.warnings,
    }


def case_fallback(case: Case) -> str:
    facts = case_facts(case)
    lines = [f"An earlier repair in the workshop ({'; '.join(facts['car(s) repaired'])}), not a manufacturer procedure:",
             f"- Diagnosis: {case.diagnosis}"]
    if case.findings:
        lines.append(f"- Checks done: {'; '.join(case.findings[:3])}")
    if case.repair_steps:
        lines.append(f"- Repair: {'; '.join(case.repair_steps[:3])}")
    lines.append(f"- Outcome: {case.outcome.value.replace('_', ' ')}")
    lines.append("Do the checks that show this applies to your car before replacing any part.")
    return "\n".join(lines)

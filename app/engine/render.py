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
from app.kb.schemas import Case, ExpertNote, NormalRange, Procedure, Step, Vehicle

_STATE_LINE = re.compile(r"^[^:]{2,80}:\s*(?:dis)?connected\b|^[^:]{2,80}:\s*(?:on|off|lock|acc|start|ready|removed|installed)\b|"
                         r"^\s*(?:IG|ignition)\b.{0,20}\b(?:on|off)\b", re.I)
_QUANTITY = {"ohm": "resistance", "kohm": "resistance", "Mohm": "resistance", "V": "voltage", "mV": "voltage",
             "A": "current", "mA": "current"}


def number(value: float) -> str:
    return f"{value:g}"


def normal_text(normal: NormalRange) -> str:
    if normal.as_printed:
        text = normal.as_printed
    else:
        unit = normal.unit
        text = {
            "between": lambda: f"{number(normal.min)} to {number(normal.max)} {unit}",
            "lt": lambda: f"less than {number(normal.max)} {unit}",
            "le": lambda: f"{number(normal.max)} {unit} or less",
            "gt": lambda: f"more than {number(normal.min)} {unit}",
            "ge": lambda: f"{number(normal.min)} {unit} or more",
        }[normal.op]()
    if normal.approximate and "about" not in text.lower():
        text = f"about {text}"
    return f"{text} {normal.condition}" if normal.condition else text


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
    measure = f"{step.measure.from_point.label} <-> {step.measure.to_point.label}" if step.measure else None
    return StepView(step_id=step.id, number=len(walk.done) + 1, of=len(walk.steps) if linear(walk.steps) else None,
                    instruction=step.check, measure=measure,
                    test_points=[step.measure.from_point, step.measure.to_point] if step.measure else [],
                    conditions=list(step.prepare), normal=step.normal,
                    normal_text=normal_text(step.normal) if step.normal else None, expected_input=expected,
                    requires_tool=step.requires_tool, basis=walk.basis)


def step_text(view: StepView) -> str:
    lines = [f"Step {view.number}{f' of {view.of}' if view.of else ''}: {view.instruction}."]
    if view.conditions:
        lines.append("Before measuring:")
        lines += [f"- {c}" for c in view.conditions]
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


def verdict_line(step: Step, reading: Reading, verdict: str, *, from_words: bool) -> str:
    said = f"I read your reading as {reading_text(reading)}. " if from_words else ""
    word = "normal" if verdict == "normal" else "outside the normal range"
    return f"{said}Step reading {reading_text(reading)}: {word} ({normal_text(step.normal)})."


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

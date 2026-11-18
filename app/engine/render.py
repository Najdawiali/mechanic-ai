"""Text built in code from records. No model touches these facts.

Everything a mechanic acts on (a test point, a normal range, a conclusion, a pin, a wire colour)
is rendered here, word for word from the record. Steps and conclusions are lines of labels written
here in each language (app/engine/wording.py, "label_*") and texts from the record (`Tr`); only the
record texts go to the model for translation, and each is checked for its numbers, pins and codes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.engine.readings import Reading
from app.engine.reply import ExpectedInput, StepView
from app.engine.state import Walk
from app.engine.wording import fixed
from app.kb.schemas import (
    Case,
    ExpertNote,
    NormalRange,
    Procedure,
    Step,
    TestPoint,
    Vehicle,
)
from app.lang.script import Language

_STATE_LINE = re.compile(r"^[^:]{2,80}:\s*(?:dis)?connected\b|^[^:]{2,80}:\s*(?:on|off|lock|acc|start|ready|removed|installed)\b|"
                         r"^\s*(?:IG|ignition)\b.{0,20}\b(?:on|off)\b", re.I)
_QUANTITY = {"ohm": "resistance", "kohm": "resistance", "Mohm": "resistance", "V": "voltage", "mV": "voltage",
             "A": "current", "mA": "current"}


def number(value: float) -> str:
    return f"{value:g}"


# -- a reply as lines of fixed labels (written here per language) and texts from the record to translate ------
@dataclass(frozen=True)
class Tr:
    """English text from a record that the model translates. `required`: a sentence, which must come back in the
    language's script; otherwise (a check name, "IG Mode OFF") it may stay as written."""

    english: str
    required: bool = False


Line = tuple  # of str (final text) and Tr (text to translate)
STOP = {"en": ".", "ar": ".", "ur": "۔"}


def english_text(lines: list[Line]) -> str:
    return "\n".join("".join(p.english if isinstance(p, Tr) else p for p in line) for line in lines)


def to_translate(lines: list[Line]) -> list[Tr]:
    return list(dict.fromkeys(p for line in lines for p in line if isinstance(p, Tr)))


def assemble(lines: list[Line], translated: dict[Tr, str]) -> str:
    return "\n".join("".join(translated.get(p, p.english) if isinstance(p, Tr) else p for p in line) for line in lines)


SIGN = {"lt": "<", "le": "≤", "gt": ">", "ge": "≥"}


def normal_text(normal: NormalRange) -> str:
    """The normal value with its comparison as a sign ("< 1 Ω", "> 1 MΩ", "7.14 – 7.90 Ω").

    A sign reads the same in every language and the translation check keeps it, where a word
    ("less than") could be dropped. `as_printed` holds only the manual's digits and unit, never the
    comparison, so the sign is always added here (first real run: "Normal: 1 Ω" for "less than 1 Ω").
    """
    text = normal_value(normal)
    return f"{text} {normal.condition}" if normal.condition else text


def normal_parts(normal: NormalRange, language: Language) -> list:
    """The normal value as parts of a line: the value in code, the manual's condition ("at room temperature") to translate."""
    value = normal_value(normal, language)
    return [value, " ", Tr(normal.condition)] if normal.condition else [value]


def normal_value(normal: NormalRange, language: Language = "en") -> str:
    if normal.as_printed:
        value = normal.as_printed.strip()
    elif normal.op == "between":
        value = f"{number(normal.min)} – {number(normal.max)} {normal.unit}"
    else:
        value = f"{number(normal.max if normal.op in ('lt', 'le') else normal.min)} {normal.unit}"
    text = value if normal.op == "between" else f"{SIGN[normal.op]} {value.lstrip('<>≤≥ ')}"
    return fixed("label_about", language, value=text) if normal.approximate else text


def point_text(point: TestPoint) -> str:
    """A test point from its fields, never from its label alone: a model-written label may leave out the pin
    (first real run: "VTC oil control solenoid valve A <-> VTC oil control solenoid valve A")."""
    if point.ground:
        return "body ground" if "body" in point.label.lower() else "ground"
    where = point.connector or point.label
    side = f", {point.terminal_side} terminal" if point.terminal_side else ""
    return f"{where}, pin {point.pin}{side}"


_DISPLAY = {("resistance", 1.0): "ohm", ("resistance", 1e3): "kohm", ("resistance", 1e6): "Mohm",
            ("voltage", 1.0): "V", ("voltage", 1e-3): "mV", ("current", 1.0): "A", ("current", 1e-3): "mA"}


def display_unit(unit: str) -> str:
    """The unit in its English form ("أوم" -> "ohm"), so English text stays English for the translation check."""
    from app.engine.readings import ReadingError, _unit

    try:
        return _DISPLAY.get(_unit(unit), unit)
    except ReadingError:
        return unit


def reading_text(reading: Reading, language: Language = "en") -> str:
    if reading.over_limit:
        return fixed("label_over_limit", language)
    return f"{number(reading.value)} {display_unit(reading.unit)}" if reading.unit else number(reading.value)


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


def step_lines(view: StepView, notices: list[str] = (), language: Language = "en") -> list[Line]:
    head = fixed("label_step_of" if view.of else "label_step", language, n=view.number, of=view.of)
    lines: list[Line] = [(f"{head} ", Tr(view.instruction), STOP[language])]
    if view.conditions:
        lines.append((fixed("label_before", language),))
        lines += [("- ", Tr(c, required=is_instruction(c))) for c in view.conditions]  # "IG Mode OFF" may stay; a sentence may not
    lines += [(f"{fixed('label_notice', language)} ", Tr(n, required=True)) for n in notices]
    if view.requires_tool:
        lines.append((fixed("label_needs_tool", language, tool=view.requires_tool),))
    if view.measure:
        lines.append((f"{fixed('label_measure', language)} {view.measure}",))
    if view.normal is not None:
        lines.append((f"{fixed('label_normal', language)} ", *normal_parts(view.normal, language)))
    elif view.normal_text:
        lines.append((f"{fixed('label_normal', language)} {view.normal_text}",))
    lines.append((fixed("label_send_reading", language),))
    return lines


def step_text(view: StepView, notices: list[str] = ()) -> str:
    return english_text(step_lines(view, notices))


def procedure_intro_lines(procedure: Procedure, language: Language = "en") -> list[Line]:
    return [(f"{procedure.code}: {procedure.meaning}.",),
            (fixed("label_procedure_intro", language, vehicle=vehicle_text(procedure.vehicle)),)]


def procedure_intro(procedure: Procedure) -> str:
    return english_text(procedure_intro_lines(procedure))


def circuit_intro_lines(walk: Walk, language: Language = "en") -> list[Line]:
    part, pin, end = (walk.extra.get(k) for k in ("part", "pin", "end"))
    title = fixed("label_circuit_title", language, part=part, pin=pin, end=end) if part and pin and end else walk.title
    return [(fixed("label_circuit_intro", language, title=title),)]


def circuit_intro(walk: Walk) -> str:
    return english_text(circuit_intro_lines(walk))


def verdict_parts(step: Step, reading: Reading, verdict: str, *, from_words: bool, number: int,
                  language: Language = "en") -> Line:
    shown = reading_text(reading, language)
    said = f"{fixed('label_read_as', language, reading=shown)} " if from_words else ""
    word = fixed("label_verdict_normal" if verdict == "normal" else "label_verdict_abnormal", language)
    head = fixed("label_reading", language, n=number, reading=shown, verdict=word)
    return (f"{said}{head} (", *normal_parts(step.normal, language), f"){STOP[language]}")


def verdict_line(step: Step, reading: Reading, verdict: str, *, from_words: bool, number: int) -> str:
    return english_text([verdict_parts(step, reading, verdict, from_words=from_words, number=number)])


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

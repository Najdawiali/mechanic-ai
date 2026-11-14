"""A DTC troubleshooting PDF -> one Procedure record.

Procedures are written differently by every manufacturer, so nothing here depends on a layout.
The work is split so that each part does what it is reliable at:

  1. code     takes the text out of the PDF, leaving a marker where each embedded image sits
  2. model    looks at each image on its own and says what it shows (the small images are where
              manuals print the two test points of a measurement)
  3. model    reads the text, with the image readings put back in place, and writes the steps
  4. code     checks the result against the PDF itself, with no answer key: every number must
              be in the PDF's text, every pin must come from an image and fit its connector,
              every measurement image must be used once

The record is always stored as a draft. Anything a check found is written into the record for
the reviewer, starting with "CHECK FAILED".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.ingest.diagram_extractor import vehicle_from
from app.ingest.extractors import Extracted, SourceDocument
from app.ingest.jobs import PermanentError
from app.ingest.pdf_text import PageImage, PdfText, read_pdf_text
from app.kb.schemas import Procedure, Review, Source
from app.llm.gateway import image_part_from_bytes, user_message_with_image

CODE = re.compile(r"\b([PCBU][0-9A-F]{4}(?:[0-9A-F]{2})?)\b")
NUMBER = re.compile(r"\d+(?:\.\d+)?")
PIN_COUNT = re.compile(r"\((\d+)\s*P\)")  # "(80P)" in a connector's name
UNIT_AS_PRINTED = {"ohm": ("Ω", "ohm"), "kohm": ("kΩ", "kohm"), "Mohm": ("MΩ", "Mohm"), "V": ("V",), "mV": ("mV",), "A": ("A",)}

IMAGE_PROMPT = """This image was cut out of a vehicle service manual. Say what it shows.
Answer with JSON only:
{"kind": "measurement" | "notice" | "connector_view" | "other",
 "from": string or null, "to": string or null,
 "quantity": "resistance" | "voltage" | "continuity" | "current" | null,
 "text": any words printed in the image, or null}

"measurement" is a small symbol of two test points joined through a meter, such as (1)-ohm-(2).
For a measurement, "from" and "to" are the two test points exactly as drawn: a pin number such
as "1" or "64", or "ground" for a ground symbol. If a number cannot be read with certainty use
null. Never guess a number."""

WRITE_PROMPT = """You convert the text of a manufacturer's DTC troubleshooting procedure into JSON.
The text is given below. Where the manual printed an image, the text has a note in square
brackets saying what the image shows. A note such as [measurement 4: from pin 1 to pin 64,
resistance] gives the two test points of the measurement in the table row beside it.

Rules:
- Copy values, units, connector names and sentences exactly as printed. Do not add anything
  from your own knowledge. If something is not in the text, leave it out or use null.
- A pin number may only come from a [measurement ...] note. Never invent a pin.
- One step per measurement, in the order of the manual. Use each [measurement ...] note once.
- Decide which connector each test point belongs to from the table's connector headings. A pin
  cannot be higher than its connector's pin count: "(2P)" has pins 1 and 2, "(80P)" pins 1 to 80.
- "check" is the name of the inspection in that table row, exactly as printed, for example
  "PG open wire inspection". "section" is the heading the table stands under.
- "if_abnormal" is the result sentence the manual prints for a reading that is not normal, and
  nothing else: no brackets, no inspection name.
- "if_normal" is the id of the next step, or null for the last step.
- "elimination_conclusion" is the manual's result sentence when every measurement is normal: only
  the sentence that names what is faulty, not the reasoning before it.
- "If abnormal, there is a problem with X." gives the conclusion "There is a problem with X."

Answer with JSON only, in this shape:
{"code": "P0000", "meaning": "...", "overview": string or null, "warnings": [], "related_docs": [],
 "confirmation": ["..."], "tool_functions": [{"tool": "...", "path": "..."}],
 "steps": [{"id": "1", "section": "...", "check": "...", "prepare": ["..."], "notices": ["..."],
            "measure": {"from": {"label": "...", "connector": "...", "pin": "1", "ground": false, "terminal_side": "male" | "female" | null},
                        "to": {"label": "body ground", "connector": null, "pin": null, "ground": true, "terminal_side": null}},
            "normal": {"op": "between" | "lt" | "le" | "gt" | "ge", "min": number or null, "max": number or null,
                       "unit": "ohm" | "kohm" | "Mohm" | "V" | "mV" | "A", "approximate": false, "condition": string or null},
            "if_abnormal": "...", "if_normal": "2" or null, "requires_tool": string or null}],
 "elimination_conclusion": string or null}

Units: write Ω as "ohm", kΩ as "kohm", MΩ as "Mohm". "less than" is "lt", "more than" is "gt",
a range is "between"."""


@dataclass(frozen=True)
class ImageReading:
    number: int
    kind: str  # measurement | notice | connector_view | other | unreadable
    start: str | None = None  # "1", "64", "ground"
    end: str | None = None
    quantity: str | None = None
    text: str | None = None

    def note(self) -> str:
        if self.kind == "measurement":
            point = lambda p: "an unreadable point" if p is None else ("ground" if p.lower() == "ground" else f"pin {p}")  # noqa: E731
            return f"[measurement {self.number}: from {point(self.start)} to {point(self.end)}, {self.quantity or 'quantity not shown'}]"
        if self.kind == "notice":
            return "[NOTICE]"
        if self.kind == "connector_view":
            return "[drawing of a connector]"
        return "[image]"  # words a model read from a picture are not passed on as if they were the manual's text


def _json(text: str) -> Any:
    """The JSON object in a model reply, with or without a code fence around it."""
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("the model's reply holds no JSON object")
    return json.loads(match.group(0))


def read_image(gateway, image: PageImage) -> ImageReading:
    message = user_message_with_image(image_part_from_bytes(image.png), IMAGE_PROMPT)
    try:
        data = _json(gateway.chat([message], role="vision", json_mode=True, max_tokens=300).text)
    except (ValueError, json.JSONDecodeError):
        return ImageReading(image.number, "unreadable")
    kind = data.get("kind") if data.get("kind") in ("measurement", "notice", "connector_view", "other") else "other"
    clean = lambda v: None if v in (None, "", "null") else str(v).strip().strip("()")  # noqa: E731
    return ImageReading(image.number, kind, clean(data.get("from")), clean(data.get("to")), clean(data.get("quantity")), clean(data.get("text")))


def text_with_readings(pdf: PdfText, readings: list[ImageReading]) -> str:
    text = pdf.text
    for reading in readings:
        text = text.replace(f"[image {reading.number}]", reading.note())
    return text


def write_procedure(gateway, text: str, *, attempts: int = 2) -> dict:
    """The model's JSON for the procedure. An answer that does not fit the record is sent back
    once with the reason; a second failure fails the job, to be tried again later."""
    messages = [{"role": "system", "content": WRITE_PROMPT}, {"role": "user", "content": text}]
    problem = "no attempt made"
    for _ in range(attempts):
        reply = gateway.chat(messages, role="reasoning", json_mode=True, max_tokens=4000).text
        try:
            data = _json(reply)
            _as_procedure(data, placeholder=True)
            return data
        except (ValueError, json.JSONDecodeError, ValidationError) as exc:
            problem = str(exc)[:1500]
            messages = messages[:2] + [{"role": "assistant", "content": reply},
                                       {"role": "user", "content": f"That answer was not accepted: {problem}\nSend the corrected JSON only."}]
    raise RuntimeError(f"the model did not produce a valid procedure: {problem}")


def _as_procedure(data: dict, *, placeholder: bool = False, **fixed) -> Procedure:
    fields = {k: v for k, v in data.items() if k != "code"}
    if placeholder:
        fixed = {"procedure_id": "check", "vehicle": {"make": "x", "model": "x"}, "code": str(data.get("code") or "?"),
                 "source": {"kind": "licensed_procedure", "file": "check"}}
    return Procedure(**fields, **fixed)


def _sentence(text: str | None) -> str | None:
    """A conclusion as a sentence: one space between words, a capital first, a full stop last."""
    if not text or not text.strip():
        return None
    text = " ".join(text.split())
    return text[0].upper() + text[1:] + ("" if text.endswith((".", "!", "?")) else ".")


TRAILING_BRACKET = re.compile(r"\.?\s*\((?:[^()]|\([^()]*\))*\)\s*\.?\s*$")


def name_conclusions(data: dict) -> None:
    """Put the conclusions of a model's answer into their stored form, in code.

    A manual often prints one result sentence under a table of several measurements ("there is a
    problem in the circuit"). So that each step's conclusion says which fault it means, the name
    of the step's inspection is added in brackets when a sentence is shared. This is done here,
    from the step's own `check` name, and never left to the model's wording.
    """
    data["elimination_conclusion"] = _sentence(data.get("elimination_conclusion"))
    steps = [step for step in data.get("steps", []) if isinstance(step, dict) and step.get("if_abnormal")]
    for step in steps:
        step["if_abnormal"] = _sentence(TRAILING_BRACKET.sub("", str(step["if_abnormal"])))
    shared = {sentence for sentence in (step["if_abnormal"] for step in steps)
              if sum(other["if_abnormal"] == sentence for other in steps) > 1}
    for step in steps:
        name = " ".join(str(step.get("check") or "").split())
        if step["if_abnormal"] in shared and name:
            step["if_abnormal"] = f"{step['if_abnormal'].rstrip('.!?')} ({name})."


ABOUT = r"(?:about|around|approx(?:imately|\.)?)"
IGNITION = re.compile(r"\b(?:IG|ignition)(?:\s+(?:mode|switch|key))?\s*[:\-]?\s*"
                      r"(?:OFF|ON(?:\s*\(II\))?|ACC(?:ESSORY)?|LOCK|START)(?![A-Za-z])", re.IGNORECASE)


def _digits_as_printed(value: Any, pdf_text: str) -> str | None:
    """How the manual writes this number ("7.90"), when it writes it one way only."""
    try:
        wanted = float(value)
    except (TypeError, ValueError):
        return None
    forms = {n for n in NUMBER.findall(pdf_text.replace(",", "")) if float(n) == wanted}
    return forms.pop() if len(forms) == 1 else None


def _without_brackets(text: str) -> str:
    text = text.strip(" ,;:-")
    while text.startswith("(") and text.endswith(")") and text.count("(") == text.count(")") == 1:
        text = text[1:-1].strip(" ,;:-")
    return text


def settle_normals(data: dict, pdf_text: str) -> None:
    """Decide in code the parts of a normal value that a model fills in unevenly.

    - "about": true only when the manual prints "about" (or "approx.") in front of the value.
    - the values with the manual's own digits (7.90, not 7.9), for the reviewer and for display.
    - the ignition state is a preparation, not a condition of the value: it is moved to `prepare`,
      so `condition` holds only what the manual prints with the value ("at room temperature").
    """
    for step in data.get("steps") or []:
        normal = step.get("normal") if isinstance(step, dict) else None
        if not isinstance(normal, dict):
            continue
        digits = [_digits_as_printed(normal[k], pdf_text) for k in ("min", "max") if normal.get(k) is not None]
        symbol = next((s for s in UNIT_AS_PRINTED.get(str(normal.get("unit")), ()) if s in pdf_text), None)
        if digits and digits[0]:
            normal["approximate"] = bool(re.search(rf"\b{ABOUT}\s+{re.escape(digits[0])}(?![\d.])", pdf_text, re.IGNORECASE))
        normal["as_printed"] = f"{' – '.join(digits)} {symbol}" if digits and all(digits) and symbol else None

        condition = normal.get("condition")
        if not isinstance(condition, str):
            continue
        ignition = IGNITION.search(condition)
        if ignition:
            prepare = step["prepare"] if isinstance(step.get("prepare"), list) else []
            if not any(IGNITION.search(str(line)) for line in prepare):
                prepare.append(" ".join(ignition.group(0).split()))
            step["prepare"] = prepare
            condition = condition[:ignition.start()] + condition[ignition.end():]
        normal["condition"] = _without_brackets(condition) or None


ARROW = re.compile(r"\s*[>＞›»→]\s*")


def _path_key(text: str) -> str:
    """A menu path without its spacing and with one kind of arrow, so "A ＞ B" equals "A > B"."""
    return "".join(ARROW.sub(">", text).lower().split())


def settle_tools(data: dict, pdf_text: str) -> None:
    """Name the tool of each tool function from the manual's own heading, and mark the steps that need it.

    A manual prints a menu path under a heading such as "HDS Function". A model sometimes gives an
    item of the path as the tool. The heading printed directly above the path decides. A step
    needs a tool when its own preparations or notices name one of the procedure's tools.
    """
    lines = [line.strip(" \t■•－-–") for line in pdf_text.splitlines()]
    functions = [f for f in data.get("tool_functions") or [] if isinstance(f, dict) and f.get("path")]
    for function in functions:
        key = _path_key(str(function["path"]))
        at = next((i for i, line in enumerate(lines) if line and _path_key(line) == key), None)
        above = next((line for line in reversed(lines[:at]) if line), "") if at is not None else ""
        heading = re.fullmatch(r"(.+?)\s+functions?", above, re.IGNORECASE)
        if heading:
            function["tool"] = heading.group(1)
    tools = [str(f["tool"]) for f in functions if f.get("tool")]
    for step in data.get("steps") or []:
        if not isinstance(step, dict) or step.get("requires_tool"):
            continue
        told = " ".join(str(line) for line in [*(step.get("prepare") or []), *(step.get("notices") or [])])
        step["requires_tool"] = next((tool for tool in tools if re.search(rf"(?<!\w){re.escape(tool)}(?!\w)", told)), None)


def _printed(sentence: str, pdf_text: str) -> bool:
    """Is this sentence in the PDF? Compared without spaces, capitals or the final full stop, so a
    line break in the PDF does not matter and a changed letter does. An inspection name added in
    brackets at the end (see the writing rules) is not part of the printed sentence."""
    squeeze = lambda t: "".join(t.lower().split())  # noqa: E731
    core = sentence.split(" (")[0] if sentence.rstrip(".").endswith(")") else sentence
    # An image printed beside a sentence (a NOTICE badge) leaves its marker inside the sentence's text.
    return squeeze(core.rstrip(".!?")) in squeeze(re.sub(r"\[image \d+\]", " ", pdf_text))


# -- checks that need no answer key ------------------------------------------------------
def check_procedure(procedure: Procedure, pdf_text: str, readings: list[ImageReading]) -> list[str]:
    """Everything in the record that the PDF itself does not back up. Empty = all checks passed."""
    problems: list[str] = []
    printed_numbers = {float(n) for n in NUMBER.findall(pdf_text.replace(",", ""))}
    measurements = [r for r in readings if r.kind == "measurement"]
    drawn_points = [{(r.start or "").lower(), (r.end or "").lower()} for r in measurements]
    used: list[int] = []

    if procedure.code.upper() not in pdf_text.upper():
        problems.append(f"the code {procedure.code} is not in the PDF's text")
    for unread in (r for r in readings if r.kind == "unreadable" or (r.kind == "measurement" and None in (r.start, r.end))):
        problems.append(f"image {unread.number} could not be read with certainty")

    if procedure.elimination_conclusion and not _printed(procedure.elimination_conclusion, pdf_text):
        problems.append(f"the all-normal conclusion is not printed in the PDF as written: {procedure.elimination_conclusion!r}")
    for function in procedure.tool_functions:
        if _path_key(function.path) not in _path_key(pdf_text):
            problems.append(f"the tool menu path is not printed in the PDF as written: {function.path!r}")
        if function.tool not in pdf_text:
            problems.append(f"the tool name {function.tool!r} is not printed in the PDF")
        elif _path_key(function.tool) in _path_key(function.path).split(">"):
            problems.append(f"the tool name {function.tool!r} is an item of its own menu path, not the tool")
    for step in procedure.steps:
        where = f"step {step.id}"
        for notice in step.notices:
            if not _printed(notice, pdf_text):
                problems.append(f"{where}: the notice is not printed in the PDF as written: {notice!r}")
        if step.if_abnormal and not _printed(step.if_abnormal, pdf_text):
            problems.append(f"{where}: the conclusion is not printed in the PDF as written: {step.if_abnormal!r}")
        if (step.measure is None) != (step.normal is None):
            problems.append(f"{where}: a measurement and its normal value must come together")
        if step.normal is not None:
            for value in (step.normal.min, step.normal.max):
                if value is not None and float(value) not in printed_numbers:
                    problems.append(f"{where}: the value {value:g} is not printed in the PDF")
            if not any(symbol in pdf_text for symbol in UNIT_AS_PRINTED.get(step.normal.unit, (step.normal.unit,))):
                problems.append(f"{where}: the unit {step.normal.unit} is not printed in the PDF")
            if step.normal.condition and not _printed(step.normal.condition, pdf_text):
                problems.append(f"{where}: the condition of the normal value is not printed in the PDF as written: {step.normal.condition!r}")
        if step.measure is None:
            continue
        points = set()
        for point in (step.measure.from_point, step.measure.to_point):
            points.add("ground" if point.ground else (point.pin or "").lower())
            if point.ground:
                continue
            count = PIN_COUNT.search(point.connector or "")
            if count and point.pin and point.pin.isdigit() and not 1 <= int(point.pin) <= int(count.group(1)):
                problems.append(f"{where}: pin {point.pin} does not exist on {point.connector}")
            if point.connector and point.connector not in pdf_text and " ".join(point.connector.split()) not in " ".join(pdf_text.split()):
                problems.append(f"{where}: the connector name {point.connector!r} is not printed in the PDF")
        if points in drawn_points:
            used.append(drawn_points.index(points))
        else:
            shown = ", ".join(sorted(p or "?" for p in points))
            problems.append(f"{where}: no image in the PDF shows a measurement between {shown}")

    if len(used) != len(set(used)) or len(used) != len(measurements):
        problems.append(f"the PDF has {len(measurements)} measurement image(s) and the record has "
                        f"{sum(s.measure is not None for s in procedure.steps)} measurement step(s): each image must be used once")
    return problems


# -- the extractor -------------------------------------------------------------------------
def extract_procedure(document: SourceDocument, gateway=None) -> list[Extracted]:
    return [extract_with_readings(document, gateway)[0]]


def extract_with_readings(document: SourceDocument, gateway=None) -> tuple[Extracted, list[ImageReading]]:
    """The record, and what was read from each image (kept for scoring and for the reviewer)."""
    try:
        pdf = read_pdf_text(document.data)
    except Exception as exc:  # noqa: BLE001  not a PDF, or damaged
        raise PermanentError(f"the file could not be read as a PDF: {exc}") from None
    if len(pdf.text) < 200:
        raise PermanentError("the PDF has almost no text: a scanned procedure cannot be read by this extractor")
    if gateway is None:
        from app.llm.gateway import LLMGateway

        gateway = LLMGateway()

    vehicle, questions = vehicle_from(pdf.header[0] if pdf.header else "", document.declared)
    readings = [read_image(gateway, image) for image in pdf.images]
    data = write_procedure(gateway, text_with_readings(pdf, readings))
    name_conclusions(data)
    settle_normals(data, pdf.text)
    settle_tools(data, pdf.text)

    # The code is taken from the PDF's own heading; the model's answer must agree with it.
    in_header = [m.group(1) for line in pdf.header for m in CODE.finditer(line)]
    code = in_header[0] if in_header else str(data.get("code") or "").strip()
    if not code:
        raise PermanentError("no fault code found in the procedure")
    if str(data.get("code") or "").upper() != code.upper():
        questions.append(f"CHECK FAILED: the PDF heading gives the code {code}; the model wrote {data.get('code')!r}")

    slug = "-".join(re.sub(r"[^a-z0-9]+", "-", str(p).lower()).strip("-") for p in
                    (vehicle.make, vehicle.model, vehicle.year, vehicle.engine_code) if p)
    procedure = _as_procedure(
        data, procedure_id=f"proc:{slug}:{code}", vehicle=vehicle, code=code,
        source=Source(kind="licensed_procedure", file=document.original_name, licensed=True),
    )
    questions += [f"CHECK FAILED: {problem}" for problem in check_procedure(procedure, pdf.text, readings)]
    questions.append("Pins were read from images by a model. Compare each step with the PDF before approving.")
    procedure = procedure.model_copy(update={"review": Review(open_questions=questions)})
    return Extracted(procedure, "model"), readings

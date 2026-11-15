"""Reading the mechanic's message into fields. The model proposes, code decides.

What the model is trusted with: what the mechanic wants (intent), which of the car's diagram
components he means (picked from a closed list), which answer he gave to a question (picked from
the options), and an English rewrite of the message for search and for the session summary.

What code checks or decides:
  * fault codes: found by pattern in the message itself; a code the model returns is kept only if
    it is written in the message (so a code can never be invented here)
  * a reading: taken by the reading parser when the message is a bare reading, or a sentence with
    one number and its unit ("طلعت القراءة ١٥ أوم"); a reading the model returns is kept only if its
    number is written in the message, or the message has no digits at all ("fifteen ohms"), and
    then the reply says back which value it understood
  * component names and answers: kept only if they are on the list the model was given; and a
    component is used only when a real word of its name, or a known other name for it (ABS for
    VSA), is in the mechanic's message. Otherwise it is "unsure" and the assistant asks. First real
    run: asked about the "VTC solenoid", the model picked the "VSA MODULATOR-CONTROL UNIT", and
    its pins were listed as verified.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Literal

from app.engine.model import ModelClient
from app.engine.readings import Reading, normalise_digits, parse_reading, reading_in_sentence
from app.engine.state import SessionState
from app.kb.keys import code_stem, norm_key
from app.lang.glossary import find_terms

Intent = Literal["diagnose", "reading", "finding", "fix_confirmed", "wiring_question", "send_diagram", "check_wire",
                 "spec_question", "answer", "other"]
INTENTS: tuple[str, ...] = Intent.__args__  # type: ignore[attr-defined]

# P0010, P0C4400, B148754, P1A7309; and Tesla alerts: TAS_a211_noRideHeightCalib, BMS_A123_SW_Internal_Isolation
DTC = re.compile(r"(?<![A-Za-z0-9])[PBCU][0-9][0-9A-F]{3}(?:[0-9A-F]{2})?(?![A-Za-z0-9])", re.I)
ALERT = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9]{1,9}[_ ][AaWwFf]\d{3}(?:_[A-Za-z0-9]+)*(?![A-Za-z0-9])")
_DIGIT = re.compile(r"\d")


def codes_in(text: str) -> list[str]:
    """Fault codes written in the text, in order, each once (compared by stem)."""
    found, stems = [], set()
    for match in sorted([*DTC.finditer(text), *ALERT.finditer(text)], key=lambda m: m.start()):
        code = match.group(0)
        code = code.upper() if DTC.fullmatch(code) else code
        stem = code_stem(code)
        if stem not in stems:
            stems.add(stem)
            found.append(code)
    return found


@dataclass
class Intake:
    intent: Intent
    english: str
    codes: list[str] = field(default_factory=list)
    reading: Reading | None = None
    reading_from_words: bool = False  # the value was written in words: say back what was understood
    components: list[str] = field(default_factory=list)
    power_and_ground: bool = False
    pins_only: bool = False  # asks only which pins of the first component serve the other, not the whole route
    unsure: list[str] = field(default_factory=list)  # components the model picked that the message does not name
    answer: str | None = None  # one of the pending question's options
    fix: str | None = None  # the repair that fixed it, in English, when intent is fix_confirmed
    terms: list[str] = field(default_factory=list)  # workshop words found by the glossary, "word = English"


SYSTEM = """You read one message from a car mechanic to a diagnostic assistant and return JSON.
The mechanic writes in English, Arabic (often Jordanian dialect) or Urdu, and mixes in English car words.
Fill the fields from the message only. Never add a code, a value or a part that the message does not contain.

intent, one of:
- diagnose: describes a fault, fault codes or symptoms, or asks what to do about them
- reading: gives a meter reading (a number with a unit, or OL) for the step he was given
- finding: says what he checked or found, without a meter reading for the current step; or a repair that did NOT fix it
- fix_confirmed: says a repair was done AND says the fault is gone (codes cleared, works normally, alert gone).
  A repair with no word about the result ("reconnected both connectors") is finding, not fix_confirmed.
- wiring_question: asks which pins, wires, colours, connectors, fuses or grounds connect parts
- send_diagram: asks to see or be sent the wiring diagram
- check_wire: asks how to test a wire or circuit
- spec_question: asks for a specific value, pin number or specification
- answer: answers a question the assistant asked (see "question asked")
- other: anything else
english: the whole message in plain English, keeping every part, symptom, code and value it mentions.
reading: the meter reading if intent is reading, else null. over_limit is true for "OL" or "infinite".
components: the diagram components the message is about, chosen ONLY from "components on this car's diagrams"; [] if none fit.
power_and_ground: true if he asks about power supply, fuses or ground of a component.
pins_only: true if he asks only which pins (and wire colours) of the first component serve the other component,
  not the whole wiring between them.
answer: if he answers the question asked, the matching option exactly as listed; else null.
result_words: if intent is fix_confirmed, the words of the message that say the fault is gone, copied exactly
  (e.g. "the car runs normally", "وراح العطل"); else null.
fix: if intent is fix_confirmed, a short English phrase naming the cause found and every repair done in this
  conversation that fixed it, using "earlier messages" too; name a procedure in words, not only by its code
  (e.g. "internal short in the inverter: inverter replaced"; "connectors re-seated and window regulator basic setting";
  "ride height calibration (PROC_TAS_X_CALIBRATE-RIDE-HEIGHT)"); else null."""


def _schema(components: list[str], options: list[str]) -> dict:
    nullable_choice = {"type": ["string", "null"]} if not options else {"type": ["string", "null"], "enum": [*options, None]}
    component_item = {"type": "string"} if not components else {"type": "string", "enum": components}
    return {
        "type": "object", "additionalProperties": False,
        "required": ["intent", "english", "codes", "reading", "components", "power_and_ground", "pins_only", "answer",
                     "result_words", "fix"],
        "properties": {
            "intent": {"type": "string", "enum": list(INTENTS)},
            "english": {"type": "string"},
            "codes": {"type": "array", "items": {"type": "string"}},
            "reading": {"anyOf": [{"type": "null"}, {
                "type": "object", "additionalProperties": False, "required": ["value", "unit", "over_limit"],
                "properties": {"value": {"type": ["number", "null"]}, "unit": {"type": ["string", "null"]},
                               "over_limit": {"type": "boolean"}}}]},
            "components": {"type": "array", "items": component_item},
            "power_and_ground": {"type": "boolean"},
            "pins_only": {"type": "boolean"},
            "answer": nullable_choice,
            "result_words": {"type": ["string", "null"]},
            "fix": {"type": ["string", "null"]},
        },
    }


# Words in a component name that do not tell one part from another
GENERIC = {"UNIT", "MODULE", "CONTROL", "CONTROLLER", "SENSOR", "SWITCH", "ASSEMBLY", "ASSY", "BOX", "RELAY", "FUSE",
           "CONNECTOR", "LEFT", "RIGHT", "FRONT", "REAR", "UPPER", "LOWER", "MAIN", "SUB", "ELECTRIC", "ELECTRICAL",
           "SYSTEM", "AND", "OF", "THE", "WITH", "NO", "SIDE", "DASH", "HOOD", "UNDER"}
# Other names mechanics use for the same part (general auto-electric usage; extend with the client's expert)
SYNONYMS = [{"ABS", "VSA", "ESC", "ESP", "VSC", "DSC", "MODULATOR"}, {"ECM", "PCM", "ECU", "ENGINE"},
            {"TCM", "TRANSMISSION", "GEARBOX"}, {"BCM", "BODY", "MICU"}, {"WSS", "WHEEL"}, {"EPB", "PARKING"},
            {"SRS", "AIRBAG"}, {"TPMS", "TIRE", "TYRE"}, {"HVAC", "CLIMATE"}, {"METER", "GAUGE", "CLUSTER", "INSTRUMENT"}]


def _words(text: str) -> set[str]:
    words = set(re.findall(r"[A-Z0-9]+", text.upper()))
    return words | {w[:-1] for w in words if w.endswith("S") and len(w) > 3}


def named(component: str, said: set[str]) -> bool:
    """Does the message name this component: a word of its name that tells it apart, or another name for it?"""
    for word in _words(component) - GENERIC:
        if word in said or any(word in group and group & said for group in SYNONYMS):
            return True
    return False


# Words that say a fault is gone. A confirmed fix needs one, inside the words the model quotes from the message.
# Missing a word here is a safe failure: the assistant keeps asking instead of closing the case.
RESULT_WORDS = re.compile(
    r"normal|\bworks?\b|working|cleared|\bgone\b|fixed|resolved|solved|\bok\b|okay|no (?:more )?(?:fault|code|alert)s?|disappeared|"
    r"طبيعي|طبيعية|راح|راحت|راحوا|انحل|انحلت|نحلت|زبط|زبطت|ضبط|ضبطت|اشتغل|اشتغلت|بدون خلل|بدون عطل|ما في عطل|انمسح|انمسحت|اختفى|تمام|"
    r"ٹھیک|درست|ختم|چل رہی|چل رہا|نارمل", re.I)


def _squash(text: str) -> str:
    return re.sub(r"[\W_]+", "", text or "").lower()


def english_of(out: dict, text: str) -> str:
    return out.get("english") if isinstance(out.get("english"), str) else text


def _number_written(value: float, text: str) -> bool:
    for found in re.findall(r"\d+(?:\.\d+)?", normalise_digits(text).replace("٫", ".")):
        if float(found) == value:
            return True
    return False


def read_message(model: ModelClient, state: SessionState, text: str, *, components: list[str],
                 report_text: str | None = None) -> Intake:
    terms = [f"{t.written} = {t.english}" for t in find_terms(text)]
    options = state.pending.question.options if state.pending else []
    asked = state.history[-2].text if len(state.history) >= 2 and state.history[-2].role == "assistant" else None
    user = json.dumps({
        "vehicle": state.vehicle.model_dump(exclude_none=True),
        "step being worked on": (next(s.check for s in state.walk.steps if s.id == state.walk.step_id) if state.walk else None),
        "question asked": {"text": asked, "options": options} if state.pending else None,
        "earlier messages": [t.english or t.text for t in state.history if t.role == "mechanic"][-6:],
        "components on this car's diagrams": components,
        "workshop words in the message": terms,
        "scan report attached": report_text,
        "message": text,
    }, ensure_ascii=False)
    out = model.json("intake", role="fast", system=SYSTEM, user=user, schema=_schema(components, options))

    intent = out.get("intent") if out.get("intent") in INTENTS else "other"
    said_gone = out.get("result_words") if isinstance(out.get("result_words"), str) else ""
    if intent == "fix_confirmed" and not (_squash(said_gone) and _squash(said_gone) in _squash(text)
                                          and RESULT_WORDS.search(said_gone)):
        intent = "finding"  # second full run: "Reconnected and secured both." was taken as a confirmed fix
    in_text = codes_in(text)
    written = norm_key(text) or ""
    codes = list(in_text)
    for code in out.get("codes") or []:
        if isinstance(code, str) and code.strip() and code_stem(code) in written and \
                code_stem(code) not in {code_stem(c) for c in codes}:
            codes.append(code.strip())

    reading, from_words = parse_reading(text), False
    if reading is None and state.walk and intent not in ("fix_confirmed", "send_diagram", "wiring_question", "check_wire"):
        reading = reading_in_sentence(text)  # code wins over the model's value when it can read it
    raw = out.get("reading")
    if reading is None and isinstance(raw, dict) and intent in ("reading", "answer", "finding"):
        value = raw.get("value")
        if raw.get("over_limit"):
            reading = Reading(over_limit=True)
        elif isinstance(value, (int, float)):
            if _number_written(float(value), text):
                reading = Reading(float(value), raw.get("unit") or None)
            elif not _DIGIT.search(normalise_digits(text)):
                reading, from_words = Reading(float(value), raw.get("unit") or None), True
    if reading is not None and intent not in ("reading", "answer"):
        intent = "reading" if state.walk else intent

    allowed = {norm_key(c): c for c in components}
    chosen, unsure = [], []
    said = _words(" ".join([text, english_of(out, text), *(t.split(" = ", 1)[1] for t in terms)]))
    for name in out.get("components") or []:
        key = norm_key(name) if isinstance(name, str) else None
        if key in allowed and allowed[key] not in chosen + unsure:
            (chosen if named(allowed[key], said) else unsure).append(allowed[key])
    answer = out.get("answer") if out.get("answer") in options else None
    fix = out.get("fix") if intent == "fix_confirmed" and isinstance(out.get("fix"), str) else None
    english = out.get("english") if isinstance(out.get("english"), str) and out.get("english").strip() else text
    return Intake(intent=intent, english=english, codes=codes, reading=reading, reading_from_words=from_words,
                  components=chosen, unsure=unsure, power_and_ground=bool(out.get("power_and_ground")),
                  pins_only=bool(out.get("pins_only")), answer=answer, fix=fix,
                  terms=terms)

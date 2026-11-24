"""The troubleshooting engine end to end: real database, scripted model.

The scripted model stands in for the provider. Each test says what the model returns, so what is
tested is what the engine does with it: which records it serves, what code decides, and what the
validator lets through.
"""

import json
from pathlib import Path

import pytest

from app.engine.engine import Engine
from app.engine.model import ScriptedModel
from app.engine.report import Attachment
from app.engine.state import SessionState
from app.kb.schemas import Case, Procedure, WiringDiagram
from app.kb.store import APPROVED_AND_DRAFT, KnowledgeStore

REPO = Path(__file__).resolve().parents[1]
ACURA = {"make": "Acura", "model": "Integra", "year": 2025, "engine_code": "L15CA", "powertrain": "ICE"}
BYD = {"make": "BYD", "model": "Qin Plus DM-i", "year": 2024, "powertrain": "PHEV"}
VSA = "VSA MODULATOR-CONTROL UNIT"
LF = "LEFT FRONT WHEEL SPEED SENSOR"
EXCEPT_CVT, CVT = "1 = EXCEPT 1.5L TURBO CVT INTEGRA", "2 = 1.5L TURBO CVT INTEGRA"


def hops(*items):
    return [dict(zip(("kind", "name", "pin", "detail"), item)) for item in items]


def route(*items, complete=True):
    made = hops(*items)
    return {"hops": made, "text": " ".join(f"{h['name']}{' pin ' + h['pin'] if h.get('pin') else ''}" for h in made if h["name"]),
            "complete": complete}


def abs_diagram() -> WiringDiagram:
    """The verified ABS routes for these pins (eval/golden/diagrams/acura-integra-2025-abs.json), built by hand."""
    pins = [
        {"component": VSA, "pin": "1", "wire_colour": "WHT", "page": "1 of 3",
         "routes": [route(("pin", VSA, "1", None), ("wire", "WHT", None, None), ("pin", "UNDER-HOOD FUSE/RELAY BOX", "A11", "FUSE 5-5 40A"))]},
        {"component": VSA, "pin": "2", "wire_colour": "RED", "page": "1 of 3",
         "routes": [route(("pin", VSA, "2", None), ("wire", "RED", None, None), ("pin", "UNDER-HOOD FUSE/RELAY BOX", "A24", "FUSE 4-2 40A"))]},
        {"component": VSA, "pin": "3", "wire_colour": "BLK", "page": "1 of 3",
         "routes": [route(("pin", VSA, "3", None), ("wire", "BLK", None, None), ("ground", "G020", None, None))]},
        {"component": VSA, "pin": "33", "wire_colour": "BRN", "page": "1 of 3", "routes": [
            route(("pin", VSA, "33", None), ("wire", "BRN", None, None), ("connector", "C304", "2", None), ("version", EXCEPT_CVT, None, None),
                  ("wire", None, None, None), ("pin", LF, "2", None)),
            route(("pin", VSA, "33", None), ("wire", "BRN", None, None), ("version", CVT, None, None), ("wire", "BRN", None, None),
                  ("pin", LF, "1", None))]},
        {"component": VSA, "pin": "32", "wire_colour": "BLU", "page": "1 of 3", "routes": [
            route(("pin", VSA, "32", None), ("wire", "BLU", None, None), ("connector", "C304", "1", None), ("version", EXCEPT_CVT, None, None),
                  ("wire", None, None, None), ("pin", LF, "1", None)),
            route(("pin", VSA, "32", None), ("wire", "BLU", None, None), ("version", CVT, None, None), ("wire", "BLU", None, None),
                  ("pin", LF, "2", None))]},
    ]
    return WiringDiagram(
        diagram_key="diagram:acura-integra-2025:abs", vehicle=ACURA, system_path=["Brakes", "Anti-Lock Brakes"], title="Anti-Lock Brakes",
        sheets=[{"page": "1 of 3", "diagram_id": "910112", "image_key": "diagrams/910112.png"},
                {"page": "2 of 3", "diagram_id": "910113"}],
        pins=pins, verdict="traced", source={"kind": "licensed_diagram", "file": "abs.pdf", "licensed": True})


def p0010() -> Procedure:
    return Procedure(**json.loads((REPO / "eval/golden/procedures/acura-integra-2025-l15ca-p0010.json").read_text(encoding="utf-8")))


def byd_case() -> Case:
    return Case(**json.loads((REPO / "seed/cases/byd-qin-plus-dmi-2024-p2b5313-p2b5411.json").read_text(encoding="utf-8")))


@pytest.fixture
def kb(conn) -> KnowledgeStore:
    store = KnowledgeStore(conn)
    for record in (p0010(), abs_diagram(), byd_case()):
        saved = store.save(record, extracted_by="hand")
        store.set_review(saved.record_id, "approved", reviewer="test")
    conn.commit()
    return store


def intake(**fields):
    base = {"intent": "diagnose", "english": "", "codes": [], "reading": None, "components": [],
            "power_and_ground": False, "pins_only": False, "answer": None, "result_words": None, "fix": None}
    return base | fields


class Script:
    """A scripted model: intake answers by message, translation that keeps the text, compose answers in order."""

    def __init__(self, intakes=None, advise=None, general=None, report=None, translate=None, web=None):
        self.intakes = intakes or {}
        self.advise = list(advise or [])
        self.general = list(general or [])
        self.web = list(web or [])
        self.report = report
        self.translate = translate
        self.model = ScriptedModel({
            "intake": self._intake, "translate": self._translate,
            "advise": lambda *_: self.advise.pop(0) if self.advise else {"use": [], "message": ""},
            "general": lambda *_: self.general.pop(0) if self.general else {"message": "Start with the battery, the fuses and the grounds."},
            "web": lambda *_: self.web.pop(0) if self.web else {"use": [], "message": ""},
            "summary": lambda *_: {"message": "Session closed."}, "report": lambda *_: self.report})

    def _intake(self, system, user, schema):
        message = json.loads(user)["message"]
        return intake(english=message) | self.intakes.get(message, {})

    def _translate(self, system, user, schema):
        if self.translate:
            return self.translate(user)
        if "lines" in schema["properties"]:  # record texts between labels: echoed as they are
            return {"lines": json.loads(user)["lines"]}
        return {"message": user.split("\n\n(Your previous")[0]}

    def calls(self, purpose):
        return [t for t in self.model.trace if t.purpose == purpose]


def session(vehicle=ACURA, language="en") -> SessionState:
    return SessionState(vehicle=vehicle, language=language)


# -- the manufacturer procedure ---------------------------------------------------------------------------
def test_a_code_with_a_procedure_starts_the_walk_and_a_bare_reading_needs_no_model(kb):
    script = Script()
    engine, state = Engine(kb, script.model), session()
    first = engine.turn(state, "2025 Acura Integra 1.5L turbo, fault code P0010.").reply
    assert (first.type, first.tier, first.step.step_id, first.step.number, first.step.of) == ("step", "verified", "1", 1, 4)
    assert [p.pin for p in first.step.test_points] == ["1", "2"]
    assert (first.step.normal.min, first.step.normal.max, first.step.normal.unit) == (7.14, 7.9, "ohm")
    assert first.evidence[0].id == "proc:acura-integra-2025-l15ca:P0010" and "P0010" in first.message
    model_calls = len(script.model.trace)

    second = engine.turn(state, "15 ohm").reply
    assert len(script.model.trace) == model_calls  # judged in code: no model call
    assert second.type == "conclusion" and "VTC oil control solenoid valve A" in second.possible_causes[0].name
    assert "Step 1 reading: 15 ohm, outside the normal range (about 7.14 – 7.9" in second.message
    assert state.walk is None and state.readings[-1].verdict == "abnormal"


def test_the_arabic_walk_shows_the_scs_instruction_once_and_ends_at_the_failed_circuit(kb):
    engine, state = Engine(kb, Script().model), session(language="ar")
    engine.turn(state, "أكورا انتيجرا 2025 محرك 1.5 تيربو، طالع عندي كود P0010")
    second = engine.turn(state, "٧.٥ اوم").reply
    assert second.step.step_id == "2" and any("SCS" in w for w in second.warnings) and second.language == "ar"
    third = engine.turn(state, "٥٠ اوم").reply
    assert third.type == "conclusion" and "PG open wire" in third.possible_causes[0].name


def test_a_reading_written_in_words_is_said_back(kb):
    script = Script(intakes={"I'm getting fifteen ohms": {"intent": "reading", "reading": {"value": 15, "unit": "ohm", "over_limit": False}}})
    engine, state = Engine(kb, script.model), session()
    engine.turn(state, "fault code P0010")
    reply = engine.turn(state, "I'm getting fifteen ohms").reply
    assert reply.type == "conclusion" and "I read your reading as 15 ohm" in reply.message


def test_a_reading_the_model_invents_is_not_used(kb):
    script = Script(intakes={"it reads 7 point 5 I think, 8 ohm": {"intent": "reading", "reading": {"value": 7.5, "unit": "ohm", "over_limit": False}}})
    engine, state = Engine(kb, script.model), session()
    engine.turn(state, "fault code P0010")
    reply = engine.turn(state, "it reads 7 point 5 I think, 8 ohm").reply
    # 7.5 is not written in the message (8 is, and the parser takes only a clear reading): the step is shown again
    assert reply.type == "step" and reply.step.step_id == "1" and state.readings == []


def test_an_unknown_engine_is_asked_and_never_assumed(kb):
    engine, state = Engine(kb, Script(intakes={"yes it is": {"intent": "answer", "answer": "yes"}}).model), session(
        {"make": "Acura", "model": "Integra", "year": 2025})
    asked = engine.turn(state, "code P0010").reply
    assert asked.type == "question" and asked.question.kind == "engine" and "L15CA" in asked.message and asked.step is None
    started = engine.turn(state, "yes it is").reply
    assert started.type == "step" and state.vehicle.engine_code == "L15CA"


def test_an_unknown_year_is_asked_from_the_years_that_have_the_procedure(kb):
    engine, state = Engine(kb, Script().model), session({"make": "Acura", "model": "Integra", "engine_code": "L15CA"})
    asked = engine.turn(state, "code P0010").reply
    assert asked.question.kind == "year" and asked.question.options == ["2025"]
    assert engine.turn(state, "it's a 2025").reply.type == "step"


# -- no data: general knowledge, labelled; or nothing -----------------------------------------------------
def test_no_procedure_and_no_case_gives_labelled_general_guidance_with_no_borrowed_values(kb):
    script = Script(general=[{"message": "Check the VTC solenoid: it should read 7.14 to 7.90 ohm."},  # values from another code
                             {"message": "Check the VTC solenoid: it should read 7.14 to 7.90 ohm."}])
    engine, state = Engine(kb, script.model), session()
    reply = engine.turn(state, "2025 Integra 1.5 turbo, code P0341. What do I measure at the VTC solenoid?").reply
    assert reply.tier == "general" and "P0341" in reply.message and "7.14" not in reply.message
    assert not any(e.type == "procedure" for e in reply.evidence)
    then = engine.turn(state, "P0010 is stored as well.").reply
    assert then.type == "step" and then.tier == "verified" and "P0010" in then.message


def test_a_pin_question_with_no_diagram_is_refused_first_then_gets_the_general_method(kb):
    # comparison run 2026-11-24 (ABS-004, HYB-010): a bare refusal scored low; the refusal stays first, never a pin
    script = Script(intakes={"Which ECM pin is the rear oxygen sensor?": {"intent": "spec_question"}})
    engine, state = Engine(kb, script.model), session({"make": "Toyota", "model": "Camry", "year": 2018})
    reply = engine.turn(state, "Which ECM pin is the rear oxygen sensor?").reply
    assert reply.tier == "general" and reply.evidence == []
    assert reply.message.split("\n")[1].startswith("Sorry, there is no verified information") and "pin number, wire colour" in reply.message
    assert "not available: never give it" in json.loads(script.calls("general")[0].request["user"])["the exact value, pin or wire asked for"]
    invents = Script(intakes={"Which ECM pin is the rear oxygen sensor?": {"intent": "spec_question"}},
                     general=[{"message": "It is ECM pin 23. Can you reach it?"}] * 2)
    bare = Engine(kb, invents.model).turn(session({"make": "Toyota", "model": "Camry", "year": 2018}),
                                          "Which ECM pin is the rear oxygen sensor?").reply
    assert bare.tier == "none" and "23" not in bare.message  # the model's pin is refused: the refusal alone


def test_general_and_none_replies_are_logged_as_gaps(kb):
    script = Script(intakes={"Which ECM pin is the rear oxygen sensor?": {"intent": "spec_question"}})
    engine = Engine(kb, script.model)
    result = engine.turn(session({"make": "Toyota", "model": "Camry", "year": 2018}), "Which ECM pin is the rear oxygen sensor?")
    assert [(g.tier, g.question) for g in result.gaps] == [("general", "Which ECM pin is the rear oxygen sensor?")]


# -- safety ------------------------------------------------------------------------------------------------
def test_an_emergency_stops_the_flow_before_any_lookup_or_model(kb):
    script = Script()
    engine, state = Engine(kb, script.model), session(BYD, "ar")
    reply = engine.turn(state, "بعد حادث عليها P2B5313، وهسا في دخان وريحة شياط طالعة من جهة بطارية الهاي فولتج").reply
    assert reply.type == "emergency" and reply.evidence == [] and "911" in reply.message and script.model.trace == []
    assert "الهاي فولتج" in reply.message and reply.warnings  # high-voltage text; marked as a draft text


# -- workshop cases ---------------------------------------------------------------------------------------
def test_a_case_with_the_same_code_is_a_hint_and_its_message_passes_the_validator(kb):
    script = Script(advise=[
        {"use": ["case:byd-qin-plus-dmi-2024:p2b5313-p2b5411"], "message": "Replace pump 12 first."},  # 12: no source
        {"use": ["case:byd-qin-plus-dmi-2024:p2b5313-p2b5411"],
         "message": "An earlier repair on a 2024 BYD Qin Plus DM-i with P2B5313: check the hybrid system coolant pump before replacing parts. Did the codes return?"},
    ])
    engine, state = Engine(kb, script.model), session(BYD)
    reply = engine.turn(state, "BYD Qin Plus 2024 with P2B5313 and P2B5411, what do I do?").reply
    assert reply.tier == "verified" and [e.type for e in reply.evidence] == ["case"] and "12" not in reply.message
    assert len(script.calls("advise")) == 2  # the first message was refused and written again


def test_a_case_found_only_by_words_is_used_only_if_the_model_says_it_fits(kb):
    script = Script(advise=[{"use": [], "message": ""}], general=[{"message": "Check the cooling pump circuit and its fuse."}])
    engine, state = Engine(kb, script.model), session(BYD)
    reply = engine.turn(state, "the engine coolant pump makes noise and the thermostat warning comes on sometimes").reply
    assert reply.tier == "general" and not any(e.type == "case" for e in reply.evidence)


def test_a_confirmed_fix_is_a_conclusion_in_the_mechanics_words(kb):
    script = Script(intakes={"Replaced the inverter, car runs normally": {"intent": "fix_confirmed", "fix": "inverter replacement", "result_words": "car runs normally"}})
    engine, state = Engine(kb, script.model), session(BYD)
    reply = engine.turn(state, "Replaced the inverter, car runs normally").reply
    assert reply.type == "conclusion" and reply.possible_causes[0].name == "inverter replacement" and state.confirmed_fix


def test_drafts_are_served_only_when_the_engine_is_told_to(kb, conn):
    draft = Case(**(byd_case().model_dump(exclude={"review"}) | {"case_id": "case:byd-draft", "fault_codes": [{"code": "P0A0000"}]}))
    kb.save(draft, extracted_by="hand")
    conn.commit()
    script = Script(advise=[{"use": ["case:byd-draft"], "message": "An earlier repair on a BYD Qin Plus DM-i with P0A0000. Check the pump first?"}],
                    general=[{"message": "Start with the battery and fuses."}])
    approved_only = Engine(kb, script.model).turn(session(BYD), "code P0A0000").reply
    assert approved_only.tier == "general"
    with_drafts = Engine(kb, script.model, statuses=APPROVED_AND_DRAFT).turn(session(BYD), "code P0A0000").reply
    assert with_drafts.tier == "verified" and with_drafts.evidence[0].status == "draft"


# -- wiring -------------------------------------------------------------------------------------------------
def test_routes_drawn_for_two_versions_are_both_listed_and_the_mechanic_is_asked(kb):
    question = "I need the wiring from the VSA unit to the left front wheel speed sensor."
    script = Script(intakes={question: {"intent": "wiring_question", "components": [VSA, LF]},
                             "It's the CVT.": {"intent": "answer", "answer": "1.5L TURBO CVT INTEGRA"}})
    engine, state = Engine(kb, script.model), session()
    asked = engine.turn(state, question).reply
    assert asked.type == "question" and asked.question.options == ["EXCEPT 1.5L TURBO CVT INTEGRA", "1.5L TURBO CVT INTEGRA"]
    assert all(word in asked.message for word in ("33", "32", "BRN", "BLU", "CVT")) and asked.tier == "verified"
    answered = engine.turn(state, "It's the CVT.").reply
    assert answered.type == "conclusion" and "C304" not in answered.message and "LEFT FRONT WHEEL SPEED SENSOR pin 1" in answered.message


def test_power_and_ground_come_from_the_fuses_and_ground_points_and_the_page_is_sent_on_request(kb):
    question = "وحدة الـ ABS ما بتوصلها كهربا. أي فيوزات لازم أفحص ووين الأرضي تبعها؟"
    script = Script(intakes={question: {"intent": "wiring_question", "components": [VSA], "power_and_ground": True},
                             "ابعثلي المخطط": {"intent": "send_diagram"}})
    engine, state = Engine(kb, script.model), session(language="ar")
    reply = engine.turn(state, question).reply
    assert reply.type == "conclusion" and all(w in reply.message for w in ("5-5", "4-2", "G020"))
    assert reply.evidence[0].type == "diagram"
    sent = engine.turn(state, "ابعثلي المخطط").reply
    assert [d.id for d in sent.diagrams] == ["910112"] and sent.diagrams[0].image_key == "diagrams/910112.png"


def test_checking_a_ground_wire_from_the_diagram_is_a_walk_judged_in_code(kb):
    script = Script(intakes={"Which pin is the ground of the VSA unit?": {"intent": "wiring_question", "components": [VSA], "power_and_ground": True},
                             "How do I check it?": {"intent": "check_wire"},
                             "VSA MODULATOR-CONTROL UNIT pin 3": {"intent": "answer", "answer": f"{VSA} pin 3"}})
    engine, state = Engine(kb, script.model), session()
    engine.turn(state, "Which pin is the ground of the VSA unit?")
    asked = engine.turn(state, "How do I check it?").reply
    assert asked.question.kind == "component"
    step = engine.turn(state, "VSA MODULATOR-CONTROL UNIT pin 3").reply
    assert step.type == "step" and step.step.basis == "general_practice" and any("not this manufacturer's" in w for w in step.warnings)
    done = engine.turn(state, "35 ohm").reply
    assert done.type == "conclusion" and "G020" in done.message


# -- scan reports and translation -------------------------------------------------------------------------
def test_codes_read_from_a_report_are_listed_back_and_modules_without_codes_are_dropped(kb):
    script = Script(report={"vehicle": None, "modules": [{"module": "PDC", "codes": ["P2B5313", "P2B5411"]},
                                                         {"module": "FMCU", "codes": []}]},
                    advise=[{"use": ["case:byd-qin-plus-dmi-2024:p2b5313-p2b5411"],
                             "message": "An earlier repair on a 2024 BYD Qin Plus DM-i with P2B5313. Did you check the hybrid coolant pump?"}])
    engine, state = Engine(kb, script.model), session(BYD, "ar")
    reply = engine.turn(state, "byd qin plus 2024، شو الأعطال اللي بهاي الصورة؟",
                        [Attachment("scan.png", "image/png", b"\x89PNG fake")]).reply
    assert reply.report_codes == ["P2B5313", "P2B5411"] and "PDC" in reply.message and "FMCU" not in reply.message
    assert state.codes == ["P2B5313", "P2B5411"]


def test_a_translation_that_changes_a_number_is_replaced_by_the_english_text(kb):
    script = Script(translate=lambda user: {"message": "الخطوة 1: القراءة 7.4 أوم"})
    engine, state = Engine(kb, script.model), session(language="ar")
    reply = engine.turn(state, "أكورا انتيجرا 2025، طالع عندي كود P0010").reply
    assert "7.14" in reply.message and "7.4 " not in reply.message and "بالإنجليزية" in reply.message
    assert len(script.calls("translate")) == 2


# -- faults found in the first real-model run (2026-11-15) ---------------------------------------------------
def p0010_as_the_model_wrote_it() -> Procedure:
    """The model's record: test point labels without the pin, the SCS instruction as a notice."""
    data = json.loads((REPO / "eval/golden/procedures/acura-integra-2025-l15ca-p0010.json").read_text(encoding="utf-8"))
    for step in data["steps"]:
        for end in ("from", "to"):
            point = step["measure"][end]
            if not point.get("ground"):
                point["label"] = point["connector"].replace(" connector (2P)", "").replace(" (80P)", "")
        step["prepare"] = [p for p in step["prepare"] if "SCS" not in p]
        if step["id"] in ("2", "3", "4"):
            step["notices"] = ["Before disconnecting the PCM connector, jump the SCS line with the HDS, and wait more than 1 minute."] + step.get("notices", [])
    data["steps"][0]["normal"]["as_printed"] = "7.14 – 7.90 Ω"
    data["steps"][1]["normal"]["as_printed"] = "1 Ω"
    data["steps"][3]["normal"]["as_printed"] = "1 MΩ"
    return Procedure(**data)


@pytest.fixture
def kb_model_record(conn) -> KnowledgeStore:
    store = KnowledgeStore(conn)
    saved = store.save(p0010_as_the_model_wrote_it(), extracted_by="model")
    store.set_review(saved.record_id, "approved", reviewer="test")
    conn.commit()
    return store


def test_the_message_names_every_pin_and_the_comparison_even_when_the_label_does_not(kb_model_record):
    engine, state = Engine(kb_model_record, Script().model), session()
    first = engine.turn(state, "fault code P0010").reply.message
    assert "pin 1, male terminal <-> VTC oil control solenoid valve A connector (2P), pin 2, male terminal" in first
    assert "about 7.14 – 7.90 Ω at room temperature" in first
    second = engine.turn(state, "7.5 ohm").reply
    assert "pin 2, female terminal <-> ground" in second.message and "Normal: < 1 Ω" in second.message
    assert "NOTICE: Before disconnecting the PCM connector, jump the SCS line" in second.message
    assert "This step needs the HDS." in second.message and second.step.requires_tool == "HDS"
    engine.turn(state, "0.3 ohm")
    fourth = engine.turn(state, "0.4 ohm").reply.message
    assert "Normal: > 1 MΩ" in fourth and "NOTICE: Before disconnecting" not in fourth  # a notice is shown once


def test_urdu_in_roman_letters_is_refused_and_a_dropped_sign_too():
    from app.engine.wording import translate
    replies = iter([{"message": "Step 2: pin 2 aur ground, normal 1 Ω hai"}, {"message": "قدم 2: پن 2 اور گراؤنڈ، 1 Ω"}])
    model = ScriptedModel({"translate": lambda *_: next(replies)})
    text, translated = translate(model, "Step 2: pin 2 <-> ground. Normal: < 1 Ω", "ur")
    assert not translated and "< 1 Ω" in text and len(model.trace) == 2
    assert [t.rejected for t in model.trace] == [
        "a number, pin, code or comparison sign of the English text is missing; not written in ur script",
        "a number, pin, code or comparison sign of the English text is missing"]


# -- faults found in the first full real-model run (2026-11-15, 42 runs) ------------------------------------
def test_a_part_the_message_does_not_name_is_never_answered_with_its_pins(kb):
    vtc = "What do I measure at the VTC solenoid and what should it read?"
    script = Script(intakes={vtc: {"intent": "spec_question", "components": [VSA]},
                             f"2025 Integra 1.5 turbo, code P0341. {vtc}": {"intent": "spec_question", "components": [VSA]}})
    engine, state = Engine(kb, script.model), session()
    with_code = engine.turn(state, f"2025 Integra 1.5 turbo, code P0341. {vtc}").reply
    assert with_code.tier == "general" and "P0341" in with_code.message and "VSA" not in with_code.message
    asked = Engine(kb, script.model).turn(session(), vtc).reply
    assert asked.question.kind == "component" and asked.question.options == [VSA, "none of these"] and asked.evidence == []


def test_a_confirmed_part_gets_its_answer(kb):
    question = "Where does the power for the brake unit come from?"
    script = Script(intakes={question: {"intent": "wiring_question", "components": [VSA], "power_and_ground": True},
                             "yes the VSA one": {"intent": "answer", "answer": VSA}})
    engine, state = Engine(kb, script.model), session()
    assert engine.turn(state, question).reply.question.kind == "component"
    answered = engine.turn(state, "yes the VSA one").reply
    assert answered.type == "conclusion" and "FUSE 5-5 40A" in answered.message


def test_asking_for_the_wiring_is_not_asking_for_the_diagram_page(kb):
    question = "I need the wiring from the VSA unit to the left front wheel speed sensor."
    script = Script(intakes={question: {"intent": "send_diagram", "components": [VSA, LF]}})
    reply = Engine(kb, script.model).turn(session(), question).reply
    assert reply.diagrams == [] and reply.question.kind == "version" and "BRN" in reply.message


def test_a_case_hint_always_names_its_codes(kb):
    script = Script(advise=[{"use": ["case:byd-qin-plus-dmi-2024:p2b5313-p2b5411"],
                             "message": "An earlier repair on a 2024 BYD Qin Plus DM-i: check the hybrid coolant pump first. Did you?"}])
    reply = Engine(kb, script.model).turn(session(BYD), "BYD Qin Plus 2024 with P2B5313 and P2B5411").reply
    assert reply.message.startswith("Hello, I'm glad to help.\nP2B5313, P2B5411:\n")  # the first reply greets, once


def test_a_hint_about_high_voltage_work_never_mentions_solvents(kb, conn):
    hv = Case(**(byd_case().model_dump(exclude={"review"}) | {"case_id": "case:hv", "sensitive": True,
                                                                 "fault_codes": [{"code": "P0AA600"}]}))
    saved = kb.save(hv, extracted_by="hand")
    kb.set_review(saved.record_id, "approved", reviewer="test")
    conn.commit()
    script = Script(advise=[{"use": ["case:hv"], "message": "An earlier repair on a BYD Qin Plus DM-i: dry the modules with kerosene. Done?"},
                            {"use": ["case:hv"], "message": "An earlier repair on a BYD Qin Plus DM-i: this needs a person qualified for high-voltage work. Done?"}])
    reply = Engine(kb, script.model).turn(session(BYD), "code P0AA600").reply
    assert "kerosene" not in reply.message and "qualified for high-voltage work" in reply.message
    assert "must not mention 'kerosene'" in script.model.trace[-2].rejected


def test_long_code_names_do_not_make_an_arabic_message_english(kb):
    state = session(language="ar")
    Engine(kb, Script().model).turn(state, "تسلا موديل 3 2020 عليها VCFRONT_a490_coolantPumpsNotIdentified و DI_a190_tireRotationRecommended")
    assert state.language == "ar"


def test_the_reading_line_is_written_in_code_and_only_record_texts_go_to_the_model(kb):
    engine, state = Engine(kb, Script().model), session(language="ar")
    engine.turn(state, "أكورا انتيجرا 2025، طالع عندي كود P0010")
    sent = []

    def translate(system, user, schema):
        sent.append(json.loads(user)["lines"])
        return {"lines": [f"{line} [ترجمة {'عربية ' * 12}]" for line in sent[-1]]}  # enough Arabic script to pass

    engine.model.handlers["translate"] = translate
    reply = engine.turn(state, "طلعت القراءة ١٥ أوم").reply
    assert "قراءة الخطوة 1: 15 ohm، خارج الحد الطبيعي (حوالي 7.14" in reply.message  # labels in code, Latin digits
    assert "النتيجة: There is a problem with VTC oil control solenoid valve A. [ترجمة" in reply.message
    assert "بالإنجليزية" not in reply.message  # accepted: no "English follows" note
    assert not any("15" in line or "Step" in line for line in sent[0])  # the model never sees the reading or a label
    assert reply.evidence[0].excerpt.startswith("Step 1 reading: 15 ohm, outside the normal range")  # the excerpt stays English



# -- faults found in the second full real-model run (2026-11-15, 38 of 42 passed) ----------------------------
def test_a_repair_without_saying_the_fault_is_gone_is_not_a_confirmed_fix(kb):
    said = "The battery coolant temperature sensor connector was not seated. Reconnected and secured it."
    script = Script(intakes={said: {"intent": "fix_confirmed", "fix": "connector re-seated", "result_words": "Reconnected and secured it"},
                             "Still the same": {"intent": "fix_confirmed", "fix": "x", "result_words": "the fault is gone"}})
    engine, state = Engine(kb, script.model), session(BYD)
    assert engine.turn(state, said).reply.type != "conclusion"  # quoted words say nothing about the result
    engine.turn(state, "Still the same")  # words not in the message: refused too
    assert state.confirmed_fix is None


def test_every_scenario_message_that_reports_a_fix_has_a_result_word():
    from app.engine.intake import RESULT_WORDS
    fixes = []
    for path in sorted((REPO / "eval/scenarios").glob("*.json")):
        scenario = json.loads(path.read_text(encoding="utf-8"))
        for turn in scenario["turns"]:
            for expect in turn.get("expect", {}).values():
                if expect.get("reply_type") == "conclusion" and expect.get("conclusion_contains") and scenario["group"] == "case":
                    fixes.append(turn["mechanic"])
    assert len(fixes) >= 10 and [m for m in fixes if not RESULT_WORDS.search(m)] == []
    assert not RESULT_WORDS.search("The connectors were not seated. Reconnected and secured both.")


def test_a_cross_vehicle_expert_note_needs_its_fault_message_named():
    from app.engine.engine import note_named
    from app.engine.intake import _words
    from app.kb.schemas import ExpertNote
    note = ExpertNote(**json.loads((REPO / "seed/expert_notes/hv-battery-temp-sensor-swap-test.json").read_text(encoding="utf-8")))
    exeed = "One motor temperature sensor circuit reads about 15 kohm. The other reads 0 ohm with the inverter main connector plugged in."
    assert not note_named(note, _words(exeed))
    assert not note_named(note, _words("P199000 from the transmission computer, front accident, replaced the HV harness"))
    assert note_named(note, _words("hybrid battery temp sensor open circuit on the scan tool"))


# -- faults found in the third full real-model run (2026-11-16, 39 of 42; fast-model comparison) -----------
def test_a_case_found_by_meaning_with_other_codes_is_never_offered(kb):
    said = "code P0AA600, the coolant pump of the hybrid electric system does not run"
    script = Script(advise=[{"use": ["case:byd-qin-plus-dmi-2024:p2b5313-p2b5411"], "message": "An earlier repair on a BYD. Check?"}])
    reply = Engine(kb, script.model).turn(session(BYD), said).reply
    assert reply.tier == "general" and script.calls("advise") == []  # removed in code before the model saw it


def test_repairs_reported_along_the_way_are_part_of_the_confirmed_fix(kb):
    seated = "The coolant temperature sensor connector was not seated. Reconnected it."
    done = "Did the window regulator basic setting and cleared the codes. Everything works normally."
    script = Script(intakes={seated: {"intent": "finding", "repair": "coolant temperature sensor connector re-seated"},
                             done: {"intent": "fix_confirmed", "fix": "window regulator basic setting", "result_words": "works normally"}})
    engine, state = Engine(kb, script.model), session(BYD)
    engine.turn(state, seated)
    reply = engine.turn(state, done).reply
    assert reply.possible_causes[0].name == "coolant temperature sensor connector re-seated; window regulator basic setting"
    from app.engine.summary import pending_case
    assert pending_case(state, "s1")["repair_steps"] == ["coolant temperature sensor connector re-seated",
                                                         "window regulator basic setting"]


# -- faults found in the fourth full real-model run (2026-11-16, 41 of 42) ---------------------------------
def test_a_case_with_no_code_is_not_offered_by_meaning_for_a_real_code():
    from types import SimpleNamespace

    from app.engine.engine import other_codes
    no_code = SimpleNamespace(fault_codes=[SimpleNamespace(code=None)])  # the reverse-polarity case
    assert other_codes(no_code, ["PCS_A027_SCHG"])  # a real alert: nothing ties that case to it
    assert other_codes(no_code, ["P0420"])
    assert not other_codes(no_code, ["VIN mismatch"])  # a wording, not a code: meaning decides
    assert not other_codes(no_code, [])  # no code reported (the A/C air shutter case)
    same = SimpleNamespace(fault_codes=[SimpleNamespace(code="PCS_A027")])
    assert not other_codes(same, ["PCS_A027_SCHG"])


def test_general_guidance_never_repeats_the_header_the_code_puts_in_front(kb):
    from app.engine.wording import fixed
    header = fixed("general_header", "en", codes="P0341")
    script = Script(general=[{"message": f"{header}\nStart with the battery and the fuses. What do you find?"}])
    reply = Engine(kb, script.model).turn(session(), "2025 Integra 1.5 turbo, code P0341.").reply
    assert reply.message.count(header) == 1 and "Start with the battery" in reply.message


def test_the_summary_shows_readings_as_the_steps_did():
    from app.engine.summary import checks
    stored = [{"step_id": "1", "value": 15.0, "unit": "Ohm", "verdict": "abnormal"},
              {"step_id": "4", "over_limit": True, "verdict": "normal"}]
    assert checks(stored) == ["Step 1: 15 ohm, outside the normal range", "Step 4: OL (over the meter's range), normal"]


# -- fifth full real-model run (2026-11-17, 42 of 42) --------------------------------------------------------
def test_the_model_noting_its_own_length_is_not_a_number_without_a_source(kb):
    said = ("Check the wiring and the connectors first, then the power supply, using the service information. "
            "120 words limit reached. What did you find?")
    script = Script(general=[{"message": said}])
    reply = Engine(kb, script.model).turn(session(), "2025 Integra 1.5 turbo, code P0341.").reply
    assert "Check the wiring" in reply.message and "120" not in reply.message
    assert not any(t.rejected for t in script.model.trace)  # accepted the first time


def test_a_partly_copied_header_is_removed_too():
    from app.engine.engine import strip_header
    from app.engine.wording import fixed
    codes = "P2B5313, P2B5411"
    header = fixed("general_header", "ar", codes=codes)
    copied = "P2B5313, P2B5411: لا توجد بيانات موثقة لهذه السيارة في قاعدة البيانات. افحص الوصلات أولاً."
    assert strip_header(copied, header, codes) == "افحص الوصلات أولاً."
    assert strip_header("Less than 1. Then check it.", fixed("general_header", "en", codes="P0341"), "P0341") == "Less than 1. Then check it."


# -- Ali's review of the demo (2026-11-17): one question at a time; polite and professional ------------------
def test_a_model_reply_with_two_questions_is_asked_again_and_then_keeps_only_the_last(kb):
    two = "Start with the fuses. Did you check the fuses? Have you checked the ground?"
    script = Script(general=[{"message": two}, {"message": two}])
    reply = Engine(kb, script.model).turn(session(), "2025 Integra 1.5 turbo, code P0341.").reply
    assert reply.message.count("?") == 1 and reply.message.rstrip().endswith("Have you checked the ground?")
    assert "asks 2 questions" in script.calls("general")[0].rejected


def test_the_first_reply_greets_once_and_an_emergency_does_not(kb):
    engine, state = Engine(kb, Script().model), session()
    first = engine.turn(state, "2025 Acura Integra 1.5L turbo, fault code P0010.").reply
    second = engine.turn(state, "7.5 ohm").reply
    assert first.message.startswith("Hello, I'm glad to help.") and "Hello" not in second.message
    alarm = Engine(kb, Script().model).turn(session(), "There is smoke coming from the battery").reply
    assert alarm.type == "emergency" and "Hello" not in alarm.message


def test_every_fixed_text_asks_at_most_one_question():
    from app.engine.wording import FIXED
    assert all(text.count("?") + text.count("؟") <= 1 for texts in FIXED.values() for text in texts.values())


def test_every_fixed_text_exists_in_every_language_with_the_same_slots_and_fills_in():
    from string import Formatter

    from app.engine.wording import FIXED, fixed

    def slots(text):
        return {field for _, field, _, _ in Formatter().parse(text) if field}

    for key, texts in FIXED.items():
        assert set(texts) == {"en", "ar", "ur"}, key
        assert slots(texts["ar"]) == slots(texts["en"]) == slots(texts["ur"]), key
        for language in texts:  # a slot called name or language must not clash with fixed's own arguments
            fixed(key, language, **{slot: "X" for slot in slots(texts[language])})


def test_the_models_own_greeting_is_dropped_so_the_reply_greets_once(kb):
    said = "مساء الخير. ابدأ بفحص البطارية والـ Fuses الخاصة بالـ ABS unit. هل فحصت البطارية؟"
    reply = Engine(kb, Script(general=[{"message": said}]).model).turn(
        session(language="ar"), "مساء الخير، أكورا انتيجرا 2025، لمبة الـ ABS ضاوية وكود P0341").reply
    assert reply.message.startswith("أهلاً بك، يسعدني مساعدتك.") and "مساء الخير" not in reply.message
    assert "ABS unit" in reply.message  # English words for tools and parts stay (Ali, 2026-11-18)


def test_a_follow_up_wiring_question_uses_the_part_named_in_an_earlier_message(kb):
    # Ali's demo, 2026-11-18: "the ABS light is on" then "which fuses should I check and where is its ground?"
    first = "أكورا انتيجرا 2025، لمبة الـ ABS ضاوية وجهاز الفحص مش قادر يتواصل مع وحدة الـ ABS"
    second = "أي فيوزات لازم أفحص ووين الأرضي تبعها؟"
    script = Script(intakes={first: {"intent": "diagnose", "english": "ABS light on, the scan tool cannot talk to the ABS unit"},
                             second: {"intent": "wiring_question", "power_and_ground": True,
                                      "english": "Which fuses should I check and where is its ground?"},
                             "ابعثلي المخطط": {"intent": "send_diagram"}},
                    general=[{"message": "ابدأ بفحص البطارية. هل فحصتها؟"}])
    engine, state = Engine(kb, script.model), session(language="ar")
    engine.turn(state, first)
    answer = engine.turn(state, second).reply
    assert answer.tier == "verified" and "G020" in answer.message  # not "no verified data", not "which part?"
    sent = engine.turn(state, "ابعثلي المخطط").reply
    assert sent.diagrams  # not "which system do you need the diagram for?"


def test_a_part_found_by_code_must_be_named_in_full():
    from app.engine.intake import _words, fully_named
    said = _words("ABS light is on, the scan tool cannot talk to the ABS unit")
    assert fully_named("VSA MODULATOR-CONTROL UNIT", said)
    assert not fully_named("VSA OFF SWITCH", said) and not fully_named("BACKUP LIGHT SWITCH", said)


def test_wiring_answers_and_their_questions_are_written_in_code_in_the_mechanics_language(kb):
    # demo, 2026-11-18: translated wiring answers read badly in Arabic, and a clicked button switched the reply to English
    q1, q2 = "أي فيوزات لازم أفحص ووين الأرضي تبعها لوحدة الـ ABS؟", "كيف أفحص الأرضي؟"
    script = Script(intakes={q1: {"intent": "wiring_question", "power_and_ground": True, "components": [VSA], "english": "ABS unit fuses and ground"},
                             q2: {"intent": "check_wire"}})
    engine, state = Engine(kb, script.model), session(language="ar")
    answer = engine.turn(state, q1).reply
    assert "• البن 3 (BLK): أرضي عند G020 (صفحة 1 من 3)." in answer.message
    assert "-BLK->" not in answer.message and "G020" in answer.evidence[0].excerpt  # the full route stays in the sources
    assert not script.calls("translate")  # no model call for a wiring answer
    asked = engine.turn(state, q2).reply
    assert asked.message == "أي سلك تريد أن تفحص؟" and f"{VSA} pin 3" in asked.question.options
    intakes_before = len(script.calls("intake"))
    step = engine.turn(state, f"{VSA} pin 3").reply  # the button
    assert state.language == "ar" and len(script.calls("intake")) == intakes_before  # same language, no model call
    assert step.type == "step" and f"فحوصات {VSA} pin 3 إلى ground G020" in step.message


# -- web search: no verified data, pages from the web (project doc web-search-design.md) --------------------
class FakeWeb:
    """An external source that returns the pages it is given, and records what it was asked."""

    name = "web"
    codes_per_lookup = 2

    def __init__(self, items, searches_per_session=6):
        from app.engine.external import ExternalLookup
        from app.engine.model import TraceEntry

        self.items, self.searches_per_session, self.asked = items, searches_per_session, []
        self._lookup, self._trace = ExternalLookup, TraceEntry

    def lookup(self, vehicle, codes, question):
        self.asked.append(list(codes))
        return self._lookup(items=list(self.items), calls=[self._trace("web_search", "exa", 1.2, cost_usd=0.007)], searched=len(codes))


def web_page(url, title, text, applies="fits", reasons=()):
    from app.engine.external import ExternalItem
    from app.web.source import item_id

    return ExternalItem(id=item_id(url), provider="exa", title=title, text=text, url=url, kind="web",
                        extra={"applies": applies, "reasons": list(reasons), "code": "P0341", "domain": url.split("/")[2]})


CAM = web_page("https://www.example-codes.com/p0341-integra", "P0341 on the 2025 Acura Integra",
               "P0341 on the 2025 Integra: the camshaft position signal does not match the crankshaft. "
               "Check the CMP sensor connector, then the timing chain. Sensor resistance 7.14 ohm.")
BULLETIN = web_page("https://static.nhtsa.gov/odi/tsbs/2024/MC-1.pdf", "Acura bulletin A24-001 P0341",
                    "A24-001: some 2023- Integra may set P0341 after an oil change", applies="check",
                    reasons=["open year range: may be another generation"])


def test_no_verified_data_answers_from_web_pages_each_linked_and_marked(kb):
    script = Script(web=[{"use": [CAM.id, BULLETIN.id],
                          "message": "Per example-codes.com, P0341 means the camshaft and crankshaft signals disagree. "
                                     "Bulletin A24-001 may also apply. First check the CMP sensor connector. What do you see?"}])
    web = FakeWeb([CAM, BULLETIN])
    engine, state = Engine(kb, script.model, external=web), session()
    result = engine.turn(state, "2025 Integra, code P0341.")
    reply = result.reply
    assert reply.tier == "web" and reply.type == "question" and reply.confidence == "low"
    assert "web pages, not verified" in reply.message and "confirm they match this car" in reply.message  # one page is "check"
    assert [(e.type, e.url, e.applies) for e in reply.evidence] == [("web", CAM.url, "fits"), ("web", BULLETIN.url, "check")]
    assert web.asked == [["P0341"]] and state.external_searches == 1
    assert [t.model for t in script.model.trace if t.purpose == "web_search"] == ["exa"]
    assert [g.tier for g in result.gaps] == ["web"]  # still a gap in the knowledge base


def test_a_value_from_a_web_page_is_refused_and_the_reply_falls_back_to_general_knowledge(kb):
    value = {"use": [CAM.id], "message": "Measure the CMP sensor: it should read 7.14 ohm. What do you read?"}
    script = Script(web=[value, value], general=[{"message": "Start with the CMP sensor connector. What do you find?"}])
    engine, state = Engine(kb, script.model, external=FakeWeb([CAM])), session()
    reply = engine.turn(state, "2025 Integra, code P0341.").reply
    assert reply.tier == "general" and "7.14" not in reply.message
    assert [t.rejected is not None for t in script.calls("web")] == [True, True]


def test_web_search_stops_at_the_sessions_limit_and_nothing_found_means_general_knowledge(kb):
    web = FakeWeb([CAM], searches_per_session=1)
    script = Script(web=[{"use": [CAM.id], "message": "Check the CMP sensor connector first. What do you see?"}],
                    general=[{"message": "Start with the battery and the fuses. What do you find?"}] * 2)
    engine, state = Engine(kb, script.model, external=web), session()
    assert engine.turn(state, "2025 Integra, code P0341.").reply.tier == "web"
    assert engine.turn(state, "P0344 as well, same car.").reply.tier == "general"  # the one search is spent
    assert web.asked == [["P0341"]]
    nothing = Engine(kb, Script().model, external=FakeWeb([])).turn(session(), "2025 Integra, code P0341.").reply
    assert nothing.tier == "general"


def test_web_search_is_never_used_where_verified_data_exists_or_for_a_pin_question(kb):
    web = FakeWeb([CAM])
    procedure = Engine(kb, Script().model, external=web).turn(session(), "2025 Acura Integra 1.5L turbo, fault code P0010.").reply
    assert procedure.tier == "verified"
    script = Script(intakes={"Which ECM pin is the rear oxygen sensor?": {"intent": "spec_question"}})
    pin = Engine(kb, script.model, external=web).turn(session({"make": "Toyota", "model": "Camry", "year": 2018}),
                                                      "Which ECM pin is the rear oxygen sensor?").reply
    assert pin.tier == "general" and "pin number, wire colour" in pin.message and web.asked == []


def test_a_high_voltage_car_gets_the_safety_warning_and_no_solvent_advice_from_the_web(kb):
    page = web_page("https://ev.example/qin", "BYD Qin Plus DM-i P2B6013", "P2B6013 on the 2024 Qin Plus DM-i: insulation fault")
    cleaner = {"use": [page.id], "message": "Clean the battery pack connector with brake cleaner. Did that help?"}
    safe = {"use": [page.id], "message": "This is an insulation fault: a qualified high-voltage technician must check it. Is the car parked?"}
    script = Script(web=[cleaner, safe])
    engine = Engine(kb, script.model, external=FakeWeb([page]))
    reply = engine.turn(session(BYD), "Qin Plus DM-i 2024, code P2B6013.").reply
    assert reply.tier == "web" and "brake cleaner" not in reply.message and any("high-voltage" in w for w in reply.warnings)


# -- comparison run 2026-11-22 (venorexai cases): wording fixes ----------------------------------------------
def test_a_value_question_with_no_data_is_refused_in_value_words_and_a_pin_question_in_wiring_words(kb):
    torque = "I need the exact head bolt torque values and sequence."
    script = Script(intakes={torque: {"intent": "spec_question"},
                             "Which ECM pin is the rear oxygen sensor?": {"intent": "spec_question"}})
    value = Engine(kb, script.model).turn(session({"make": "Changan", "model": "CS85", "year": 2023}), torque).reply
    assert value.tier == "general" and "exact value or specification" in value.message and "pin" not in value.message
    pin = Engine(kb, script.model).turn(session({"make": "Toyota", "model": "Camry", "year": 2018}),
                                        "Which ECM pin is the rear oxygen sensor?").reply
    assert pin.tier == "general" and "pin number, wire colour" in pin.message


def test_the_model_never_calls_the_mechanic_colleague(kb):
    said = "يا زميلي. يشير الكود إلى خلل في الحساس. هل فحصت الوصلة؟"
    script = Script(general=[{"message": said}, {"message": said}])
    reply = Engine(kb, script.model).turn(session(language="ar"), "انتيجرا 2025، الكود P0341").reply
    assert reply.tier == "general" and "يا زميلي" not in reply.message and "يشير الكود" in reply.message


# -- NHTSA public records (app/public): no verified data, and a note beside verified answers -----------------
class FakePublic:
    """Public records that return the items they are given, and record what they were asked."""

    name = "nhtsa"

    def __init__(self, items=(), known=()):
        self.items, self.known_items, self.asked, self.asked_known = list(items), list(known), [], []

    def lookup(self, vehicle, codes, question):
        from app.engine.external import ExternalLookup
        from app.engine.model import TraceEntry

        self.asked.append((list(codes), question))
        return ExternalLookup(items=list(self.items), calls=[TraceEntry("public_search", "nhtsa", 0.01)])

    def known(self, vehicle, codes):
        self.asked_known.append(list(codes))
        return list(self.known_items)


def pub_record(source_type, external_id, title, text, applies="fits"):
    from app.engine.external import ExternalItem
    from app.public.source import LABEL

    source = {"official_recall": "nhtsa_recall", "official_investigation": "nhtsa_investigation",
              "unverified_report": "nhtsa_complaint"}[source_type]
    return ExternalItem(id=f"pub:{source}:{external_id}:1", provider="nhtsa", title=title, text=text, url=None, kind=source,
                        extra={"applies": applies, "source_type": source_type, "label": LABEL[source_type],
                               "external_id": external_id, "domain": "NHTSA (US)"})


RECALL = pub_record("official_recall", "23V100", "NHTSA recall 23V100 — Honda Integra 2023-2025: ENGINE",
                    "VTC oil control solenoid may fail and set P0010. Remedy: dealers will replace the solenoid.")
COMPLAINT = pub_record("unverified_report", "11500001", "Owner complaint (unverified) — Honda Integra 2024: ENGINE",
                       "Engine rattles at cold start and the check engine light came on with P0341.", applies="check")


def test_no_verified_data_answers_from_public_records_labelled_and_marked(kb):
    script = Script(web=[{"use": [COMPLAINT.id], "message": "One US owner complaint (unverified) describes a rattle at cold "
                                                            "start with P0341. First check the oil level. What is the level?"}])
    public = FakePublic([COMPLAINT])
    engine, state = Engine(kb, script.model, public=public), session()
    result = engine.turn(state, "2025 Integra, code P0341.")
    reply = result.reply
    assert reply.tier == "public" and reply.confidence == "low"
    assert "US public records (NHTSA" in reply.message and "US-market cars can differ" in reply.message  # "check" record
    assert [(e.type, e.applies) for e in reply.evidence] == [("public", "check")]
    assert reply.evidence[0].title.startswith("NHTSA owner complaint (unverified owner report)")
    assert public.asked[0][0] == ["P0341"] and [g.tier for g in result.gaps] == ["public"]
    assert "NHTSA" in script.calls("web")[0].request["system"]  # the model is told what each kind of record is


def test_public_records_answer_a_symptom_with_no_code_and_with_web_the_header_names_both(kb):
    script = Script(web=[{"use": [COMPLAINT.id], "message": "A US owner report describes a cold-start rattle. Is it at the top of the engine?"}])
    engine = Engine(kb, script.model, public=FakePublic([COMPLAINT]))
    reply = engine.turn(session(), "the engine rattles when cold").reply
    assert reply.tier == "public" and reply.message.split("\n")[1].startswith("No verified data")
    both = Script(web=[{"use": [CAM.id, RECALL.id], "message": "Per example-codes.com and a US recall, check the CMP connector first. What do you see?"}])
    mixed = Engine(kb, both.model, external=FakeWeb([CAM]), public=FakePublic([RECALL])).turn(session(), "2025 Integra, code P0341.").reply
    assert mixed.tier == "web" and "web pages and US public records (NHTSA)" in mixed.message
    assert {e.type for e in mixed.evidence} == {"web", "public"}


def test_nothing_used_from_public_records_falls_back_to_general_knowledge(kb):
    script = Script(web=[{"use": [], "message": ""}], general=[{"message": "Start with the CMP sensor connector. What do you find?"}])
    reply = Engine(kb, script.model, public=FakePublic([COMPLAINT])).turn(session(), "2025 Integra, code P0341.").reply
    assert reply.tier == "general"


def test_a_verified_procedure_gets_the_known_records_note_once_and_its_steps_unchanged(kb):
    public = FakePublic(known=[RECALL])
    plain = Engine(kb, Script().model).turn(session(), "2025 Acura Integra 1.5L turbo, fault code P0010.").reply
    engine, state = Engine(kb, Script().model, public=public), session()
    first = engine.turn(state, "2025 Acura Integra 1.5L turbo, fault code P0010.").reply
    assert first.tier == "verified" and first.step == plain.step  # the verified step is the same
    assert first.message.startswith(plain.message.split("\n")[0])
    assert "Also on record for this car and code (US public records, NHTSA" in first.message
    assert "• Safety recall: NHTSA recall 23V100" in first.message
    assert [e.type for e in first.evidence][-1] == "public" and public.asked_known == [["P0010"]]
    assert not public.asked  # verified data exists: no public lookup for the answer itself
    engine.turn(state, "15 ohm")
    again = Engine(kb, Script().model, public=public).turn(state, "fault code P0010").reply
    assert "23V100" not in again.message  # listed once per session


def test_public_records_off_changes_nothing(kb):
    script = Script(general=[{"message": "Start with the CMP sensor connector. What do you find?"}])
    reply = Engine(kb, script.model, public=None).turn(session(), "2025 Integra, code P0341.").reply
    assert reply.tier == "general" and "NHTSA" not in reply.message


# -- hazards and general standard values (comparison run 2026-11-24) ----------------------------------------
KONA = {"make": "Hyundai", "model": "Kona Electric", "year": 2022, "powertrain": "EV"}


def test_a_hands_on_high_voltage_question_gets_the_safety_text_first_never_a_wiring_question(kb):
    q = "Isolation fault warning, the car won't go READY. I have a multimeter, can I test the orange cables myself?"
    script = Script(intakes={q: {"intent": "wiring_question", "components": []}},
                    general=[{"message": "No: this needs a qualified high-voltage technician. Has the car been in water?"}])
    engine, state = Engine(kb, script.model), session(KONA)
    reply = engine.turn(state, q).reply
    lines = reply.message.split("\n")
    assert lines[1].startswith("Safety first: do not touch, unplug, open, clean or measure the orange")  # after the greeting
    assert lines[2].startswith("With a high-voltage isolation fault warning, do not charge the car")
    assert reply.tier == "general" and "wiring diagram" not in reply.message
    assert any("draft" in w for w in reply.warnings) and any("high-voltage" in w for w in reply.warnings)
    facts = json.loads(script.calls("general")[0].request["user"])
    assert any("high-voltage" in t for t in facts["safety topics (a fixed safety text is shown above your answer)"])
    again = engine.turn(state, "OK, and what should I tell the owner?").reply
    assert "Safety first" not in again.message  # once per session


def test_a_flashing_engine_light_gets_the_driving_warning_and_the_cylinder_number_is_allowed(kb):
    q = "2013 Sonata, P0300 and P0302, the light was flashing. Can I drive to Dubai this weekend?"
    script = Script(general=[{"message": "Cylinder 2 is misfiring: swap its coil with a neighbouring cylinder and see if the misfire moves. "
                                         "Does it happen in rain?"}])
    reply = Engine(kb, script.model).turn(session({"make": "Hyundai", "model": "Sonata", "year": 2013}), q).reply
    assert "A flashing check-engine light means a severe misfire" in reply.message
    assert "Cylinder 2 is misfiring" in reply.message and not any(t.rejected for t in script.calls("general"))


def test_a_general_standard_value_is_allowed_only_when_its_topic_is_asked(kb):
    q = "Camry 2018 with U0100 and U0129. I measured about 120 ohm between pin 6 and pin 14 with the battery off. Normal?"
    good = {"message": "A healthy high-speed CAN bus reads about 60 ohm there (general standard, confirm in the service "
                       "information): 120 ohm means one terminator is open. Which modules are on the bus?"}
    script = Script(general=[good])
    reply = Engine(kb, script.model, statuses=APPROVED_AND_DRAFT).turn(session({"make": "Toyota", "model": "Camry", "year": 2018}), q).reply
    assert "60 ohm" in reply.message and not any(t.rejected for t in script.calls("general"))
    off = Script(general=[good, good], )  # approved records only: the draft standard values are not given
    plain = Engine(kb, off.model).turn(session({"make": "Toyota", "model": "Camry", "year": 2018}), q).reply
    assert "60 ohm" not in plain.message and all(t.rejected for t in off.calls("general"))

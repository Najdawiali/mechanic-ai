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
            "power_and_ground": False, "answer": None, "fix": None}
    return base | fields


class Script:
    """A scripted model: intake answers by message, translation that keeps the text, compose answers in order."""

    def __init__(self, intakes=None, advise=None, general=None, report=None, translate=None):
        self.intakes = intakes or {}
        self.advise = list(advise or [])
        self.general = list(general or [])
        self.report = report
        self.translate = translate
        self.model = ScriptedModel({
            "intake": self._intake, "translate": self._translate,
            "advise": lambda *_: self.advise.pop(0) if self.advise else {"use": [], "message": ""},
            "general": lambda *_: self.general.pop(0) if self.general else {"message": "Start with the battery, the fuses and the grounds."},
            "summary": lambda *_: {"message": "Session closed."}, "report": lambda *_: self.report})

    def _intake(self, system, user, schema):
        message = json.loads(user)["message"]
        return intake(english=message) | self.intakes.get(message, {})

    def _translate(self, system, user, schema):
        return self.translate(user) if self.translate else {"message": user.split("\n\n(Your previous")[0]}

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
    assert "15 ohm" in second.message and "outside the normal range" in second.message
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


def test_a_pin_question_with_no_diagram_gets_no_verified_information(kb):
    script = Script(intakes={"Which ECM pin is the rear oxygen sensor?": {"intent": "spec_question"}})
    engine, state = Engine(kb, script.model), session({"make": "Toyota", "model": "Camry", "year": 2018})
    reply = engine.turn(state, "Which ECM pin is the rear oxygen sensor?").reply
    assert reply.tier == "none" and reply.evidence == []


def test_general_and_none_replies_are_logged_as_gaps(kb):
    script = Script(intakes={"Which ECM pin is the rear oxygen sensor?": {"intent": "spec_question"}})
    engine = Engine(kb, script.model)
    result = engine.turn(session({"make": "Toyota", "model": "Camry", "year": 2018}), "Which ECM pin is the rear oxygen sensor?")
    assert [(g.tier, g.question) for g in result.gaps] == [("none", "Which ECM pin is the rear oxygen sensor?")]


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
    script = Script(intakes={"Replaced the inverter, car runs normally": {"intent": "fix_confirmed", "fix": "inverter replacement"}})
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

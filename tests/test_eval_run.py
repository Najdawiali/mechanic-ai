"""The scenario runner, offline: the real scenarios, the test database, a scripted model."""

import pytest

from app.engine.reply import Reply
from app.kb.store import APPROVED, KnowledgeStore
from eval.run import HeldOut, check, conditions, load, play
from eval.scenario_schema import TurnCheck
from tests.test_engine import Script, abs_diagram, byd_case, p0010


@pytest.fixture
def kb(conn) -> KnowledgeStore:
    store = KnowledgeStore(conn)
    for record in (p0010(), abs_diagram(), byd_case()):
        saved = store.save(record, extracted_by="hand")
        store.set_review(saved.record_id, "approved", reviewer="test")
    conn.commit()
    return store


WORDS = {"I'm getting fifteen ohms": {"intent": "reading", "reading": {"value": 15, "unit": "ohm", "over_limit": False}}}


@pytest.mark.parametrize("only", ["p0010:", "emergency:", "no-data:acura-integra-2025:p0341"])
def test_the_procedure_emergency_and_no_data_scenarios_pass_with_a_model_that_reads_correctly(kb, only):
    scenarios = load(only)
    assert scenarios
    for scenario in scenarios:
        for condition in conditions(scenario):
            result = play(scenario, condition, kb, Script(intakes=WORDS).model, statuses=APPROVED)
            assert result.passed, [(t.turn, t.wrong, t.error) for t in result.turns]


def test_a_wrong_reply_fails_its_checks():
    reply = Reply(type="step", language="en", message="Measure 12 V at pin 64.", tier="general")
    wrong = check(reply, TurnCheck(reply_type="conclusion", label="verified", message_must_not_contain=["12 V"],
                                   evidence_types=["procedure"]))
    assert len(wrong) == 4


def test_a_held_out_case_is_hidden_from_every_lookup(kb):
    held = HeldOut(kb.conn, {byd_case().case_id})
    vehicle = {"make": "BYD", "model": "Qin Plus DM-i", "year": 2024, "powertrain": "PHEV"}
    from app.kb.schemas import Vehicle
    assert kb.find_by_codes(Vehicle(**vehicle), ["P2B5313"]) and not held.find_by_codes(Vehicle(**vehicle), ["P2B5313"])


def test_a_rejected_translation_is_listed_with_its_reason(kb):
    (scenario,) = load("p0010:valve:ar")
    script = Script(translate=lambda user: {"message": user})  # an English "translation": not in Arabic script
    first = play(scenario, "standard", kb, script.model, statuses=APPROVED).turns[0]
    assert first.calls == ["intake", "translate (rejected: not written in ar script)",
                           "translate (rejected: not written in ar script)"]
    assert "بالإنجليزية" in first.message  # after two refusals the English text is sent, with a note in Arabic

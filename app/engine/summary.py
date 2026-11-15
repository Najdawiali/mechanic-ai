"""What the session leaves behind when it is closed (handoff 7.3, POST /ai/sessions/{id}/close).

  summary       a short account for the platform's history, in the mechanic's language
  record        the same facts as data: vehicle, codes, readings with verdicts, conclusion, fix, sources
  pending_case  a draft workshop case built from the session, for the platform's admin to review.
                It is never served until a person approves it and sends it back (POST /ai/cases).

The facts come from the session state, not from the model. The model writes the summary sentences
only, and they pass the validator; if they do not, a summary written in code is used.
"""

from __future__ import annotations

from app.engine import render
from app.engine.model import ModelClient
from app.engine.readings import Reading
from app.engine.state import SessionState
from app.engine.validator import Allowed
from app.engine.wording import compose, translate

SYSTEM = """Write a short summary of a car diagnosis session for the workshop's records: the vehicle, the fault codes,
the checks done with their readings and results, the conclusion, and the confirmed repair if there is one.
Use only the facts given. At most 100 words, plain sentences, no headings."""


def record(state: SessionState) -> dict:
    return {
        "vehicle": state.vehicle.model_dump(exclude_none=True),
        "codes": state.codes,
        "readings": [r.model_dump(exclude_none=True) for r in state.readings],
        "conclusion": state.conclusion,
        "confirmed_fix": state.confirmed_fix,
        "evidence_ids": state.cited,
        "emergency": state.emergency,
    }


def _code_summary(facts: dict) -> str:
    lines = [f"Vehicle: {render.vehicle_text(facts['vehicle_record'])}."]
    if facts["codes"]:
        lines.append(f"Fault codes: {', '.join(facts['codes'])}.")
    for r in facts["readings"]:
        shown = render.reading_text(Reading(value=r.get("value"), unit=r.get("unit"), over_limit=r.get("over_limit", False)))
        lines.append(f"Step {r['step_id']}: {shown} ({r['verdict']}).")
    if facts["conclusion"]:
        lines.append(f"Conclusion: {facts['conclusion']}")
    if facts["confirmed_fix"]:
        lines.append(f"Confirmed repair: {facts['confirmed_fix']}")
    return " ".join(lines)


def pending_case(state: SessionState, session_id: str) -> dict | None:
    """A draft case, only when the mechanic confirmed a repair: an unconfirmed guess is not a case."""
    if not state.confirmed_fix:
        return None
    v = state.vehicle
    found = [t.english or t.text for t in state.history if t.role == "mechanic"]
    return {
        "case_id": f"case:session:{session_id}",
        "make": v.make, "models": [v.model],
        "repaired_cars": [{"model": v.model, "year": v.year}],
        "year_from": v.year, "year_to": v.year,
        "powertrain": v.powertrain.value if v.powertrain else None, "engine": v.engine_code,
        "fault_codes": [{"code": c} for c in state.codes],
        "findings": [f"Step {r.step_id}: {render.reading_text(Reading(value=r.value, unit=r.unit, over_limit=r.over_limit))} ({r.verdict})"
                     for r in state.readings],
        "symptoms": found[:1],
        "diagnosis": state.conclusion or state.confirmed_fix,
        "repair_steps": list(state.repairs) or [state.confirmed_fix],
        "notes": found[1:],
        "outcome": "fixed_not_verified",
        "source": {"kind": "client_case_file", "file": f"session:{session_id}", "captured_via": "AI assistant session"},
    }


def summarise(model: ModelClient, state: SessionState, session_id: str, *, role: str = "reasoning") -> dict:
    facts = record(state)
    english_facts = {**facts, "vehicle_record": state.vehicle,
                     "what the mechanic said": [t.english or t.text for t in state.history if t.role == "mechanic"]}
    allowed = Allowed.from_texts([str(facts), *english_facts["what the mechanic said"], render.vehicle_text(state.vehicle)])
    out = compose(model, "summary", system=SYSTEM, facts={k: v for k, v in english_facts.items() if k != "vehicle_record"},
                  language=state.language, allowed=allowed, role=role)
    if out and out["message"].strip():
        text = out["message"]
    else:
        text, _ = translate(model, _code_summary(english_facts), state.language)
    return {"summary": text, "language": state.language, "record": facts, "pending_case": pending_case(state, session_id)}

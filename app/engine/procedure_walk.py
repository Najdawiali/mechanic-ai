"""Walking a troubleshooting procedure: which step comes next, and when it ends.

Pure functions over a Procedure record. No model, no database, no state of their own, so
the logic that decides a diagnosis is small enough to test exhaustively.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.engine.readings import Reading, compare
from app.kb.schemas import Procedure, Step


@dataclass(frozen=True)
class Outcome:
    step: Step | None = None  # the next step to give the mechanic
    conclusion: str | None = None  # or the verdict, in the manual's words
    decided_by_step: str | None = None  # the step whose reading was abnormal; None = by elimination


def first_step(procedure: Procedure) -> Step:
    return procedure.steps[0]


def get_step(procedure: Procedure, step_id: str) -> Step:
    for step in procedure.steps:
        if step.id == step_id:
            return step
    raise KeyError(f"{procedure.procedure_id} has no step {step_id!r}")


def advance(procedure: Procedure, step_id: str, reading: Reading) -> Outcome:
    """The mechanic reported `reading` for step `step_id`. What now?

    Raises ReadingError (with a question to ask) when the reading cannot be compared.
    """
    return walk(procedure.steps, step_id, reading, procedure.elimination_conclusion, name=procedure.procedure_id)


def walk(steps: list[Step], step_id: str, reading: Reading, when_all_normal: str | None, *, name: str = "these steps") -> Outcome:
    """The same decision for any list of linked steps: a manufacturer's procedure, or the checks
    worked out from a wiring diagram (app/engine/circuit_plan.py)."""
    by_id = {step.id: step for step in steps}
    if step_id not in by_id:
        raise KeyError(f"{name} has no step {step_id!r}")
    step = by_id[step_id]
    if step.normal is None:
        raise ValueError(f"step {step_id!r} has no normal range to compare with")
    if compare(reading, step.normal) == "abnormal":
        return Outcome(conclusion=step.if_abnormal, decided_by_step=step.id)
    if step.if_normal is not None:
        return Outcome(step=by_id[step.if_normal])
    if when_all_normal is None:
        raise ValueError(f"{name}: all steps normal and no conclusion by elimination")
    return Outcome(conclusion=when_all_normal)

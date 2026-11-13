"""An evaluation scenario: a scripted mechanic, and what the assistant must and must not do.

Two kinds of expectation:

  per turn (`Turn.expect`)       machine-checked against the reply's fields: its type, its
                                 label, the step's test points and normal range, the conclusion
  per conversation (`expected`)  behaviour a reviewer or a judge checks, in plain words

Knowledge-base conditions. A solved case can be both a KB record and a test, and those must
not be confused, so a case scenario is judged twice:

  case_held_out  the matching case is NOT in the KB  -> the assistant must not invent the fix
  case_in_kb     the matching case IS in the KB      -> the assistant must offer it as a hint

Every other scenario uses the single condition `standard`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.kb.schemas import NormalRange, Review, Source, Vehicle

Condition = Literal["standard", "case_in_kb", "case_held_out"]
Group = Literal["procedure", "language", "case", "diagram", "report", "no_data", "emergency"]
ReplyType = Literal["question", "step", "conclusion", "escalation", "emergency"]
Label = Literal["verified", "general", "none"]
EvidenceType = Literal["procedure", "diagram", "case", "expert_note"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadingValue(_Strict):
    """What the intake step must extract from the mechanic's message."""

    value: float | None = None
    unit: str | None = None
    over_limit: bool = False


class PointCheck(_Strict):
    pin: str | None = None
    ground: bool = False
    connector_contains: str | None = None  # e.g. "PCM connector E"


class StepCheck(_Strict):
    step_id: str
    test_points: list[PointCheck]  # compared as a set: either end may come first
    normal: NormalRange | None = None  # must equal the source exactly (value fidelity)


class TurnCheck(_Strict):
    """Checks on one reply. All text comparisons ignore letter case."""

    reply_type: ReplyType | None = None
    label: Label | None = None
    label_any_of: list[Label] = Field(default_factory=list)  # when more than one label is acceptable
    step: StepCheck | None = None
    conclusion_contains: list[str] = Field(default_factory=list)  # in the reply's cause names or message
    message_must_contain: list[str] = Field(default_factory=list)
    message_must_not_contain: list[str] = Field(default_factory=list)
    warnings_must_contain: list[str] = Field(default_factory=list)
    evidence_types: list[EvidenceType] = Field(default_factory=list)  # at least these are cited
    evidence_must_not_include: list[EvidenceType] = Field(default_factory=list)
    # Codes read from an attached scan report. Compared as an exact set: none missed, none invented.
    report_codes: list[str] | None = None
    # Diagram images attached to the reply, by diagram id. None = not checked; [] = none may be sent.
    diagrams_sent: list[str] | None = None

    @model_validator(mode="after")
    def _one_way_to_state_the_label(self) -> TurnCheck:
        if self.label and self.label_any_of:
            raise ValueError("give label or label_any_of, not both")
        return self


class Turn(_Strict):
    mechanic: str  # exactly as the mechanic wrote it
    attachments: list[str] = Field(default_factory=list)
    reading: ReadingValue | None = None
    expect: dict[Condition, TurnCheck] = Field(default_factory=dict)  # checks on the reply to this turn
    note: str | None = None


class Expectation(_Strict):
    must_do: list[str]
    must_not: list[str]
    final_cause: str | None = None  # the known correct conclusion, if the scenario reaches one


class Scenario(_Strict):
    scenario_id: str
    group: Group
    language: Literal["ar", "en", "ur"]
    dialect: str | None = None
    vehicle: Vehicle
    codes: list[str]
    turns: list[Turn]
    related_case_id: str | None = None
    related_procedure_id: str | None = None
    expected: dict[Condition, Expectation]
    source: Source | None = None  # None = written by us for the test, not taken from a client or licensed file
    review: Review = Field(default_factory=Review)

    @model_validator(mode="after")
    def _conditions_fit_the_group(self) -> Scenario:
        want = {"case_in_kb", "case_held_out"} if self.group == "case" else {"standard"}
        if set(self.expected) != want:
            raise ValueError(f"a {self.group!r} scenario needs exactly the conditions {sorted(want)}")
        for i, turn in enumerate(self.turns):
            extra = set(turn.expect) - want
            if extra:
                raise ValueError(f"turn {i + 1}: conditions {sorted(extra)} do not belong to a {self.group!r} scenario")
        return self

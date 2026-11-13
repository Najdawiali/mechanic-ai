"""An evaluation scenario: a scripted mechanic, and what the assistant must and must not do.

The same scenario is judged under two knowledge-base conditions, because a solved case
can be both a KB record and a test, and those must not be confused:

  case_held_out  the matching case is NOT in the KB  -> the assistant must not invent the fix
  case_in_kb     the matching case IS in the KB      -> the assistant must offer it as a hint
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.kb.schemas import Review, Source, Vehicle


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Turn(_Strict):
    mechanic: str  # exactly as the mechanic wrote it
    attachments: list[str] = Field(default_factory=list)
    note: str | None = None


class Expectation(_Strict):
    must_do: list[str]
    must_not: list[str]
    final_cause: str | None = None  # the known correct conclusion, if the scenario reaches one


class Scenario(_Strict):
    scenario_id: str
    language: Literal["ar", "en", "ur"]
    dialect: str | None = None
    vehicle: Vehicle
    codes: list[str]
    turns: list[Turn]
    related_case_id: str | None = None
    expected: dict[Literal["case_held_out", "case_in_kb"], Expectation]
    source: Source
    review: Review = Field(default_factory=Review)

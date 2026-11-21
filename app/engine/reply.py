"""The reply the AI service returns on every turn (handoff 7.3, the API contract).

The app shows `message`; everything else is structured, so the app can render a step, the sources
and the pictures without parsing text. `tier` is the answer policy's label:

  verified  every specific fact in the reply comes from a record in the knowledge base, cited in `evidence`
  general   general knowledge, labelled as not verified for this car
  web       no verified data: the reply comes from web pages, each one linked in `evidence` with its fit for
            this car ("fits" or "check"); never verified, never the source of a value (project doc web-search-design.md)
  none      no basis: the reply asks a question, or says there is no verified information
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.kb.schemas import NormalRange, TestPoint

ReplyType = Literal["question", "step", "conclusion", "escalation", "emergency"]
Tier = Literal["verified", "web", "general", "none"]
EvidenceType = Literal["procedure", "diagram", "case", "expert_note", "external", "web"]


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExpectedInput(_Out):
    kind: Literal["resistance", "voltage", "current"]
    unit: str


class StepView(_Out):
    step_id: str
    number: int
    of: int | None = None  # None = the walk may branch, so the total is not known
    instruction: str  # what this step checks, in the source's words
    measure: str | None = None  # "A pin 1 <-> B pin 2"
    test_points: list[TestPoint] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)  # the preparation, in order
    normal: NormalRange | None = None
    normal_text: str | None = None  # as the source prints it
    expected_input: ExpectedInput | None = None
    requires_tool: str | None = None
    basis: Literal["manufacturer", "general_practice"] = "manufacturer"


class Cause(_Out):
    name: str
    likelihood: Literal["high", "medium", "low"]
    evidence_ids: list[str] = Field(default_factory=list)


class Evidence(_Out):
    id: str
    type: EvidenceType
    title: str
    excerpt: str
    status: Literal["approved", "draft"] = "approved"  # draft only when the service reads drafts (evaluation)
    url: str | None = None  # web: the page, shown to the mechanic as a link (credit to the publisher)
    applies: Literal["fits", "check"] | None = None  # web: "check" = confirm the page is for this car (year, engine)


class DiagramPage(_Out):
    id: str  # the provider's diagram id printed on the page
    record_key: str
    title: str
    page: str
    image_key: str | None = None  # where the image is in the shared storage


class QuestionView(_Out):
    kind: Literal["version", "engine", "year", "codes", "component", "other"]
    options: list[str] = Field(default_factory=list)


class Reply(_Out):
    type: ReplyType
    language: Literal["en", "ar", "ur"]
    message: str
    step: StepView | None = None
    question: QuestionView | None = None
    possible_causes: list[Cause] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    diagrams: list[DiagramPage] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "low"
    tier: Tier
    report_codes: list[str] | None = None  # codes read from an attached scan report, to be confirmed by the mechanic
    session_status: Literal["open", "closed"] = "open"

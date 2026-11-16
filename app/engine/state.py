"""The AI's working state for one session (handoff 7.2). The backend never reads it.

Stored as JSON in ai.session.state and loaded at the start of every turn, so the service holds
nothing in memory between turns and any instance can answer the next one.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.engine.reply import QuestionView
from app.kb.schemas import Step, Vehicle
from app.lang.script import Language


class _State(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Walk(_State):
    """A list of linked steps being worked through: a manufacturer procedure or diagram checks."""

    kind: Literal["procedure", "circuit"]
    evidence_id: str  # "proc:..." or "diagram:...#COMPONENT/PIN"
    record_id: int | None = None
    title: str
    steps: list[Step]  # a copy: the walk goes on even if the record is replaced in the meantime
    when_all_normal: str | None = None
    step_id: str
    done: list[str] = Field(default_factory=list)  # step ids answered, in order
    cautions: list[str] = Field(default_factory=list)
    basis: Literal["manufacturer", "general_practice"] = "manufacturer"
    status: Literal["approved", "draft"] = "approved"
    extra: dict = Field(default_factory=dict)  # procedure only: confirmation lines, warnings


class ReadingLog(_State):
    walk: str  # evidence id of the walk
    step_id: str
    value: float | None = None
    unit: str | None = None
    over_limit: bool = False
    verdict: Literal["normal", "abnormal"]


class Pending(_State):
    """A question the assistant asked; the next message is read as its answer first."""

    question: QuestionView
    about: str  # what the answer unlocks: "procedure", "wiring"
    context: dict = Field(default_factory=dict)


class Turn(_State):
    role: Literal["mechanic", "assistant"]
    text: str
    english: str | None = None  # the mechanic's message rewritten in English by intake, for search and the summary


class Offered(_State):
    record_id: int
    key: str  # the evidence id
    type: Literal["case", "expert_note"]


class WiringContext(_State):
    """The last wiring answer: what "send me the diagram" and "how do I check it" refer to."""

    components: list[str] = Field(default_factory=list)
    pins: list[tuple[str, str]] = Field(default_factory=list)  # (component, pin)
    pages: list[tuple[int, str]] = Field(default_factory=list)  # (record id, page)
    power_and_ground: bool = False


class SessionState(_State):
    vehicle: Vehicle
    language: Language = "en"
    codes: list[str] = Field(default_factory=list)  # as the mechanic or the report gave them
    symptoms: str | None = None
    emergency: bool = False
    walk: Walk | None = None
    readings: list[ReadingLog] = Field(default_factory=list)
    shown_notices: list[str] = Field(default_factory=list)
    pending: Pending | None = None
    cited: list[str] = Field(default_factory=list)  # every evidence id shown in the session
    offered: list[Offered] = Field(default_factory=list)  # cases and expert notes shown as hints
    wiring: WiringContext | None = None
    version_answer: str | None = None  # which drawn version the mechanic said his car is
    conclusion: str | None = None
    confirmed_fix: str | None = None
    repairs: list[str] = Field(default_factory=list)  # every repair the mechanic reported, in order, the fixing one last
    history: list[Turn] = Field(default_factory=list)
    closed: bool = False

    HISTORY_KEPT: ClassVar[int] = 12

    def remember(self, role: Literal["mechanic", "assistant"], text: str, english: str | None = None) -> None:
        self.history.append(Turn(role=role, text=text, english=english))
        del self.history[:-self.HISTORY_KEPT]

    def cite(self, *ids: str) -> None:
        for evidence_id in ids:
            if evidence_id not in self.cited:
                self.cited.append(evidence_id)

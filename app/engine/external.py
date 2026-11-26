"""The slot for an outside source of diagnostic data: web search now (app/web), a licensed provider later.

The client holds wiring diagrams, but troubleshooting procedures for only a few codes. The research
(project doc `external-diagnostic-data-sources.md`) found no public API or MCP server with current,
licensed manufacturer procedures; a licensed provider (HaynesPro, Autodata, MotorData, ALLDATA
Connect or MOTOR) is the client's decision and contract. The engine is built against this interface
so a source can be plugged in without changing the engine. Settings: EXTERNAL_SOURCE=off | web.

What a source returns is evidence, never a final answer: the engine shows it in its own tier, with
the source named and linked, and the validator treats it as untrusted (project doc web-search-design.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from app.engine.model import TraceEntry
from app.kb.schemas import Vehicle

if TYPE_CHECKING:
    from app.config import Settings


@dataclass(frozen=True)
class ExternalItem:
    id: str  # "web:<hash of the url>", later "ext:<provider>:<their id>"
    provider: str
    title: str
    text: str  # what may be shown and cited
    url: str | None = None
    kind: str = "fault_code"  # web | fault_code | component_test | pin_data | known_fix
    extra: dict = field(default_factory=dict)  # web: applies ("fits" | "check"), reasons, code, domain


@dataclass
class ExternalLookup:
    items: list[ExternalItem] = field(default_factory=list)
    calls: list[TraceEntry] = field(default_factory=list)  # every provider call, for the session trace
    searched: int = 0  # how many searches this lookup spent (counted against the session's limit)


class ExternalSource(Protocol):
    name: str
    searches_per_session: int  # 0 = never look anything up
    codes_per_lookup: int

    def lookup(self, vehicle: Vehicle, codes: list[str], question: str) -> ExternalLookup: ...


class NoExternalSource:
    """The default: no outside source is connected."""

    name = "off"
    searches_per_session = 0
    codes_per_lookup = 0

    def lookup(self, vehicle: Vehicle, codes: list[str], question: str) -> ExternalLookup:
        return ExternalLookup()


def from_settings(settings: Settings) -> ExternalSource:
    name = (settings.external_source or "off").strip().lower()
    if name == "off":
        return NoExternalSource()
    if name == "web":
        from app.web.source import WebSource

        return WebSource.from_settings(settings)
    raise ValueError(f"external source {name!r} is not available (use off or web)")

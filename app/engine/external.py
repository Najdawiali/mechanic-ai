"""The slot for an outside source of diagnostic data: a licensed repair-data provider, later web search.

The client holds wiring diagrams, but troubleshooting procedures for only a few codes. The research
(project doc `external-diagnostic-data-sources.md`) found no public API or MCP server with current,
licensed manufacturer procedures; a licensed provider (HaynesPro, Autodata, MotorData, ALLDATA
Connect or MOTOR) is the client's decision and contract. The engine is built against this interface
so a provider can be plugged in without changing the engine. Until then it is off.

What a provider adapter must return is evidence, never a final answer: the engine shows it in its
own tier, "licensed external source", between the client's verified records and general knowledge,
with the provider named, and the validator treats it like any other source.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from app.kb.schemas import Vehicle


@dataclass(frozen=True)
class ExternalItem:
    id: str  # "ext:<provider>:<their id>"
    provider: str
    title: str
    text: str  # what may be shown and cited
    url: str | None = None
    kind: str = "fault_code"  # fault_code | component_test | pin_data | known_fix
    extra: dict = field(default_factory=dict)


class ExternalSource(Protocol):
    name: str

    def lookup(self, vehicle: Vehicle, codes: list[str], question: str) -> list[ExternalItem]: ...


class NoExternalSource:
    """The default: no outside source is connected."""

    name = "off"

    def lookup(self, vehicle: Vehicle, codes: list[str], question: str) -> list[ExternalItem]:
        return []


def from_settings(name: str) -> ExternalSource:
    if name in ("", "off", None):
        return NoExternalSource()
    raise ValueError(f"external source {name!r} is not available: no provider adapter is installed")

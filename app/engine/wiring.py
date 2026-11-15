"""Answers about wiring, from the diagram's stored pins and routes. No model writes these facts.

  between(A, B)        the pins of A whose wire reaches B, with each route as stored
  power_and_ground(A)  the pins of A fed from a fuse, and the pins of A that go to a ground point

A diagram can draw the same pin twice, for two versions of the car ("1 = EXCEPT 1.5L TURBO CVT",
"2 = 1.5L TURBO CVT"). When the matching routes are drawn for more than one version and the
mechanic has not said which his car is, every version is listed and he is asked; the routes are
never merged and one is never picked for him ("don't mix: each car has its own specification").
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.engine.circuit_plan import plans_for_pin
from app.kb.keys import norm_key
from app.kb.store import PinHit

_MEANING_SEP = " = "


@dataclass
class Line:
    pin: PinHit
    route: dict
    versions: tuple[str, ...]  # the meanings ("1.5L TURBO CVT INTEGRA"), not the marker digits
    text: str


@dataclass
class Answer:
    lines: list[Line] = field(default_factory=list)
    versions: list[str] = field(default_factory=list)  # distinct versions among the lines
    pages: list[tuple[int, str]] = field(default_factory=list)  # (record id, page) the lines come from

    @property
    def needs_version(self) -> bool:
        return len(self.versions) > 1


def meaning(marker: str) -> str:
    return marker.split(_MEANING_SEP, 1)[1].strip() if _MEANING_SEP in marker else marker.strip()


def _versions(pin: PinHit, route: dict) -> tuple[str, ...]:
    found = [pin.version] if pin.version else []
    found += [h.get("name") for h in route["hops"] if h.get("kind") == "version" and h.get("name")]
    return tuple(dict.fromkeys(meaning(v) for v in found))


def _ends_at(route: dict, component: str) -> bool:
    last = route["hops"][-1] if route["hops"] else {}
    return last.get("kind") == "pin" and norm_key(last.get("name")) == norm_key(component)


def _collect(lines: list[Line]) -> Answer:
    answer = Answer(lines=lines)
    for line in lines:
        for v in line.versions:
            if v not in answer.versions:
                answer.versions.append(v)
        page = (line.pin.record_id, line.pin.page)
        if page not in answer.pages:
            answer.pages.append(page)
    return answer


def _text(pin: PinHit, route: dict, versions: tuple[str, ...]) -> str:
    drawn = f" [drawn for: {', '.join(versions)}]" if versions else ""
    unfinished = " (the diagram reader could not follow this wire to its end)" if not route.get("complete") else ""
    return f"{route['text']}{drawn}{unfinished} (page {pin.page})"


def between(pins: list[PinHit], other: str) -> Answer:
    lines = [Line(p, r, _versions(p, r), _text(p, r, _versions(p, r))) for p in pins for r in p.routes if _ends_at(r, other)]
    return _collect(lines)


def power_and_ground(pins: list[PinHit]) -> Answer:
    lines = []
    for pin in pins:
        plans = plans_for_pin(pin)
        for route, plan in zip(pin.routes, plans, strict=True):
            if plan.role in ("feed", "ground"):
                versions = _versions(pin, route)
                what = "power from" if plan.role == "feed" else "ground"
                lines.append(Line(pin, route, versions,
                                  f"{pin.component} pin {pin.pin} ({pin.wire_colour or 'colour not printed'}): {what} {plan.goes_to}. "
                                  f"Route: {_text(pin, route, versions)}"))
    return _collect(lines)


def for_version(answer: Answer, version: str) -> Answer:
    """Only the lines drawn for this version, and the lines drawn for every version."""
    return _collect([line for line in answer.lines if not line.versions or version in line.versions])


def text(answer: Answer, heading: str) -> str:
    if not answer.versions or not answer.needs_version:
        return "\n".join([heading, *(f"- {line.text}" for line in answer.lines)])
    out = [heading]
    for version in answer.versions:
        out.append(f"If the car is {version}:")
        out += [f"- {line.text}" for line in answer.lines if version in line.versions]
    common = [line for line in answer.lines if not line.versions]
    if common:
        out.append("On every version:")
        out += [f"- {line.text}" for line in common]
    return "\n".join(out)

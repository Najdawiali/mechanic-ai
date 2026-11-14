"""The electrical checks for one wire of a component, worked out from the wiring diagram.

Most fault codes reach us with no manufacturer procedure. The diagram still says what each pin
of a component is joined to: a fuse, a ground point, a pin of another component, or another
system. From that, the checks an auto electrician makes follow by rule, with no model:

    a wire to a ground point   -> is the path to ground sound?
    a wire to another pin      -> is the wire unbroken? is it free of a short to ground?
    a wire from a fuse         -> is the fuse sound, and the wire unbroken?
    a wire to another system   -> nothing can be measured from this diagram; say which one is needed

Two limits are used, and only these: a sound wire reads below 1 ohm from end to end, and a wire
with no short reads above 1 megohm to ground. They are general electrical practice. They are the
figures the one manufacturer procedure we hold (Acura P0010) uses for the same two checks, which
is evidence, not proof, that they suit every car: THE CLIENT'S EXPERT MUST CONFIRM THEM. Every
plan says that its limits are general practice and not the manufacturer's. Nothing here states a
component's own resistance, a signal voltage or a supply voltage: those need a published value.

A check is offered only where the diagram proves the reading means what the plan says it means.
The short-to-ground check needs a wire that stands alone once its ends are unplugged. It is left
out, with the reason given, for a supply wire (the fuse feeds other units), for a wire that
passes a junction connector (which joins it to wires the reader does not follow), and for a wire
with a branch that cannot be unplugged at a known pin. A wrong "shorted to ground" sends a
mechanic into a sound harness, so no check is better than a doubtful one.

The steps are ordinary `Step` records, so a reading is judged in code by the same walker that
judges a manufacturer's procedure (app/engine/procedure_walk.walk).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from app.kb.schemas import Measure, NormalRange, Step, TestPoint

WIRE_IS_SOUND = NormalRange(op="lt", max=1, unit="ohm")
NO_SHORT_TO_GROUND = NormalRange(op="gt", min=1, unit="Mohm")
BASIS = ("General electrical practice: a sound wire reads below 1 ohm end to end, and a wire with no short reads above "
         "1 megohm to ground. These are not this manufacturer's published values.")
MARKED = re.compile(r"^(?P<name>.+?) \[(?P<marker>.+)\]$")  # "C303 [1 = EXCEPT 1.5L TURBO CVT INTEGRA]"

Role = Literal["ground", "feed", "to_component", "to_system", "unknown"]


@dataclass(frozen=True)
class CircuitPlan:
    component: str
    pin: str
    role: Role
    goes_to: str  # the far end in words: "ground G020", "PCM pin A49", "FUSE 28 10A at UNDER-DASH FUSE/RELAY BOX pin A24"
    steps: list[Step] = field(default_factory=list)
    when_all_normal: str | None = None  # what the readings prove when every step is normal
    cannot_check: str | None = None  # why there are no steps, in words for the mechanic
    not_covered: str | None = None  # a check that was left out, and why
    cautions: tuple[str, ...] = ()  # what the mechanic must confirm on the car
    applies_to: tuple[str, ...] = ()  # version markers this route is drawn under; empty = every version
    route: str = ""  # the route as stored, to cite
    basis: str = BASIS


def _f(record: Any, name: str):
    return record.get(name) if isinstance(record, dict) else getattr(record, name)


def _hops(route: Any) -> list[tuple[str, str | None, str | None, str | None]]:
    return [(_f(h, "kind"), _f(h, "name"), _f(h, "pin"), _f(h, "detail")) for h in _f(route, "hops")]


def _is_fuse(detail: str | None) -> bool:
    return bool(detail) and detail.upper().startswith("FUSE")


def _pin_text(component: str, pin: str | None, colour: str | None) -> str:
    return f"{component} pin {pin or '?'} ({colour + ' wire' if colour else 'wire colour not printed'})"


def _end_in_words(hops: list) -> str:
    kind, name, pin, detail = hops[-1]
    match kind:
        case "ground":
            return f"ground {name}"
        case "system":
            return f"the {name}"
        case "pin" if pin:
            return f"{detail} at {name} pin {pin}" if _is_fuse(detail) else f"{name} pin {pin}"
        case "pin":
            return f"{name}, pin number not printed"
    return "an end the diagram reader could not follow"


def _are_alternatives(a: list, b: list) -> bool:
    """Two routes of one pin that part at a version brace are drawn for different versions of the
    car: only one of them exists on any car. Routes that part anywhere else are branches of one
    wire, and both exist."""
    for x, y in zip(a, b):
        if x != y:
            return x[0] == "version" and y[0] == "version"
    return False


def _capital(text: str) -> str:
    return text[0].upper() + text[1:]


def plan_route(component: str, pin: str, route: Any, *, version: str | None = None, branches: tuple = ()) -> CircuitPlan:
    """The checks for one stored route of one pin. `route` is a Route record or its stored dict;
    `branches` are the other routes of the same pin that exist on the same car."""
    hops = _hops(route)
    wires = [name for kind, name, _, _ in hops if kind == "wire"]
    markers = tuple(dict.fromkeys([*([version] if version else []), *(name for kind, name, _, _ in hops if kind == "version")]))
    common = {"component": component, "pin": pin, "applies_to": markers, "route": _f(route, "text") or ""}

    if not hops or not _f(route, "complete") or hops[-1][0] == "open":
        return CircuitPlan(role="unknown", goes_to="not read from the diagram", **common,
                           cannot_check="The diagram reader could not follow this wire to its end, so no check is offered for it.")
    goes_to = _end_in_words(hops)
    if any(kind == "pin" and not number for kind, _, number, _ in hops):
        return CircuitPlan(role="unknown", goes_to=goes_to, **common,
                           cannot_check="The diagram prints no pin number at the far end of this wire, so the test point cannot be named.")
    last_kind, far_name, far_pin, far_detail = hops[-1]
    if last_kind == "system":
        return CircuitPlan(role="to_system", goes_to=goes_to, **common,
                           cannot_check=f"This wire continues in the {far_name}. Its far end is not on this diagram: "
                                        "the wiring diagram of that system is needed.")

    # the connectors the wire passes, in order: where to look when a reading is abnormal
    passed, cautions = [], []
    for kind, name, number, _ in hops[1:-1]:
        if kind not in ("connector", "junction_connector"):
            continue
        marked = MARKED.match(name or "")
        if marked and marked["marker"] not in markers:
            cautions.append(f"The diagram marks connector {marked['name']} with \"{marked['marker']}\". Confirm on the car that it is fitted.")
        passed.append(f"connector {name} pin {number or '?'}")
    look_at = f" Inspect, in this order: {', '.join(passed)}." if passed else ""
    if any(kind == "wire" and detail == "dashed" for kind, _, _, detail in hops):
        cautions.append("The diagram draws this wire dashed: it is fitted on some versions only. Confirm on the car that it is fitted.")

    here = _pin_text(component, pin, wires[0] if wires else None)
    probe = TestPoint(label=here, connector=component, pin=pin)
    ground = TestPoint(label="body ground", ground=True)
    near_fuse = hops[0][3] if _is_fuse(hops[0][3]) else None

    def unplug(name: str, fuse: str | None) -> str:
        return f"{fuse} removed from {name} (check first that it is not blown)" if fuse else f"{name} connector: disconnected"

    if last_kind == "ground":
        step = Step(id="1", check=f"Ground path of {component} pin {pin}", prepare=["Ignition OFF", unplug(component, near_fuse)],
                    measure=Measure(**{"from": probe, "to": ground}), normal=WIRE_IS_SOUND,
                    if_abnormal=f"The ground path from {here} to ground point {far_name} is open or has high resistance."
                                f"{look_at} Then inspect ground point {far_name} itself.")
        return CircuitPlan(role="ground", goes_to=goes_to, steps=[step], cautions=tuple(cautions), **common,
                           when_all_normal=f"The ground path from {here} to ground point {far_name} is sound.")

    # the far end is a pin of another component, or a terminal of a fuse box
    far_fuse = far_detail if _is_fuse(far_detail) else None
    is_feed = bool(near_fuse or far_fuse)
    there = _pin_text(far_name, far_pin, wires[-1] if wires else None)
    between = f"the wire between {here} and {there}"
    prepare = ["Ignition OFF", unplug(component, near_fuse), unplug(far_name, far_fuse)]

    # may the wire be tested for a short to ground? only if it stands alone once its ends are unplugged
    why_not, also_unplug = [], []
    if is_feed:
        why_not.append("it is a supply wire, and the same fuse may feed other units. A short to ground on a supply wire shows as a blown fuse")
    junctions = list(dict.fromkeys(name for kind, name, _, _ in hops if kind == "junction_connector"))
    if junctions and not is_feed:
        why_not.append(f"it passes junction connector {', '.join(junctions)}, which joins it to other wires")
    for other in branches:
        other_hops = _hops(other)
        end_kind, end_name, end_pin, end_detail = other_hops[-1] if other_hops else ("open", None, None, None)
        alone = (_f(other, "complete") and end_kind == "pin" and end_pin and not _is_fuse(end_detail)
                 and not any(kind == "junction_connector" for kind, *_ in other_hops))
        if alone:
            also_unplug.append(unplug(end_name, None))
        elif not is_feed:
            why_not.append(f"it also runs to {_end_in_words(other_hops) if other_hops else 'an end the diagram reader could not follow'}")

    steps = [Step(id="1", check=f"Open circuit check of {between}", prepare=prepare, measure=Measure(**{"from": probe, "to": TestPoint(label=there, connector=far_name, pin=far_pin)}),
                  normal=WIRE_IS_SOUND, if_abnormal=f"There is an open circuit or high resistance in {between}.{look_at}",
                  if_normal=None if why_not else "2")]
    if why_not:
        not_covered = f"No short-to-ground check is offered for this wire: {'; '.join(why_not)}."
        proven = f"{_capital(between)} is unbroken from end to end. A short to ground was not checked."
    else:
        not_covered = None
        proven = f"{_capital(between)} is sound: no open circuit and no short to ground."
        steps.append(Step(id="2", check=f"Short to ground check of {between}", prepare=[*prepare, *dict.fromkeys(also_unplug)],
                          measure=Measure(**{"from": probe, "to": ground}), normal=NO_SHORT_TO_GROUND,
                          if_abnormal=f"{_capital(between)} is shorted to ground.{look_at}"))
    return CircuitPlan(role="feed" if is_feed else "to_component", goes_to=goes_to, steps=steps, when_all_normal=proven,
                       not_covered=not_covered, cautions=tuple(cautions), **common)


def plans_for_pin(pin: Any) -> list[CircuitPlan]:
    """One plan per route of a stored pin (a `PinHit` from the knowledge base, or a `DiagramPin`)."""
    routes = list(_f(pin, "routes"))
    hops = [_hops(route) for route in routes]
    return [plan_route(_f(pin, "component"), _f(pin, "pin"), route, version=_f(pin, "version"),
                       branches=tuple(other for j, other in enumerate(routes) if j != i and not _are_alternatives(hops[i], hops[j])))
            for i, route in enumerate(routes)]

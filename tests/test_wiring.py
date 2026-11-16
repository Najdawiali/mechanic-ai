"""Wiring answers from stored routes: versions, marked connectors, power and ground."""

from app.engine import wiring
from app.kb.store import PinHit

EXCEPT, CVT = "1 = EXCEPT 1.5L TURBO CVT INTEGRA", "2 = 1.5L TURBO CVT INTEGRA"


def hit(pin, colour, *routes):
    return PinHit(record_id=1, diagram_key="diagram:x", diagram_title="Anti-Lock Brakes", verdict="traced", component="VSA UNIT",
                  pin=pin, wire_colour=colour, page="1 of 3", fuse=None, routes=list(routes))


def route(*items):
    hops = [dict(zip(("kind", "name", "pin", "detail"), item)) for item in items]
    return {"hops": hops, "text": " ".join(str(h["name"]) for h in hops), "complete": True}


# the left rear sensor: same VSA pins and colours on both versions, a different connector at the sensor end
LR = [hit("31", "RED",
          route(("pin", "VSA UNIT", "31"), ("wire", "RED"), ("connector", "C224", "8"), ("version", EXCEPT), ("connector", "C229", "4"), ("pin", "LR WSS", "1")),
          route(("pin", "VSA UNIT", "31"), ("wire", "RED"), ("connector", "C224", "8"), ("version", CVT), ("connector", "C223", "3"), ("pin", "LR WSS", "1"))),
      hit("30", "YEL",
          route(("pin", "VSA UNIT", "30"), ("wire", "YEL"), ("connector", "C224", "13"), ("version", EXCEPT), ("connector", "C229", "5"), ("pin", "LR WSS", "2")),
          route(("pin", "VSA UNIT", "30"), ("wire", "YEL"), ("connector", "C224", "13"), ("version", CVT), ("connector", "C223", "4"), ("pin", "LR WSS", "2")))]


def test_two_versions_are_kept_apart_and_named_by_their_meaning():
    answer = wiring.between(LR, "LR WSS")
    assert answer.needs_version and answer.versions == ["EXCEPT 1.5L TURBO CVT INTEGRA", "1.5L TURBO CVT INTEGRA"]
    only_cvt = wiring.for_version(answer, "1.5L TURBO CVT INTEGRA")
    assert all("C223" in line.text and "C229" not in line.text for line in only_cvt.lines)
    text = wiring.text(answer, "VSA UNIT to LR WSS:")
    assert text.index("If the car is EXCEPT") < text.index("C229") < text.index("If the car is 1.5L") < text.index("C223")


def test_same_pins_and_colours_at_the_first_part_on_every_version_is_detected():
    assert wiring.same_near_end(wiring.between(LR, "LR WSS"))
    other = [hit("31", "RED", route(("pin", "VSA UNIT", "31"), ("wire", "RED"), ("version", EXCEPT), ("pin", "LR WSS", "1"))),
             hit("40", "GRN", route(("pin", "VSA UNIT", "40"), ("wire", "GRN"), ("version", CVT), ("pin", "LR WSS", "1")))]
    assert not wiring.same_near_end(wiring.between(other, "LR WSS"))


def test_a_connector_marked_for_another_version_is_flagged_on_the_route():
    drawn = hit("33", "BRN", route(("pin", "VSA UNIT", "33"), ("wire", "BRN"), ("connector", f"C304 [{EXCEPT}]", "2"),
                                   ("version", CVT), ("wire", "BRN"), ("pin", "LF WSS", "1")))
    (line,) = wiring.between([drawn], "LF WSS").lines
    assert f'connector C304 is marked "{EXCEPT}": confirm on the car that it is fitted' in line.text


def test_power_and_ground_name_the_fuse_and_the_ground_point_once():
    pins = [hit("1", "WHT", route(("pin", "VSA UNIT", "1"), ("wire", "WHT"), ("pin", "UNDER-HOOD FUSE/RELAY BOX", "A11", "FUSE 5-5 40A"))),
            hit("3", "BLK", route(("pin", "VSA UNIT", "3"), ("wire", "BLK"), ("ground", "G020")))]
    feed, ground = wiring.power_and_ground(pins).lines
    assert "power from FUSE 5-5 40A at UNDER-HOOD FUSE/RELAY BOX pin A11" in feed.text
    assert "VSA UNIT pin 3 (BLK): to ground G020." in ground.text

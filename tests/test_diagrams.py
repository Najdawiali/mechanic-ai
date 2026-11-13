"""The diagram reader, on a small diagram drawn here with the same conventions as the real ones.

The licensed diagrams are not in the repository, so these tests draw their own: coloured wires
of the real width, a connector gap, a junction dot, component boxes, a ground and a page join.
`scripts/score_diagrams.py` checks the reader against the real files.
"""

from __future__ import annotations

import pymupdf
import pytest

from app.ingest.diagrams import COMPONENT_FILL, assess, is_complete, pins_of, read_pdf, trace

RED, BLUE, GREEN = (1, 0, 0), (0, 0, 1), (0.16, 0.65, 0)


def text(page, x, y, words):
    page.insert_text((x, y), words, fontsize=5)


def wire(page, points, colour):
    shape = page.new_shape()
    shape.draw_polyline(points)
    shape.finish(color=colour, width=0.91, closePath=False)
    shape.commit()


def box(page, rect, name):
    page.draw_rect(rect, color=None, fill=COMPONENT_FILL)
    text(page, rect[0], rect[3] + 6, name)


def new_page(doc, number, of):
    page = doc.new_page(width=612, height=792)
    text(page, 28, 40, "2025 Test Car L4-1.5L Turbo (T15)")
    text(page, 28, 55, "Vehicle > Brakes > Diagrams >")
    text(page, 28, 90, f"Test Diagram - Page {number} of {of}")
    page.draw_rect((29, 101, 555, 754), color=(0, 0, 0), width=0.87)
    return page


def build(mislabel: bool = False) -> bytes:
    doc = pymupdf.open()
    page = new_page(doc, 1, 2)
    box(page, (60, 200, 100, 300), "TEST UNIT")
    # pin 1: red to connector C224 pin 8, then blue to a junction; from there to SENSOR A and to ground
    wire(page, [(103, 220), (200, 220)], RED)
    text(page, 104, 218, "1")
    text(page, 130, 218, "BLU" if mislabel else "RED")
    text(page, 196, 218, "8")
    text(page, 196, 229, "C224")
    wire(page, [(202.4, 220), (300, 220)], BLUE)
    text(page, 240, 218, "BLU")
    page.draw_circle((303.4, 220), 0.75, color=(0, 0, 0), width=1.52)
    wire(page, [(306.8, 220), (400, 220)], BLUE)
    text(page, 350, 218, "BLU")
    text(page, 392, 218, "2")
    box(page, (403, 205, 440, 235), "SENSOR A")
    wire(page, [(303.4, 223.4), (303.4, 300)], BLUE)
    text(page, 308, 304, "G101")
    # pin 2: green, off the right edge of the page as join number 1
    wire(page, [(103, 240), (500, 240)], GREEN)
    text(page, 104, 238, "2")
    text(page, 150, 238, "GRN")
    text(page, 505, 242, "1")
    # a second red wire, so the reader can learn what red lines are called
    wire(page, [(103, 260), (180, 260), (180, 400)], RED)
    text(page, 104, 258, "3")
    text(page, 130, 258, "RED")
    if mislabel:
        text(page, 150, 258, "RED")
    box(page, (165, 403, 200, 430), "HORN")
    text(page, 174, 398, "1")

    page = new_page(doc, 2, 2)
    wire(page, [(40, 240), (300, 240)], GREEN)
    text(page, 31, 242, "1")
    text(page, 100, 238, "GRN")
    text(page, 292, 238, "1")
    box(page, (303, 225, 340, 255), "LAMP")
    return doc.tobytes()


@pytest.fixture(scope="module")
def diagram():
    return read_pdf(build())


def routes(diagram, component, pin) -> list[str]:
    return sorted(" ".join(str(hop) for hop in route) for route in trace(diagram, component, pin))


def test_the_header_is_read(diagram):
    assert diagram.vehicle_line == "2025 Test Car L4-1.5L Turbo (T15)"
    assert (diagram.system_path, diagram.title, diagram.page_labels) == (["Brakes", "Diagrams"], "Test Diagram", ["1 of 2", "2 of 2"])


def test_every_wire_end_is_attached_and_every_colour_agrees(diagram):
    assert diagram.problems == []
    assert all(wire.colour_agrees is not False for page in diagram.pages for wire in page.wires)


def test_pins_are_read_with_their_wire_colours(diagram):
    assert pins_of(diagram, "test unit") == [("1", "RED", "1 of 2"), ("2", "GRN", "1 of 2"), ("3", "RED", "1 of 2")]


def test_a_wire_is_followed_through_a_connector_and_a_junction(diagram):
    assert routes(diagram, "TEST UNIT", "1") == [
        "TEST UNIT pin 1 -RED-> C224 pin 8 -BLU-> -BLU-> SENSOR A pin 2",
        "TEST UNIT pin 1 -RED-> C224 pin 8 -BLU-> -BLU-> ground G101",
    ]


def test_a_wire_is_followed_onto_the_next_page(diagram):
    assert routes(diagram, "TEST UNIT", "2") == ["TEST UNIT pin 2 -GRN-> (page 2 of 2) -GRN-> LAMP pin 1"]


def test_a_route_can_be_followed_from_either_end(diagram):
    assert routes(diagram, "LAMP", "1") == ["LAMP pin 1 -GRN-> (page 1 of 2) -GRN-> TEST UNIT pin 2"]
    assert routes(diagram, "HORN", "1") == ["HORN pin 1 -RED-> TEST UNIT pin 3"]


def test_an_unknown_pin_has_no_route(diagram):
    assert trace(diagram, "TEST UNIT", "9") == [] and trace(diagram, "NO SUCH PART", "1") == []


def test_a_wire_whose_label_disagrees_with_its_drawn_colour_is_reported():
    problems = read_pdf(build(mislabel=True)).problems
    assert any("drawn RED, labelled BLU" in problem for problem in problems)


# -- the verdict: how far a file can be trusted, with no answer key ---------------------
def test_a_clean_diagram_is_trusted_for_pins_and_routes(diagram):
    quality = assess(diagram)
    assert (quality.verdict, quality.reasons, quality.unattached_ends, quality.colour_disagree) == ("traced", (), 0, 0)
    assert all(is_complete(route) for route in trace(diagram, "TEST UNIT", "1"))


def test_a_diagram_with_a_disagreeing_colour_is_only_partly_trusted():
    quality = assess(read_pdf(build(mislabel=True)))
    assert quality.verdict in ("partial", "show_only") and any("labelled another" in reason for reason in quality.reasons)


def test_a_file_this_reader_does_not_understand_is_stored_to_show_only():
    doc = pymupdf.open()
    page = new_page(doc, 1, 1)  # the header, and nothing drawn the way the reader knows
    page.draw_line((100, 300), (400, 300), color=(0, 0, 0), width=3)
    text(page, 100, 290, "A scanned or differently drawn diagram")
    quality = assess(read_pdf(doc.tobytes()))
    assert quality.verdict == "show_only" and "no wires were found" in quality.reasons[0]


def test_a_route_that_stops_at_an_unattached_end_is_marked_incomplete():
    doc = pymupdf.open()
    page = new_page(doc, 1, 1)
    box(page, (60, 200, 100, 300), "TEST UNIT")
    wire(page, [(103, 220), (250, 220)], RED)  # ends in mid-air
    text(page, 104, 218, "1")
    text(page, 130, 218, "RED")
    wire(page, [(103, 240), (400, 240)], RED)
    text(page, 104, 238, "2")
    text(page, 130, 238, "RED")
    box(page, (403, 225, 440, 255), "LAMP")
    diagram = read_pdf(doc.tobytes())
    assert [is_complete(route) for route in trace(diagram, "TEST UNIT", "1")] == [False]
    assert [is_complete(route) for route in trace(diagram, "TEST UNIT", "2")] == [True]
    assert assess(diagram).unattached_ends == 1

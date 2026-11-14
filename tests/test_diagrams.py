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


# -- arrows to other systems --------------------------------------------------------------------
def arrow(page, x, y, side=1):
    """An arrowhead at the end of a wire: its back at x, its tip pointing to the side."""
    shape = page.new_shape()
    shape.draw_polyline([(x, y - 2.3), (x + 4.1 * side, y), (x, y + 2.3)])
    shape.finish(color=(0, 0, 0), fill=(0, 0, 0), width=0.36, closePath=False)  # two lines, filled: as in the real files
    shape.commit()


def group_brace(page, x, top, bottom, side=1):
    """An upright brace beside several arrowheads; its point is on the far side from them."""
    middle = (top + bottom) / 2
    shape = page.new_shape()
    shape.draw_polyline([(x, top), (x + 1.7 * side, top + 1.5), (x + 1.7 * side, middle - 1.5), (x + 3.6 * side, middle),
                         (x + 1.7 * side, middle + 1.5), (x + 1.7 * side, bottom - 1.5), (x, bottom)])
    shape.finish(color=(0, 0, 0), width=0.36, closePath=False)
    shape.commit()


def to_another_system(page, pin: str, y: float, label: tuple[str, ...] = ()):
    """A red wire from a pin of TEST UNIT to an arrowhead, with the label printed beside the arrow."""
    wire(page, [(103, y), (150, y)], RED)
    text(page, 104, y - 2, pin)
    text(page, 120, y - 2, "RED")
    arrow(page, 150, y)
    for row, words in enumerate(label):
        text(page, 157, y + 2 + 5.5 * (row - (len(label) - 1) / 2), words)


def arrows_page():
    doc = pymupdf.open()
    page = new_page(doc, 1, 1)
    box(page, (60, 200, 100, 300), "TEST UNIT")
    to_another_system(page, "1", 211, ("DOOR LOCKS SYSTEM",))
    to_another_system(page, "2", 220, ("INTERIOR LIGHTS SYSTEM",))  # the next row: its own label, 9 pt lower
    to_another_system(page, "3", 232, ("WIPER/WASHER &", "HEADLIGHTS SYSTEMS"))  # one arrow, a label of two rows
    for pin, y in (("4", 250), ("5", 259), ("6", 268)):  # three arrows in a brace, one label at its point
        to_another_system(page, pin, y)
    group_brace(page, 154.5, 246, 272)
    text(page, 160, 258.25, "DOOR LOCKS &")
    text(page, 160, 263.75, "ANTI-THEFT SYSTEMS")
    to_another_system(page, "7", 277, ("SHIFT INTERLOCK SYSTEM",))  # just under the brace, outside it
    return doc, page


def test_an_arrow_is_named_by_the_text_on_its_own_row_or_by_its_brace():
    doc, _ = arrows_page()
    diagram = read_pdf(doc.tobytes())
    ends = {pin: trace(diagram, "TEST UNIT", pin)[0][-1].name for pin in "1234567"}
    assert ends == {"1": "DOOR LOCKS SYSTEM", "2": "INTERIOR LIGHTS SYSTEM", "3": "WIPER/WASHER & HEADLIGHTS SYSTEMS",
                    "4": "DOOR LOCKS & ANTI-THEFT SYSTEMS", "5": "DOOR LOCKS & ANTI-THEFT SYSTEMS",
                    "6": "DOOR LOCKS & ANTI-THEFT SYSTEMS", "7": "SHIFT INTERLOCK SYSTEM"}
    assert assess(diagram).verdict == "traced"


def test_an_arrow_with_no_label_is_not_given_its_neighbours_label():
    doc, page = arrows_page()
    to_another_system(page, "8", 286)  # 9 pt under pin 7's arrow, and nothing printed beside it
    diagram = read_pdf(doc.tobytes())
    (route,) = trace(diagram, "TEST UNIT", "8")
    assert route[-1].name == "?" and not is_complete(route)
    quality = assess(diagram)
    assert quality.verdict == "partial" and any("not named" in reason for reason in quality.reasons)


# -- pages ------------------------------------------------------------------------------------
def sheet(doc, title: str | None, title_y: float = 90):
    page = doc.new_page(width=612, height=792)
    text(page, 28, 40, "2025 Test Car L4-1.5L Turbo (T15)")
    text(page, 28, 55, "Vehicle > Brakes > Diagrams >")
    if title:
        text(page, 28, title_y, title)
    return page


def lamp_circuit(page):
    page.draw_rect((29, 101, 555, 754), color=(0, 0, 0), width=0.87)
    box(page, (60, 200, 100, 300), "TEST UNIT")
    wire(page, [(103, 220), (400, 220)], RED)
    text(page, 104, 218, "1")
    text(page, 130, 218, "RED")
    box(page, (403, 205, 440, 235), "LAMP")


def test_a_title_printed_on_a_sheet_of_its_own_belongs_to_the_drawing_on_the_next_sheet():
    doc = pymupdf.open()
    sheet(doc, "Test Diagram - Page 1 of 2", title_y=120)  # a long header pushed the title down and the drawing off the sheet
    lamp_circuit(sheet(doc, None))
    lamp_circuit(sheet(doc, "Test Diagram - Page 2 of 2"))
    diagram = read_pdf(doc.tobytes())
    assert diagram.page_labels == ["1 of 2", "2 of 2"] and diagram.title == "Test Diagram"
    assert assess(diagram).verdict == "traced"


def test_a_drawing_with_no_title_is_read_and_reported_never_dropped():
    doc = pymupdf.open()
    lamp_circuit(sheet(doc, None))
    lamp_circuit(sheet(doc, "Test Diagram - Page 2 of 2"))
    diagram = read_pdf(doc.tobytes())
    assert diagram.page_labels == ["?", "2 of 2"] and len(trace(diagram, "TEST UNIT", "1")) == 2
    quality = assess(diagram)
    assert quality.verdict == "partial" and any("2 page(s); 1 were read" in reason for reason in quality.reasons)


# -- component names --------------------------------------------------------------------------
def rows(page, x, y, *lines):
    for number, words in enumerate(lines):
        text(page, x, y + 5.5 * number, words)


def test_a_fuse_box_is_named_by_the_text_beside_it_not_by_a_part_inside_it():
    doc = pymupdf.open()
    page = new_page(doc, 1, 1)
    page.draw_rect((60, 150, 200, 190), color=None, fill=COMPONENT_FILL)
    rows(page, 70, 162, "FUSE", "11", "10A")
    rows(page, 120, 165, "RELAY", "CONTROL", "MODULE")  # a part inside the box
    rows(page, 203, 156, "UNDER-HOOD", "FUSE/RELAY", "BOX", "(LEFT SIDE OF", "ENGINE COMPT)")  # the box's name, beside it
    wire(page, [(75, 193), (75, 300)], RED)
    text(page, 65, 199, "A45")
    text(page, 70, 250, "RED")
    box(page, (60, 303, 100, 330), "LAMP")
    text(page, 70, 299, "1")
    diagram = read_pdf(doc.tobytes())
    names = {(component.name, component.location) for component in diagram.pages[0].components}
    assert ("UNDER-HOOD FUSE/RELAY BOX", "LEFT SIDE OF ENGINE COMPT") in names
    assert trace(diagram, "UNDER-HOOD FUSE/RELAY BOX", "A45")


def test_a_name_that_was_cut_short_makes_the_diagram_only_partly_trusted():
    doc = pymupdf.open()
    page = new_page(doc, 1, 1)
    box(page, (60, 200, 100, 300), "UNIT")
    wire(page, [(103, 220), (400, 220)], RED)
    text(page, 104, 218, "1")
    text(page, 130, 218, "RED")
    box(page, (403, 205, 440, 235), "LAMP")
    quality = assess(read_pdf(doc.tobytes()))
    assert quality.verdict == "partial"


# -- versions of the car ------------------------------------------------------------------------
def ringed(page, x, y, mark):
    """A version marker: a number inside a small ring."""
    page.draw_circle((x, y), 2.6, color=(0, 0, 0), width=0.36)
    text(page, x - 1.4, y + 1.7, mark)


def version_brace(page, left, right, y):
    """A brace with its point up: the wire above it continues as the wire under each half."""
    middle = (left + right) / 2
    shape = page.new_shape()
    shape.draw_polyline([(left, y + 4), (left + 2, y + 2), (middle - 2, y + 2), (middle, y), (middle + 2, y + 2),
                         (right - 2, y + 2), (right, y + 4)])
    shape.finish(color=(0, 0, 0), width=0.36, closePath=False)
    shape.commit()


def two_versions_page(doc, number, of, meaning_of_1, meaning_of_2):
    """TEST UNIT pin 1 goes to a speed sensor that is drawn twice: once for version 1, once for version 2."""
    page = new_page(doc, number, of)
    page.draw_rect((250, 150, 350, 180), color=None, fill=COMPONENT_FILL)
    text(page, 250, 146, "TEST UNIT")
    wire(page, [(300, 183), (300, 240)], RED)
    text(page, 294.5, 190, "1")
    text(page, 292, 215, "RED")
    version_brace(page, 260, 340, 244)
    ringed(page, 268, 238, "1")
    ringed(page, 332, 238, "2")
    for x, colour, name in ((275, RED, "RED"), (325, BLUE, "BLU")):
        wire(page, [(x, 251), (x, 330)], colour)
        text(page, x - 8, 290, name)
        text(page, x - 5.5, 328, "1")
        box(page, (x - 15, 333, x + 15, 360), "SPEED SENSOR")
    ringed(page, 60, 700, "1")
    text(page, 66, 701.7, meaning_of_1)
    ringed(page, 60, 712, "2")
    text(page, 66, 713.7, meaning_of_2)
    return page


def test_a_version_marker_means_what_the_legend_of_its_own_page_says():
    doc = pymupdf.open()
    two_versions_page(doc, 1, 2, "EXCEPT CVT", "CVT")
    two_versions_page(doc, 2, 2, "TYPE-S", "EXCEPT TYPE-S")  # the same markers, other cars
    diagram = read_pdf(doc.tobytes())
    assert [page.versions for page in diagram.pages] == [{"1": "EXCEPT CVT", "2": "CVT"}, {"1": "TYPE-S", "2": "EXCEPT TYPE-S"}]
    first, second = (sorted(" ".join(str(hop) for hop in route) for route in routes_of)
                     for routes_of in (trace_page(diagram, 0), trace_page(diagram, 1)))
    assert first == ["TEST UNIT pin 1 -RED-> [1 = EXCEPT CVT] -RED-> SPEED SENSOR pin 1",
                     "TEST UNIT pin 1 -RED-> [2 = CVT] -BLU-> SPEED SENSOR pin 1"]
    assert second == ["TEST UNIT pin 1 -RED-> [1 = TYPE-S] -RED-> SPEED SENSOR pin 1",
                      "TEST UNIT pin 1 -RED-> [2 = EXCEPT TYPE-S] -BLU-> SPEED SENSOR pin 1"]


def trace_page(diagram, number):
    from app.ingest.diagrams import trace_from

    page = diagram.pages[number]
    return [route for wi, w in enumerate(page.wires) for ei, end in enumerate(w.ends)
            if end.kind == "pin" and end.component == "TEST UNIT" for route in trace_from(diagram, number, wi, ei)]


def test_a_number_after_a_name_belongs_to_the_name_and_a_fitted_note_is_not_a_place():
    doc = pymupdf.open()
    page = new_page(doc, 1, 1)
    for x, name in ((60, "PRESSURE SENSOR 2"), (160, "PRESSURE SENSOR 1")):
        page.draw_rect((x, 150, x + 60, 180), color=None, fill=COMPONENT_FILL)
        text(page, x + 18, 135, "(M/T)")  # notes and name centred over the box, as on the real pages
        text(page, x + 8, 140.5, "(ON BOOSTER)")
        text(page, x, 146, name)
        wire(page, [(x + 20, 183), (x + 20, 300)], RED)
        text(page, x + 14.5, 190, "1")
        text(page, x + 12, 240, "RED")
    box(page, (60, 303, 240, 330), "CONTROL UNIT A")
    text(page, 74.5, 298, "7")
    text(page, 174.5, 298, "8")
    diagram = read_pdf(doc.tobytes())
    sensors = {(c.name, c.location, c.fitted) for c in diagram.pages[0].components if "SENSOR" in c.name}
    assert sensors == {("PRESSURE SENSOR 2", "ON BOOSTER", "M/T"), ("PRESSURE SENSOR 1", "ON BOOSTER", "M/T")}
    assert [str(route[-1]) for route in trace(diagram, "PRESSURE SENSOR 2", "1", exact=True)] == ["CONTROL UNIT A pin 7"]
    assert [str(route[-1]) for route in trace(diagram, "PRESSURE SENSOR 1", "1", exact=True)] == ["CONTROL UNIT A pin 8"]

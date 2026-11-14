"""A wiring-diagram PDF -> one WiringDiagram record with its pins and routes, and its page images.

The reading itself is in app/ingest/diagrams.py. This module decides what is stored:

  * the vehicle comes from the page header; what the uploader declared fills in a make or model
    of more than one word, which the header alone cannot split
  * every pin gets its routes worked out now, so answering later is a lookup, not an analysis
  * a diagram the reader does not understand is stored to be shown, with no pins (see `assess`)
"""

from __future__ import annotations

import re

import pymupdf

from app.ingest import diagrams
from app.ingest.extractors import Asset, Extracted, SourceDocument
from app.ingest.jobs import PermanentError
from app.kb.keys import norm_key
from app.kb.schemas import (
    DiagramComponent, DiagramPin, DiagramSheet, Review, Route, RouteHop, Source, Vehicle, WiringDiagram,
)

IMAGE_DPI = 120
# "2025 Acura Integra L4-1.5L Turbo (L15CA)": year, make, model, engine description, engine code
VEHICLE_LINE = re.compile(r"^(?P<year>(?:19|20)\d{2}) (?P<make>\S+) (?P<rest>.+?)(?: \((?P<code>[^()]+)\))?$")
ENGINE_PART = re.compile(r" (?:[A-Z]\d{1,2}-\d|EV\b|Electric\b).*$")


def vehicle_from(line: str, declared: dict) -> tuple[Vehicle, list[str]]:
    """The vehicle a diagram belongs to, and anything about it a reviewer should look at."""
    questions: list[str] = []
    header = VEHICLE_LINE.match(line or "")
    year = int(header["year"]) if header else None
    make = header["make"] if header else None
    model = ENGINE_PART.sub("", header["rest"]).strip() if header else None
    code = header["code"] if header else None
    if declared.get("make") and declared.get("model"):
        if make and (norm_key(declared["make"]) != norm_key(make) or norm_key(declared["model"]) != norm_key(model)):
            questions.append(f"The page header reads {line!r}; the uploader declared {declared['make']} {declared['model']}. "
                             "The declared make and model were used.")
        make, model = str(declared["make"]), str(declared["model"])
    if declared.get("year") and year and int(declared["year"]) != year:
        questions.append(f"The page header gives the year {year}; the uploader declared {declared['year']}. The header was used.")
    year = year or (int(declared["year"]) if declared.get("year") else None)
    if not (make and model and year):
        raise PermanentError("cannot tell which vehicle this diagram is for: no vehicle line in the header and no "
                             "make, model and year declared")
    return Vehicle(make=make, model=model, year=year, engine_code=code), questions


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def extract_diagram(document: SourceDocument) -> list[Extracted]:
    try:
        diagram = diagrams.read_pdf(document.data)
    except Exception as exc:  # noqa: BLE001  not a PDF, or damaged
        raise PermanentError(f"the file could not be read as a PDF: {exc}") from None
    quality = diagrams.assess(diagram)
    vehicle, questions = vehicle_from(diagram.vehicle_line, document.declared)
    title = diagram.title or document.original_name.rsplit(".", 1)[0]
    vehicle_slug = "-".join(_slug(str(part)) for part in (vehicle.make, vehicle.model, vehicle.year, vehicle.engine_code) if part)
    key = f"diagram:{vehicle_slug}:{diagram.diagram_ids[0] if diagram.diagram_ids else _slug(title)}"

    # page images, without the browser's print header and footer
    assets, sheets = [], []
    with pymupdf.open(stream=document.data, filetype="pdf") as pdf:
        for page, label, diagram_id in zip(diagram.pages, diagram.page_labels, diagram.diagram_ids):
            raw = pdf[page.number]
            clip = pymupdf.Rect(0, diagrams.MARGIN_TOP, raw.rect.width, raw.rect.height - diagrams.MARGIN_BOTTOM)
            image_key = f"derived/diagrams/{document.file_id}/{_slug(label)}.png"
            assets.append(Asset(image_key, raw.get_pixmap(dpi=IMAGE_DPI, clip=clip).tobytes("png"), "image/png"))
            sheets.append(DiagramSheet(page=label, diagram_id=diagram_id, image_key=image_key, versions=page.versions))

    components = {(c.name, c.location, c.fitted, label) for page, label in zip(diagram.pages, diagram.page_labels)
                  for c in page.components if c.name != "?"}
    # A marker can mean one car on one page and another on the next: only meanings every page agrees on are kept here.
    legends = [page.versions for page in diagram.pages]
    versions = {marker: meaning for legend in legends for marker, meaning in legend.items()
                if all(other.get(marker, meaning) == meaning for other in legends)}

    # One stored pin per wire end at a component: the pin, the colour of THAT wire and where it goes.
    pins: list[DiagramPin] = []
    if quality.verdict != "show_only":
        for pi, (page, label) in enumerate(zip(diagram.pages, diagram.page_labels)):
            in_box: dict[int | None, list[tuple[DiagramPin, set[str]]]] = {}
            for wi, wire in enumerate(page.wires):
                for ei, end in enumerate(wire.ends):
                    if end.kind != "pin" or not end.pin or not end.component or end.component == "?":
                        continue
                    traced = diagrams.trace_from(diagram, pi, wi, ei)
                    routes = [Route(hops=[RouteHop(kind=h.kind, name=h.name, pin=h.pin, detail=h.detail) for h in route],
                                    text=" ".join(str(h) for h in route), complete=diagrams.is_complete(route)) for route in traced]
                    pin = DiagramPin(component=end.component, pin=end.pin, wire_colour=wire.colour, page=label, fuse=end.fuse,
                                     routes=routes)
                    passed = {hop.name for route in traced for hop in route if hop.kind == "version" and hop.name}
                    in_box.setdefault(end.box, []).append((pin, passed))
                    pins.append(pin)
            # A page that draws a component twice draws it once per version of the car. A drawing whose
            # wires all lead through one version only is the drawing for that version, pins to ground included.
            names = [c.name for c in page.components]
            for box, members in in_box.items():
                if box is None or names.count(names[box]) < 2:
                    continue
                passed = set().union(*(versions_passed for _, versions_passed in members))
                if len(passed) == 1:
                    for pin, _ in members:
                        pin.version = next(iter(passed))

    # One pin with two different wires is either two drawings of one terminal or a slip in the diagram: a person decides.
    wires_at: dict[tuple, set] = {}
    for pin in pins:
        wires_at.setdefault((pin.component, pin.pin, pin.version), set()).add((pin.wire_colour or "no colour printed", pin.page))
    for (component, number, _), wires in sorted(wires_at.items(), key=str):
        if len({colour for colour, _ in wires}) > 1:
            drawn = " and ".join(f"{colour} on page {page}" for colour, page in sorted(wires))
            questions.append(f"Pin {number} of {component} is drawn with two different wires ({drawn}). Check both against the pages.")
    if quality.verdict != "traced":
        questions.append("The reader could not attach everything on this diagram: " + "; ".join(quality.reasons))
    record = WiringDiagram(
        diagram_key=key, vehicle=vehicle, system_path=diagram.system_path, title=title, sheets=sheets,
        components=[DiagramComponent(name=n, location=loc, fitted=fitted, page=label)
                    for n, loc, fitted, label in sorted(components, key=str)],
        versions=versions, pins=pins, verdict=quality.verdict, verdict_reasons=list(quality.reasons),
        source=Source(kind="licensed_diagram", file=document.original_name, licensed=True),
        review=Review(open_questions=questions),
    )
    return [Extracted(record, "code", tuple(assets))]

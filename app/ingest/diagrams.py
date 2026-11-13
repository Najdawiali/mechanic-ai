"""Wiring-diagram PDFs -> pins, wires and connections, read from the drawing itself.

The supplied diagrams are vector drawings with real text, so nothing here uses a model or
looks at pixels. A page is read as:

  wires        coloured lines of one width; a striped wire is two lines drawn on top of each other
  connectors   a small gap in a wire, with the pin number beside it and the connector id nearby
  junctions    a dot where wires meet
  components   light-blue boxes; a wire that ends at a box ends at one of its pins
  junction     a component named J/C: pins facing each other across the box are joined inside it
  connectors
  version      a brace: the wires at its point continue as the wires in its left half on one
  braces       version of the car and as the wires in its right half on another; each half is
               named by a ringed number or a short text printed at the brace
  dashed wires a wire fitted on some versions only, drawn as a row of filled dashes
  arrows       a wire ending in an arrowhead goes to another system, named beside the arrow
  page joins   a wire that leaves the page continues on the next page under the same number

Every wire is checked two ways: the colour it is drawn in must agree with the colour printed
beside it. A disagreement is reported, never hidden.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import pymupdf

# -- what the drawing looks like (measured on the supplied files) -------------------
WIRE_WIDTH = (0.85, 1.0)  # a plain wire
STRIPE_BASE_WIDTH = (0.55, 0.65)  # a striped wire: this line, with a thinner one on top
STRIPE_TOP_WIDTH = (0.25, 0.35)
JUNCTION_WIDTH = 1.52
JUNCTION_REACH = 4.5  # a wire stops this close to the centre of the dot it joins
DASH_THICKNESS = (0.6, 1.2)
COMPONENT_FILL = (0.89, 0.96, 0.99)
MARGIN_TOP, MARGIN_BOTTOM = 27, 30  # the browser's print header and footer
MAX_GAP = 11.0  # the widest connector symbol between two wire ends
MIN_WIRE_LENGTH = 4.0

_COLOUR = r"(?:LT |DK )?(?:BLK|WHT|RED|GRN|BLU|YEL|BRN|ORG|PNK|GRY|PPL|VIO|TAN)"
COLOUR_LABEL = re.compile(rf"^{_COLOUR}(?:/{_COLOUR})?$")
PIN = re.compile(r"^(?:\d{1,3}|[A-Z]{1,2}\d{1,2})$")  # 31, A11, C20
CONNECTOR_ID = re.compile(r"^C\d{3}$")
GROUND_ID = re.compile(r"^G\d{3}$")
PAGE_REF = re.compile(r"^\d{1,3}$")


@dataclass(frozen=True)
class Label:
    text: str
    x: float  # centre
    y: float
    rect: tuple[float, float, float, float]


@dataclass
class End:
    """One end of a wire, and what it is attached to."""

    xy: tuple[float, float]
    out: tuple[int, int]  # the direction the wire points at this end
    kind: str = "open"  # pin | connector | junction | branch | ground | system | page | open
    component: str | None = None  # the component, for a pin
    pin: str | None = None
    ids: tuple[str, ...] = ()  # connector ids, a ground id, the other system, or the page-join number
    other: tuple[int, int] | None = None  # for a connector: the wire end across the gap
    junction: int | None = None  # which dot, or which brace
    fuse: str | None = None  # for a fuse-box terminal: the fuse above it
    version: str | None = None  # under a brace: the marker this branch belongs to
    lane: int | None = None  # under a brace: position from the left, to pair wires across it


@dataclass
class Wire:
    points: list[tuple[float, float]]
    stroke: tuple[float, float, float]
    stripe: tuple[float, float, float] | None
    dashed: bool = False  # fitted on some versions only
    labels: list[str] = field(default_factory=list)  # colour labels printed beside it
    drawn_as: str | None = None  # the colour name its line colour stands for
    ends: list[End] = field(default_factory=list)

    @property
    def colour(self) -> str | None:
        """The printed colour, or the drawn one where nothing is printed beside the wire."""
        return Counter(self.labels).most_common(1)[0][0] if self.labels else self.drawn_as

    @property
    def colour_agrees(self) -> bool | None:
        """Does the printed colour match the drawn one? None = one of the two is missing."""
        if not self.labels or self.drawn_as is None:
            return None
        return self.colour == self.drawn_as


@dataclass
class Component:
    rect: tuple[float, float, float, float]
    name: str
    location: str | None = None
    fuses: list[tuple[float, str]] = field(default_factory=list)  # (x position, "FUSE 28 10A") inside a fuse box


@dataclass
class Page:
    number: int  # position in the PDF, from 0
    wires: list[Wire]
    components: list[Component]
    labels: list[Label]
    versions: dict[str, str] = field(default_factory=dict)  # marker -> what it means, from the legend
    problems: list[str] = field(default_factory=list)


# -- text ----------------------------------------------------------------------------
def _is_fuse_number(lb: Label, labels: list[Label]) -> bool:
    """A number printed right after or right under the word FUSE."""
    return bool(PAGE_REF.match(lb.text)) and any(
        o.text == "FUSE" and ((abs(o.y - lb.y) < 1.5 and 0 <= lb.rect[0] - o.rect[2] < 4)
                              or (0 < lb.y - o.y < 8 and abs(o.x - lb.x) < 5)) for o in labels)


def _labels(page: pymupdf.Page) -> list[Label]:
    """Words inside the drawing area. `LT`/`DK` is joined to the colour that follows it."""
    height = page.rect.height
    words = [w for w in page.get_text("words") if MARGIN_TOP < w[1] and w[3] < height - MARGIN_BOTTOM]
    out: list[Label] = []
    skip = set()
    for i, (x0, y0, x1, y1, text, block, line, _) in enumerate(words):
        if i in skip:
            continue
        if (text in ("LT", "DK") or text.endswith(("/LT", "/DK"))) and i + 1 < len(words) \
                and words[i + 1][5:7] == (block, line):
            nxt = words[i + 1]
            x0, y0, x1, y1 = min(x0, nxt[0]), min(y0, nxt[1]), max(x1, nxt[2]), max(y1, nxt[3])
            text = f"{text} {nxt[4]}"
            skip.add(i + 1)
        out.append(Label(text, (x0 + x1) / 2, (y0 + y1) / 2, (x0, y0, x1, y1)))
    return out


def _text_blocks(labels: list[Label]) -> list[tuple[tuple[float, float, float, float], str]]:
    """Descriptive text (component names, locations) regrouped by position into blocks.

    Words on one row with small gaps make a line; lines stacked closely with a shared centre
    make a block. A line that opens a bracket starts a new block, so a location note is kept
    apart from the name above it.
    """
    def is_fuse_number(lb: Label) -> bool:
        return _is_fuse_number(lb, labels)

    def names_a_junction_connector(lb: Label) -> bool:
        return any(o.text == "J/C" and abs(o.y - lb.y) < 1.5 and 0 <= lb.rect[0] - o.rect[2] < 4 for o in labels)

    words = [
        lb for lb in labels
        if not ((PIN.match(lb.text) and not is_fuse_number(lb)) or COLOUR_LABEL.match(lb.text) or GROUND_ID.match(lb.text)
                or (CONNECTOR_ID.match(lb.text) and not names_a_junction_connector(lb)))
    ]
    words.sort(key=lambda lb: (round(lb.y), lb.rect[0]))
    lines: list[list[Label]] = []
    for lb in words:
        last = lines[-1][-1] if lines else None
        if last and abs(last.y - lb.y) < 1.5 and 0 <= lb.rect[0] - last.rect[2] < 4.0:
            lines[-1].append(lb)
        else:
            lines.append([lb])
    boxed = [((min(w.rect[0] for w in ln), min(w.rect[1] for w in ln), max(w.rect[2] for w in ln), max(w.rect[3] for w in ln)),
              " ".join(w.text for w in ln)) for ln in lines]
    boxed.sort(key=lambda b: (b[0][1], b[0][0]))
    blocks: list[list] = []  # [rect, [texts]]
    for rect, text in boxed:
        cx = (rect[0] + rect[2]) / 2
        home = None
        if not text.startswith("("):
            for block in blocks:
                brect, texts = block
                closed = not texts[0].startswith("(") or texts[-1].endswith(")")
                if texts[0].startswith("(") and closed:
                    continue
                if -2.5 <= rect[1] - brect[3] < 2.5 and abs((brect[0] + brect[2]) / 2 - cx) < 6:
                    home = block
        else:
            home = None
        if home is None:
            # a bracketed note that is still open keeps collecting its lines
            for block in blocks:
                brect, texts = block
                if texts[0].startswith("(") and not texts[-1].endswith(")") and -2.5 <= rect[1] - brect[3] < 2.5 \
                        and abs((brect[0] + brect[2]) / 2 - cx) < 10:
                    home = block
        if home is None:
            blocks.append([rect, [text]])
        else:
            b = home[0]
            home[0] = (min(b[0], rect[0]), min(b[1], rect[1]), max(b[2], rect[2]), max(b[3], rect[3]))
            home[1].append(text)
    return [(rect, re.sub(r"- ", "-", " ".join(texts))) for rect, texts in blocks]


# -- drawing ---------------------------------------------------------------------------
def _rgb(value) -> tuple[float, float, float]:
    return tuple(round(v, 2) for v in value)


def _rect_distance(point, rect) -> float:
    dx = max(rect[0] - point[0], 0, point[0] - rect[2])
    dy = max(rect[1] - point[1], 0, point[1] - rect[3])
    return math.hypot(dx, dy)


def _segment_distance(point, a, b) -> float:
    (px, py), (ax, ay), (bx, by) = point, a, b
    length2 = (bx - ax) ** 2 + (by - ay) ** 2
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((px - ax) * (bx - ax) + (py - ay) * (by - ay)) / length2))
    return math.hypot(px - (ax + t * (bx - ax)), py - (ay + t * (by - ay)))


def _wire_distance(point, wire: Wire) -> float:
    return min(_segment_distance(point, a, b) for a, b in zip(wire.points, wire.points[1:]))


def _direction(tip, before) -> tuple[int, int]:
    dx, dy = tip[0] - before[0], tip[1] - before[1]
    return (int(math.copysign(1, dx)), 0) if abs(dx) >= abs(dy) else (0, int(math.copysign(1, dy)))


def _read_shapes(page: pymupdf.Page):
    plain, bases, tops, dots, boxes, rings, braces, dashed, arrows, links = [], [], [], [], [], [], [], [], [], []
    for path in page.get_drawings():
        kinds = {item[0] for item in path["items"]}
        width = round(path.get("width") or 0, 2)
        rect = path["rect"]
        if path.get("fill") and not path.get("color") and _rgb(path["fill"]) == COMPONENT_FILL:
            boxes.append((rect.x0, rect.y0, rect.x1, rect.y1))
        elif kinds == {"c"} and abs(width - JUNCTION_WIDTH) < 0.05:
            dots.append(((rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2))
        elif kinds == {"c"} and 4.5 < rect.width < 6 and 4.5 < rect.height < 6:
            rings.append((rect.x0, rect.y0, rect.x1, rect.y1))  # the ring around a version marker
        elif path["type"] == "fs" and len(path["items"]) == 2 and 3.5 < rect.width < 5.5 and 3.5 < rect.height < 5.5:
            arrows.append((rect.x0, rect.y0, rect.x1, rect.y1))
        elif path["type"] == "f" and kinds == {"l"} and path.get("fill"):
            thin, long = sorted((rect.width, rect.height))
            if thin < 0.4 and long >= 2:
                links.append((rect.x0, rect.y0, rect.x1, rect.y1))
            if DASH_THICKNESS[0] <= thin <= DASH_THICKNESS[1] and long >= 8:
                mid_x, mid_y = (rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2
                points = [(rect.x0, mid_y), (rect.x1, mid_y)] if rect.width > rect.height else [(mid_x, rect.y0), (mid_x, rect.y1)]
                dashed.append((points, _rgb(path["fill"])))
        elif kinds == {"l"} and len(path["items"]) == 6 and rect.width > 12 and rect.height < 6 and not path.get("fill") \
                and _rgb(path["color"]) == (0.0, 0.0, 0.0):
            points = [path["items"][0][1]] + [item[2] for item in path["items"]]
            peak, tip = points[3], points[0]  # the point in the middle, and one of the two outer tips
            if abs(peak.x - (rect.x0 + rect.x1) / 2) < rect.width / 4 and abs(peak.y - tip.y) > 1.5:
                braces.append((rect.x0, rect.x1, peak.x, peak.y, tip.y))  # left, right, middle, point, tips
        elif kinds == {"l"} and path.get("color") and not path.get("fill"):
            points = [path["items"][0][1]] + [item[2] for item in path["items"]]
            points = [(p.x, p.y) for p in points]
            if sum(math.dist(a, b) for a, b in zip(points, points[1:])) < MIN_WIRE_LENGTH:
                continue
            target = (plain if WIRE_WIDTH[0] <= width <= WIRE_WIDTH[1] else
                      bases if STRIPE_BASE_WIDTH[0] <= width <= STRIPE_BASE_WIDTH[1] else
                      tops if STRIPE_TOP_WIDTH[0] <= width <= STRIPE_TOP_WIDTH[1] else None)
            if target is not None and _rgb(path["color"]) != (0.0, 0.0, 0.0) or target is plain:
                target.append((points, _rgb(path["color"])))
    return plain, bases, tops, dots, boxes, rings, braces, dashed, arrows, links


def _same_path(a, b) -> bool:
    return len(a) == len(b) and all(math.dist(p, q) < 1.5 for p, q in zip(a, b))


def _merge_boxes(boxes):
    groups: list[list] = []
    for box in boxes:
        touching = [g for g in groups if any(
            box[0] <= o[2] + 0.5 and o[0] <= box[2] + 0.5 and box[1] <= o[3] + 0.5 and o[1] <= box[3] + 0.5 for o in g)]
        merged = [box] + [o for g in touching for o in g]
        groups = [g for g in groups if g not in touching] + [merged]
    return [(min(b[0] for b in g), min(b[1] for b in g), max(b[2] for b in g), max(b[3] for b in g)) for g in groups]


def read_page(page: pymupdf.Page, number: int = 0) -> Page:
    labels = _labels(page)
    plain, bases, tops, dots, boxes, rings, braces, dashed, arrows, links = _read_shapes(page)
    frame = max((p["rect"] for p in page.get_drawings() if p["rect"].width > 400 and p["rect"].height > 400),
                key=lambda r: r.width * r.height, default=page.rect)
    labels = [lb for lb in labels if frame.x0 <= lb.x <= frame.x1 and frame.y0 <= lb.y <= frame.y1]
    wires = [Wire(points, stroke, None) for points, stroke in plain]
    wires += [Wire(points, fill, None, dashed=True) for points, fill in dashed]
    for points, stroke in bases:
        top = next((t for t in tops if _same_path(points, t[0])), None)
        if top is not None:
            wires.append(Wire(points, stroke, top[1]))
    problems: list[str] = []

    # A number inside a small ring is a version marker (1 = one version of the car, 2 = another),
    # not a pin.
    markers = [lb for lb in labels if PAGE_REF.match(lb.text) and any(_rect_distance((lb.x, lb.y), r) == 0 for r in rings)]
    pin_labels = [lb for lb in labels if PIN.match(lb.text) and lb not in markers and not _is_fuse_number(lb, labels)]

    _name_colours(wires, labels)
    components = _components(boxes, labels)
    _attach_ends(wires, dots, components, pin_labels, labels, problems, braces, markers, arrows, links)
    for i, wire in enumerate(wires):
        if wire.colour_agrees is False:
            problems.append(f"wire {i}: drawn {wire.drawn_as}, labelled {wire.colour}")
    # The legend: a marker with its meaning written to its right.
    versions = {}
    for marker in markers:
        note = [lb for lb in labels if abs(lb.y - marker.y) < 2 and 3 < lb.rect[0] - marker.x < 12
                and not PIN.match(lb.text) and not COLOUR_LABEL.match(lb.text)]
        if note:
            row = sorted((lb for lb in labels if abs(lb.y - marker.y) < 2 and lb.rect[0] > marker.x + 3), key=lambda lb: lb.x)
            words = [row[0]]
            for lb in row[1:]:
                if lb.rect[0] - words[-1].rect[2] > 4:
                    break
                words.append(lb)
            versions[marker.text] = " ".join(lb.text for lb in words)
    return Page(number, wires, components, labels, versions, problems)


def _name_colours(wires: list[Wire], labels: list[Label]) -> None:
    """Give each colour label to the nearest wire, then learn which line colour means which name."""
    for lb in labels:
        if not COLOUR_LABEL.match(lb.text):
            continue
        # The label is printed about 4 pt above a horizontal run, or left of a vertical one.
        best, best_cost = None, 99.0
        for wire in wires:
            for (ax, ay), (bx, by) in zip(wire.points, wire.points[1:]):
                if abs(ay - by) < 0.5 and min(ax, bx) - 2 <= lb.x <= max(ax, bx) + 2:
                    side = ay - lb.y
                elif abs(ax - bx) < 0.5 and min(ay, by) - 2 <= lb.y <= max(ay, by) + 2:
                    side = ax - lb.x
                else:
                    continue
                if 1.5 <= side <= 6.5 and abs(side - 4) < best_cost:
                    best, best_cost = wire, abs(side - 4)
        if best is not None:
            best.labels.append(lb.text)
    votes: dict[tuple, Counter] = defaultdict(Counter)
    for wire in wires:
        for text in wire.labels:
            base, _, stripe = text.partition("/")
            votes[wire.stroke][base] += 1
            if wire.stripe and stripe:
                votes[wire.stripe][stripe] += 1
    names = {stroke: counter.most_common(1)[0][0] for stroke, counter in votes.items()}
    for wire in wires:
        base = names.get(wire.stroke)
        if base and wire.stripe:
            stripe = names.get(wire.stripe)
            wire.drawn_as = f"{base}/{stripe}" if stripe else None
        else:
            wire.drawn_as = base


def _components(boxes, labels: list[Label]) -> list[Component]:
    blocks = _text_blocks(labels)
    not_a_name = re.compile(r"^(HOT |\d+A$|FUSE \d|RELAY ENERGIZED|ENERGIZED|ALL TIMES|M$|EXCEPT |\d)")
    names = [(rect, text) for rect, text in blocks if not text.startswith("(") and not not_a_name.match(text)]
    notes = [(rect, text) for rect, text in blocks if text.startswith("(")]
    out = []
    for box in _merge_boxes(boxes):
        inside = [text for rect, text in names if _rect_distance(((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2), box) == 0
                  and re.search(r"BOX|UNIT|MODULE", text)]
        if not inside:  # a fuse box prints its name across the top edge of the box
            inside = [text for rect, text in names if re.search(r"BOX", text) and _rect_distance(
                ((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2), box) < 6 and box[0] <= (rect[0] + rect[2]) / 2 <= box[2]]
        outside = sorted(
            (( _rect_distance(((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2), box), text, rect) for rect, text in names
             if _rect_distance(((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2), box) > 0), key=lambda t: t[0])
        if inside:
            name, anchor = inside[0], box
        elif outside and outside[0][0] < 24:
            name, anchor = outside[0][1], outside[0][2]
        else:
            name, anchor = "?", box
        centre = ((anchor[0] + anchor[2]) / 2, (anchor[1] + anchor[3]) / 2)
        note = min(notes, key=lambda n: _rect_distance(centre, n[0]), default=None)
        location = note[1].strip("()") if note and _rect_distance(centre, note[0]) < 14 else None
        fuses = [((rect[0] + rect[2]) / 2, text) for rect, text in blocks
                 if text.startswith("FUSE ") and _rect_distance(((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2), box) == 0]
        out.append(Component(box, name, location, fuses))
    return out


def _pin_beside(end: End, pin_labels: list[Label]) -> str | None:
    """The pin number printed beside a wire end: above a horizontal wire, left of a vertical one,
    just inside the end."""
    best, best_cost = None, 99.0
    for lb in pin_labels:
        dx, dy = lb.x - end.xy[0], lb.y - end.xy[1]
        along = -(dx * end.out[0] + dy * end.out[1])  # positive = back along the wire
        side = -dy if end.out[0] else -dx  # positive = above, or left
        if -6.5 <= along <= 8 and 1.5 <= side <= 6.5:
            cost = abs(along - 2.5) + abs(side - 4)
            if cost < best_cost:
                best, best_cost = lb.text, cost
    return best


def _attach_ends(wires, dots, components, pin_labels, labels, problems, braces=(), markers=(), arrows=(), links=()) -> None:
    for wire in wires:
        first, last = wire.points[0], wire.points[-1]
        wire.ends = [End(first, _direction(first, wire.points[1])), End(last, _direction(last, wire.points[-2]))]
    all_ends = [(wi, ei, end) for wi, wire in enumerate(wires) for ei, end in enumerate(wire.ends)]
    xs = [x for wire in wires for x, _ in wire.points]
    left, right = (min(xs), max(xs)) if xs else (0, 0)
    connector_ids = [lb for lb in labels if CONNECTOR_ID.match(lb.text)]
    ground_ids = [lb for lb in labels if GROUND_ID.match(lb.text)]

    for wi, ei, end in all_ends:
        # a junction dot
        for di, dot in enumerate(dots):
            if math.dist(end.xy, dot) < JUNCTION_REACH:
                end.kind, end.junction = "junction", di
        if end.kind != "open":
            continue
        # the wire end facing this one across a small gap: a connector pin
        best = None
        for wj, ej, other in all_ends:
            if wj == wi or other.out != (-end.out[0], -end.out[1]):
                continue
            dx, dy = other.xy[0] - end.xy[0], other.xy[1] - end.xy[1]
            ahead = dx * end.out[0] + dy * end.out[1]
            aside = abs(dx * end.out[1] - dy * end.out[0])
            if 0.3 < ahead <= MAX_GAP and aside < 0.7 and (best is None or ahead < best[0]):
                best = (ahead, wj, ej)
        if best:
            end.kind, end.other = "connector", (best[1], best[2])
            continue
        # the edge of the page: the wire continues on another page
        if end.out[0] and (end.xy[0] > right - 0.7 or end.xy[0] < left + 0.7):
            ref = [lb for lb in labels if PAGE_REF.match(lb.text) and abs(lb.y - end.xy[1]) < 3.5
                   and 2 <= (lb.x - end.xy[0]) * end.out[0] <= 10]
            near_box = any(_rect_distance(end.xy, c.rect) < 8 for c in components)
            if ref and not near_box:
                end.kind, end.ids = "page", (ref[0].text,)
                continue
        # an arrowhead: the wire goes to another system, named beside the arrow
        for arrow in arrows:
            if _rect_distance(end.xy, arrow) < 1.0:
                tip = (arrow[2] if end.out[0] > 0 else arrow[0] if end.out[0] < 0 else end.xy[0],
                       arrow[3] if end.out[1] > 0 else arrow[1] if end.out[1] < 0 else end.xy[1])
                named = min(_text_blocks(labels), key=lambda b: _rect_distance(tip, b[0]), default=None)
                end.kind = "system"
                end.ids = (named[1],) if named and _rect_distance(tip, named[0]) < 12 else ("?",)
        if end.kind != "open":
            continue
        # a version brace: wires above it continue as the wires below it
        for bi, (x0, x1, middle, point, tips) in enumerate(braces):
            if end.out[0] or not (x0 - 1 <= end.xy[0] <= x1 + 1):
                continue
            up = 1 if point < tips else -1  # +1: the point is above, so the shared wires are above
            if end.out == (0, up) and 0 < (point - end.xy[1]) * up < 7:
                end.kind, end.junction = "branch", bi  # a shared wire, at the point of the brace
            elif end.out == (0, -up) and 0 < (end.xy[1] - tips) * up < 7:
                half = (x0, middle) if end.xy[0] < middle else (middle, x1)
                name = [m.text for m in markers if half[0] <= m.x <= half[1] and 0 < (tips - m.y) * up < 16]
                if not name:  # no ringed number: a short text names the half
                    name = [text for rect, text in _text_blocks(labels)
                            if half[0] <= (rect[0] + rect[2]) / 2 <= half[1] and -2 < (tips - (rect[1] + rect[3]) / 2) * up < 18]
                end.kind, end.junction, end.version = "branch", bi, (name[0] if name else "?")
        if end.kind != "open":
            continue
        # a component pin
        owner = min(components, key=lambda c: _rect_distance(end.xy, c.rect), default=None)
        if owner is not None and _rect_distance(end.xy, owner.rect) < 8:
            end.kind, end.component, end.pin = "pin", owner.name, _pin_beside(end, pin_labels)
            above = min(owner.fuses, key=lambda f: abs(f[0] - end.xy[0]), default=None)
            if above is not None and abs(above[0] - end.xy[0]) < 10:
                end.fuse = above[1]
            continue
        ground = min(ground_ids, key=lambda lb: math.dist(end.xy, (lb.x, lb.y)), default=None)
        if ground is not None and math.dist(end.xy, (ground.x, ground.y)) < 20:
            end.kind, end.ids = "ground", (ground.text,)

    # a dot with a ground id beside it is a ground point
    for di, dot in enumerate(dots):
        ground = min(ground_ids, key=lambda lb: math.dist(dot, (lb.x, lb.y)), default=None)
        if ground is not None and math.dist(dot, (ground.x, ground.y)) < 22:
            for _, _, end in all_ends:
                if end.junction == di and end.kind == "junction":
                    end.ids = (ground.text,)

    # wires that pass through a junction dot without ending there
    for wi, wire in enumerate(wires):
        for di, dot in enumerate(dots):
            if _wire_distance(dot, wire) < 0.8 and all(e.junction != di for e in wire.ends):
                wire.ends.append(End(dot, (0, 0), kind="junction", junction=di))

    # lanes under a brace: wires are paired across it by their order from the left
    for bi in range(len(braces)):
        groups: dict[str | None, list[End]] = defaultdict(list)
        for _, _, end in all_ends:
            if end.kind == "branch" and end.junction == bi:
                groups[end.version].append(end)
        for version, members in groups.items():
            for lane, end in enumerate(sorted(members, key=lambda e: e.xy[0])):
                end.lane = lane
        sizes = {version: len(members) for version, members in groups.items()}
        if sizes and (len(set(sizes.values())) > 1 or None not in sizes or "?" in sizes):
            problems.append(f"brace {bi}: wires above and below do not pair up {sizes}")

    # connector pins: the number beside the gap, and the connector id shared by a column of gaps
    gaps = []
    for wi, ei, end in all_ends:
        if end.kind == "connector" and (wi, ei) < end.other:
            other = wires[end.other[0]].ends[end.other[1]]
            end.pin = other.pin = _pin_beside(end, pin_labels) or _pin_beside(other, pin_labels)
            centre = ((end.xy[0] + other.xy[0]) / 2, (end.xy[1] + other.xy[1]) / 2)
            gaps.append((centre, end, other))
    for centre, end, other in gaps:
        horizontal = bool(end.out[0])
        column = [g for g in gaps if bool(g[1].out[0]) == horizontal
                  and abs((g[0][0] - centre[0]) if horizontal else (g[0][1] - centre[1])) < 1.5]
        column.sort(key=lambda g: g[0][1] if horizontal else g[0][0])
        # keep the unbroken run of gaps around this one
        position = lambda g: g[0][1] if horizontal else g[0][0]  # noqa: E731
        here = position((centre, end, other))
        run = [g for g in column if abs(position(g) - here) < 0.01]
        for step in (1, -1):
            ordered = [g for g in column if (position(g) - here) * step > 0]
            ordered.sort(key=lambda g: abs(position(g) - here))
            edge = here
            for g in ordered:
                if abs(position(g) - edge) > 15:  # the pins of one connector sit on neighbouring rows
                    break
                run.append(g)
                edge = position(g)
        # pins joined by the connector's dashed line, followed from gap to gap
        linked, grew = [(centre, end, other)], True
        while grew:
            grew = False
            for lk in links:
                if any(_rect_distance(g[0], lk) < 3.5 for g in linked):
                    for g in gaps:
                        if g not in linked and _rect_distance(g[0], lk) < 3.5:
                            linked.append(g)
                            grew = True
        if len(linked) > 1:
            run = linked
        found = []
        for g in run:
            for lb in connector_ids:
                if math.dist(g[0], (lb.x, lb.y)) < 16:
                    mark = min(markers, key=lambda m: _rect_distance((m.x, m.y), lb.rect), default=None)
                    text = f"{lb.text} [{mark.text}]" if mark and _rect_distance((mark.x, mark.y), lb.rect) < 9 else lb.text
                    if text not in found:
                        found.append(text)
        end.ids = other.ids = tuple(sorted(found))
        if not found:
            problems.append(f"connector gap at ({centre[0]:.0f},{centre[1]:.0f}) has no connector id")
    for wi, ei, end in all_ends:
        if end.kind == "open":
            problems.append(f"wire {wi} end at ({end.xy[0]:.0f},{end.xy[1]:.0f}) is attached to nothing")
        elif end.kind == "connector" and end.pin is None:
            problems.append(f"wire {wi} connector end at ({end.xy[0]:.0f},{end.xy[1]:.0f}) has no pin number")


# -- a whole diagram, and following a wire through it -------------------------------------
HEADER_PAGE = re.compile(r"(?P<title>.+?) - Page (?P<n>\d+) of (?P<m>\d+)$")


@dataclass
class Diagram:
    vehicle_line: str  # "2025 Acura Integra L4-1.5L Turbo (L15CA)"
    system_path: list[str]
    title: str
    pages: list[Page]
    page_labels: list[str]  # "1 of 3", one per page
    diagram_ids: list[str]

    @property
    def problems(self) -> list[str]:
        return [f"page {label}: {problem}" for page, label in zip(self.pages, self.page_labels) for problem in page.problems]


def read_pdf(source: str | bytes) -> Diagram:
    """Read every diagram page of a PDF. Pages that carry only a header are skipped."""
    doc = pymupdf.open(stream=source, filetype="pdf") if isinstance(source, bytes) else pymupdf.open(source)
    vehicle_line, system_path, title = "", [], ""
    pages, page_labels, diagram_ids = [], [], []
    for number, raw in enumerate(doc):
        header = [line.strip() for line in raw.get_text("text", clip=pymupdf.Rect(0, MARGIN_TOP, raw.rect.width, 101)).splitlines()
                  if line.strip()]
        label = next((m for m in (HEADER_PAGE.match(line) for line in header) if m), None)
        if label is None:
            continue
        vehicle_line = vehicle_line or next((line for line in header if re.match(r"^(19|20)\d{2} ", line)), "")
        path_line = next((line for line in header if " > " in line), "")
        system_path = system_path or [part.strip() for part in path_line.split(">") if part.strip() and part.strip() != "Vehicle"]
        title = title or label["title"]
        page = read_page(raw, number)
        if not page.wires:  # a page that carries only the header
            continue
        pages.append(page)
        page_labels.append(f"{label['n']} of {label['m']}")
        diagram_ids += [lb.text for lb in page.labels if re.fullmatch(r"\d{6}", lb.text)][:1] or ["?"]
    return Diagram(vehicle_line, system_path, title, pages, page_labels, diagram_ids)


@dataclass(frozen=True)
class Quality:
    """How far a diagram can be trusted, decided from checks that need no human and no answer key.

      traced     every wire end is attached and every printed colour agrees with the drawn one:
                 pins and routes may be used in answers
      partial    most of the drawing was read; routes that pass a problem are marked incomplete
      show_only  the file is not drawn the way this reader understands (a scanned image, another
                 drawing style): it is stored and can be shown, and no pin or route is taken from it
    """

    verdict: str
    wires: int
    colour_agree: int
    colour_disagree: int
    no_printed_colour: int
    unattached_ends: int
    reasons: tuple[str, ...]


# Starting limits, to be tuned as more diagram families are scored.
MAX_UNATTACHED_SHARE = 0.10
MAX_DISAGREE_SHARE = 0.02


def assess(diagram: Diagram) -> Quality:
    wires = [wire for page in diagram.pages for wire in page.wires]
    agree = sum(wire.colour_agrees is True for wire in wires)
    disagree = sum(wire.colour_agrees is False for wire in wires)
    unattached = sum(end.kind == "open" for wire in wires for end in wire.ends)
    ends = sum(len(wire.ends) for wire in wires)
    reasons = []
    if not wires:
        reasons.append("no wires were found: the page is an image, or is drawn in a style this reader does not know")
    if wires and agree == 0:
        reasons.append("no wire has a printed colour that could be checked against its drawn colour")
    if disagree:
        reasons.append(f"{disagree} wire(s) are drawn in one colour and labelled another")
    if unattached:
        reasons.append(f"{unattached} wire end(s) are attached to nothing")
    other = len(diagram.problems) - disagree - unattached
    if other > 0:
        reasons.append(f"{other} other problem(s), such as a connector with no id")
    if not wires or agree == 0 or unattached > MAX_UNATTACHED_SHARE * ends or disagree > MAX_DISAGREE_SHARE * len(wires):
        verdict = "show_only"
    elif reasons:
        verdict = "partial"
    else:
        verdict = "traced"
    return Quality(verdict, len(wires), agree, disagree, len(wires) - agree - disagree, unattached, tuple(reasons))


def is_complete(route: list[Hop]) -> bool:
    """False when the route stops at an end the reader could not attach: it must not be
    presented as the whole path."""
    return route[-1].kind != "open"


@dataclass(frozen=True)
class Hop:
    kind: str  # pin | wire | connector | junction_connector | version | ground | system | page | open
    name: str | None = None  # component, connector ids, ground id, other system, version, colour
    pin: str | None = None
    detail: str | None = None  # the fuse at a fuse-box terminal; "dashed" for a wire fitted on some versions

    def __str__(self) -> str:
        match self.kind:
            case "wire":
                return f"-{self.name or 'colour not given'}{' (some versions)' if self.detail else ''}->"
            case "pin":
                return f"{self.name} pin {self.pin or '?'}" + (f" ({self.detail})" if self.detail else "")
            case "connector" | "junction_connector":
                return f"{self.name} pin {self.pin or '?'}"
            case "version":
                return f"[{self.name}]"
            case "page":
                return f"(page {self.name})"
        return f"{self.kind} {self.name or ''}".strip()


def _same(a: str | None, b: str) -> bool:
    squeeze = lambda t: re.sub(r"[^A-Z0-9]", "", (t or "").upper())  # noqa: E731
    return squeeze(b) in squeeze(a)


def pins_of(diagram: Diagram, component: str) -> list[tuple[str | None, str | None, str]]:
    """Every pin of a component that a wire is attached to: (pin, wire colour, page label)."""
    found = {(end.pin, wire.colour, label) for page, label in zip(diagram.pages, diagram.page_labels)
             for wire in page.wires for end in wire.ends if end.kind == "pin" and _same(end.component, component)}
    return sorted(found, key=lambda t: (len(t[0] or ""), t[0] or ""))


def trace(diagram: Diagram, component: str, pin: str) -> list[list[Hop]]:
    """Follow the wire from a component pin to everything it reaches.

    Returns one route per destination. A route passes through connectors, junctions, junction
    connectors, version braces and page joins, and ends at a component pin, a ground, another
    system, or (reported, never hidden) an end that is attached to nothing.
    """
    routes: list[list[Hop]] = []
    for pi, page in enumerate(diagram.pages):
        for wi, wire in enumerate(page.wires):
            for ei, end in enumerate(wire.ends):
                if end.kind == "pin" and end.pin == pin and _same(end.component, component):
                    start = Hop("pin", end.component, end.pin, end.fuse)
                    _walk(diagram, pi, wi, ei, [start], {(pi, wi)}, routes)
    return routes


def _walk(diagram: Diagram, pi: int, wi: int, came_in: int, route: list[Hop], seen: set, routes: list) -> None:
    page = diagram.pages[pi]
    wire = page.wires[wi]
    route = route + [Hop("wire", wire.colour, detail="dashed" if wire.dashed else None)]
    exits = [(ei, end) for ei, end in enumerate(wire.ends) if ei != came_in]
    for _, end in exits:
        nexts: list[tuple[int, int, int, list[Hop]]] = []  # page, wire, end, hops passed on the way
        match end.kind:
            case "connector":
                nexts.append((pi, end.other[0], end.other[1], [Hop("connector", "/".join(end.ids) or "connector ?", end.pin)]))
            case "junction" if end.ids:  # a ground point: the route ends here
                routes.append(route + [Hop("ground", ", ".join(end.ids))])
                continue
            case "junction":
                nexts += [(pi, wj, ej, []) for wj, other in enumerate(page.wires) for ej, e in enumerate(other.ends)
                          if wj != wi and e.kind == "junction" and e.junction == end.junction]
            case "branch":
                for wj, other in enumerate(page.wires):
                    for ej, e in enumerate(other.ends):
                        if e.kind == "branch" and e.junction == end.junction and e.lane == end.lane \
                                and (e.version is None) != (end.version is None):
                            nexts.append((pi, wj, ej, [Hop("version", e.version or end.version)]))
            case "page":
                for pj in (pi - 1, pi + 1):
                    if 0 <= pj < len(diagram.pages):
                        nexts += [(pj, wj, ej, [Hop("page", diagram.page_labels[pj])])
                                  for wj, other in enumerate(diagram.pages[pj].wires) for ej, e in enumerate(other.ends)
                                  if e.kind == "page" and e.ids == end.ids and e.out == (-end.out[0], 0)
                                  and (pj - pi) == end.out[0]]
            case "pin" if (end.component or "").startswith("J/C"):
                across = [(pi, wj, ej, [Hop("junction_connector", end.component, end.pin), Hop("junction_connector", e.component, e.pin)])
                          for wj, other in enumerate(page.wires) for ej, e in enumerate(other.ends)
                          if wj != wi and e.kind == "pin" and e.component == end.component and e.out == (-end.out[0], -end.out[1])
                          and abs((e.xy[0] - end.xy[0]) * end.out[1] - (e.xy[1] - end.xy[1]) * end.out[0]) < 1.0]
                nexts += across
                if not across:
                    routes.append(route + [Hop("pin", end.component, end.pin)])
            case "pin":
                routes.append(route + [Hop("pin", end.component, end.pin, end.fuse)])
            case "ground" | "system":
                routes.append(route + [Hop(end.kind, ", ".join(end.ids))])
            case _:
                routes.append(route + [Hop("open", f"at ({end.xy[0]:.0f},{end.xy[1]:.0f})")])
        if end.kind in ("connector", "junction", "branch", "page") and not nexts:
            routes.append(route + [Hop("open", f"{end.kind} with nothing on the other side")])
        for pj, wj, ej, hops in nexts:
            if (pj, wj) not in seen:
                _walk(diagram, pj, wj, ej, route + hops, seen | {(pj, wj)}, routes)

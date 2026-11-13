"""What gets copied out of a record into the lookup tables.

Pure functions: a record in, rows out. The record stays the single source; these rows can be
rebuilt from it at any time.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from itertools import product

from app.kb.keys import code_key, code_stem, norm_key
from app.kb.schemas import Case, ExpertNote, Procedure, WiringDiagram

KbRecord = Procedure | WiringDiagram | Case | ExpertNote

RECORD_TYPES: dict[str, type] = {
    "procedure": Procedure,
    "diagram": WiringDiagram,
    "case": Case,
    "expert_note": ExpertNote,
}


@dataclass(frozen=True)
class ScopeRow:
    make_key: str | None
    model_key: str | None
    engine_key: str | None
    year_from: int | None
    year_to: int | None
    powertrain: str | None


@dataclass(frozen=True)
class CodeRow:
    code_raw: str
    code_key: str
    stem_key: str
    module: str | None


@dataclass(frozen=True)
class TermRow:
    kind: str  # system | module | part
    term_key: str
    term_text: str


@dataclass(frozen=True)
class UnitRow:
    kind: str
    ordinal: int
    text: str
    origin: str | None = None


def record_type(record: KbRecord) -> str:
    for name, cls in RECORD_TYPES.items():
        if isinstance(record, cls):
            return name
    raise TypeError(f"not a knowledge-base record: {type(record).__name__}")


def record_key(record: KbRecord) -> str:
    match record:
        case Procedure():
            return record.procedure_id
        case WiringDiagram():
            return record.diagram_key
        case Case():
            return record.case_id
        case ExpertNote():
            return record.note_id
    raise TypeError(type(record).__name__)


def body_of(record: KbRecord) -> dict:
    """The record as stored: everything except its review, which lives in its own columns."""
    return record.model_dump(mode="json", by_alias=True, exclude={"review"})


def body_hash(body: dict) -> str:
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _pt(value) -> str | None:
    return None if value is None else str(value.value if hasattr(value, "value") else value)


def scope_rows(record: KbRecord) -> list[ScopeRow]:
    """Always at least one row. A row with no make means the record is not tied to a make."""
    match record:
        case Procedure() | WiringDiagram():
            v = record.vehicle
            return [ScopeRow(norm_key(v.make), norm_key(v.model), norm_key(v.engine_code), v.year, v.year, _pt(v.powertrain))]
        case Case() if record.repaired_cars:
            return list(dict.fromkeys(
                ScopeRow(norm_key(record.make), norm_key(car.model), None, car.year, car.year, _pt(record.powertrain))
                for car in record.repaired_cars))
        case Case():
            models = [norm_key(m) for m in record.models] or [None]
            return [
                ScopeRow(norm_key(record.make), m, None, record.year_from, record.year_to, _pt(record.powertrain))
                for m in dict.fromkeys(models)
            ]
        case ExpertNote():
            a = record.applies_to
            makes = [norm_key(m) for m in a.makes] or [None]
            # A model belongs to a make. With several makes we cannot tell which make a model
            # belongs to, so the scope stays at the make level and the models stay in the body.
            models = [norm_key(m) for m in a.models] if len(a.makes) == 1 else []
            powertrains = [_pt(p) for p in a.powertrains] or [None]
            return [
                ScopeRow(make, model, None, a.year_from, a.year_to, pt)
                for make, model, pt in product(dict.fromkeys(makes), dict.fromkeys(models or [None]), powertrains)
            ]
    raise TypeError(type(record).__name__)


def code_rows(record: KbRecord) -> list[CodeRow]:
    match record:
        case Procedure():
            found = [(record.code, None)]
        case Case():
            found = [(fc.code, fc.module) for fc in record.fault_codes if fc.code]
        case ExpertNote():
            found = [(c, None) for c in record.fault_codes]
        case _:
            found = []
    rows: dict[str, CodeRow] = {}
    for raw, module in found:
        key = code_key(raw)
        rows.setdefault(key, CodeRow(raw.strip(), key, code_stem(raw), module))
    return list(rows.values())


def term_rows(record: KbRecord) -> list[TermRow]:
    found: list[tuple[str, str]] = []
    match record:
        case Case():
            found += [("system", record.system)] if record.system else []
            found += [("module", m) for m in record.modules]
            found += [("module", fc.module) for fc in record.fault_codes if fc.module]
            found += [("part", p.part) for p in record.parts_replaced]
        case ExpertNote():
            found += [("system", s) for s in record.systems]
        case WiringDiagram():
            found += [("system", s) for s in record.system_path]
    rows: dict[tuple[str, str], TermRow] = {}
    for kind, text in found:
        key = norm_key(text)
        if key:
            rows.setdefault((kind, key), TermRow(kind, key, text.strip()))
    return list(rows.values())


def unit_rows(record: KbRecord) -> list[UnitRow]:
    """The small pieces search runs on. Each is one idea, so a match means something specific."""
    found: list[tuple[str, str | None, str | None]] = []  # kind, text, origin
    match record:
        case Procedure():
            found += [("meaning", record.meaning, None), ("overview", record.overview, None)]
        case WiringDiagram():
            found += [("title", record.title, None)]
            found += [("component", name, None) for name in dict.fromkeys(c.name for c in record.components)]
        case Case():
            found += [("symptom", s, None) for s in record.symptoms]
            found += [("fault_text", fc.text_on_tool, None) for fc in record.fault_codes]
            found += [("finding", f, None) for f in record.findings]
            found += [("diagnosis", record.diagnosis, None), ("root_cause", record.root_cause, None)]
            found += [("repair", r, None) for r in record.repair_steps]
        case ExpertNote():
            found += [("title", record.title, None)]
            found += [("symptom", s, None) for s in record.symptoms]
            found += [("fault_text", t, None) for t in record.fault_texts]
            found += [("claim", c.text, c.origin.value) for c in record.claims]
            found += [("threshold", f"{t.quantity}: {t.meaning}", t.origin.value) for t in record.thresholds]
            found += [("advice", c.text, c.origin.value) for c in record.customer_advice]
    rows: list[UnitRow] = []
    counters: dict[str, int] = {}
    for kind, text, origin in found:
        if text and text.strip():
            counters[kind] = counters.get(kind, 0) + 1
            rows.append(UnitRow(kind, counters[kind], text.strip(), origin))
    return rows


@dataclass(frozen=True)
class PinRow:
    component_key: str
    component: str
    pin_key: str
    pin: str
    wire_colour: str | None
    page: str
    fuse: str | None
    routes: list[dict]


def pin_rows(record: KbRecord) -> list[PinRow]:
    """The pins of a wiring diagram, each with its routes, for exact lookup by component and pin."""
    if not isinstance(record, WiringDiagram):
        return []
    return [
        PinRow(norm_key(p.component) or "?", p.component, norm_key(p.pin) or "?", p.pin, p.wire_colour, p.page, p.fuse,
               [r.model_dump(mode="json") for r in p.routes])
        for p in record.pins
    ]

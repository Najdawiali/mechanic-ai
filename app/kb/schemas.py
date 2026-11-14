"""Knowledge-base record types (handoff 7.4), as Pydantic models.

Four kinds of knowledge, each with a different level of authority:

  Procedure    manufacturer troubleshooting steps (licensed source, e.g. ALLDATA)
  WiringDiagram  a wiring diagram read into pins and routes (licensed source)
  Case         one real repair done in the client's workshop
  ExpertNote   a rule of thumb from the client's expert; applies across vehicles

Every record carries `source` (where it came from) and `review` (who approved it).
Nothing reaches the mechanic unless review.status == "approved".

`extra="forbid"` everywhere: a misspelled field is an error, not silently dropped.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# -- shared ------------------------------------------------------------------
class Powertrain(StrEnum):
    ICE = "ICE"
    HYBRID = "Hybrid"
    PHEV = "PHEV"
    EV = "EV"


class Vehicle(Record):
    make: str
    model: str
    year: int | None = None  # None = unknown; never guess a year
    engine_code: str | None = None
    powertrain: Powertrain | None = None


class Source(Record):
    kind: Literal["licensed_procedure", "licensed_diagram", "client_case_file", "expert_transcript", "scan_report"]
    file: str  # path under data/, or the original file name
    captured_via: str | None = None  # e.g. "voice conversation with ChatGPT"
    licensed: bool = False  # True = from a licensed manual; the source file itself never goes into git


class Review(Record):
    status: Literal["draft", "approved", "rejected"] = "draft"
    reviewer: str | None = None
    reviewed_on: dt.date | None = None
    open_questions: list[str] = Field(default_factory=list)  # to ask the expert before approval

    @model_validator(mode="after")
    def _approved_needs_reviewer(self) -> Review:
        if self.status == "approved" and not self.reviewer:
            raise ValueError("an approved record must name its reviewer")
        return self


class NormalRange(Record):
    """A numeric condition. Readings are compared to it IN CODE, never by the LLM."""

    op: Literal["between", "lt", "le", "gt", "ge"]
    min: float | None = None
    max: float | None = None
    unit: str  # ohm, kohm, Mohm, V, mV, A (see app/engine/readings.py)
    approximate: bool = False  # the manual says "about"
    condition: str | None = None  # e.g. "at room temperature"
    as_printed: str | None = None  # the values and unit with the manual's own digits ("7.14 – 7.90 Ω"); shown, never compared

    @model_validator(mode="after")
    def _bounds_match_op(self) -> NormalRange:
        need_min = self.op in ("between", "gt", "ge")
        need_max = self.op in ("between", "lt", "le")
        if need_min != (self.min is not None) or need_max != (self.max is not None):
            raise ValueError(f"op={self.op!r} needs min={'yes' if need_min else 'no'}, max={'yes' if need_max else 'no'}")
        if self.op == "between" and self.min > self.max:  # type: ignore[operator]
            raise ValueError("min is greater than max")
        return self

    def contains(self, value: float) -> bool:
        """`value` must already be in `self.unit` (unit conversion happens before this)."""
        return {
            "between": lambda: self.min <= value <= self.max,
            "lt": lambda: value < self.max,
            "le": lambda: value <= self.max,
            "gt": lambda: value > self.min,
            "ge": lambda: value >= self.min,
        }[self.op]()


# -- Procedure ---------------------------------------------------------------
class TestPoint(Record):
    label: str  # "valve connector pin 1", "body ground"
    connector: str | None = None
    pin: str | None = None  # a string: pins can be "64", "A3", "B+"
    ground: bool = False
    terminal_side: Literal["male", "female"] | None = None  # which face of the connector is probed

    @model_validator(mode="after")
    def _pin_or_ground(self) -> TestPoint:
        if self.ground and (self.pin or self.connector):
            raise ValueError("a ground test point has no connector or pin")
        if not self.ground and not self.pin:
            raise ValueError("a test point is either ground or a pin")
        return self


class Measure(Record):
    from_point: TestPoint = Field(alias="from")
    to_point: TestPoint = Field(alias="to")


class Step(Record):
    id: str
    section: str | None = None  # the inspection this step belongs to, as titled in the manual
    check: str
    prepare: list[str] = Field(default_factory=list)  # ignition state, connectors to disconnect
    notices: list[str] = Field(default_factory=list)  # NOTICE boxes that apply to this step
    measure: Measure | None = None
    normal: NormalRange | None = None
    if_abnormal: str  # the conclusion when the reading is outside `normal`
    if_normal: str | None = None  # next step id; None = last step
    requires_tool: str | None = None  # factory tool, e.g. "HDS"


class ToolFunction(Record):
    tool: str  # e.g. "HDS" (Honda's factory tool)
    path: str  # the menu path exactly as printed


class Procedure(Record):
    procedure_id: str
    vehicle: Vehicle
    code: str  # exactly as written; matched with exact SQL, never by similarity
    meaning: str
    overview: str | None = None  # when the control unit sets this code
    warnings: list[str] = Field(default_factory=list)
    related_docs: list[str] = Field(default_factory=list)
    confirmation: list[str] = Field(default_factory=list)  # how to confirm the fault / the repair
    tool_functions: list[ToolFunction] = Field(default_factory=list)
    steps: list[Step]
    elimination_conclusion: str | None = None  # the verdict when every step reads normal
    source: Source
    review: Review = Field(default_factory=Review)

    @model_validator(mode="after")
    def _steps_link_up(self) -> Procedure:
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate step ids")
        for s in self.steps:
            if s.if_normal is not None and s.if_normal not in ids:
                raise ValueError(f"step {s.id}: if_normal points to unknown step {s.if_normal!r}")
        return self


# -- WiringDiagram ------------------------------------------------------------
class RouteHop(Record):
    kind: Literal["pin", "wire", "connector", "junction_connector", "version", "ground", "system", "page", "open"]
    name: str | None = None  # component, connector ids, ground id, other system, version, or wire colour
    pin: str | None = None
    detail: str | None = None  # the fuse at a fuse-box terminal; "dashed" for a wire fitted on some versions


class Route(Record):
    """Where a wire goes from one pin, worked out once when the diagram is read."""

    hops: list[RouteHop]
    text: str  # the same route in one line, for showing and for citing
    complete: bool  # False = it stops at an end the reader could not attach: never present it as the whole path


class DiagramPin(Record):
    component: str
    pin: str  # a string: pins can be "31", "A11"
    wire_colour: str | None = None  # None = the diagram prints no colour for this wire
    page: str  # "1 of 3"
    fuse: str | None = None
    # When the page draws this component more than once, the version of the car this drawing is for
    # ("1.5L TURBO", "2 = 1.5L TURBO CVT INTEGRA"). None = drawn once, or the drawing does not say.
    version: str | None = None
    routes: list[Route] = Field(default_factory=list)


class DiagramComponent(Record):
    name: str
    location: str | None = None
    fitted: str | None = None  # which cars have it, when the diagram says so: "M/T", "IF EQUIPPED"
    page: str


class DiagramSheet(Record):
    page: str  # "1 of 3"
    diagram_id: str  # the provider's id printed on the page
    image_key: str | None = None  # where the page image is in storage, to send when the mechanic asks
    versions: dict[str, str] = Field(default_factory=dict)  # this page's legend: marker -> meaning


class WiringDiagram(Record):
    """One wiring diagram (all its pages), read into pins and routes."""

    diagram_key: str
    vehicle: Vehicle
    system_path: list[str]  # e.g. ["Brakes and Traction Control", "Antilock Brakes / Traction Control Systems"]
    title: str
    sheets: list[DiagramSheet]
    components: list[DiagramComponent] = Field(default_factory=list)
    # marker -> meaning, only for markers that mean the same on every page; each sheet has its own legend,
    # and every route spells out the meaning of the markers it passes
    versions: dict[str, str] = Field(default_factory=dict)
    pins: list[DiagramPin] = Field(default_factory=list)
    # How far the reading can be trusted (see app/ingest/diagrams.py):
    # traced = pins and routes usable; partial = incomplete routes are marked; show_only = no pins taken.
    verdict: Literal["traced", "partial", "show_only"]
    verdict_reasons: list[str] = Field(default_factory=list)
    source: Source
    review: Review = Field(default_factory=Review)

    @model_validator(mode="after")
    def _show_only_has_no_pins(self) -> WiringDiagram:
        if self.verdict == "show_only" and self.pins:
            raise ValueError("a show_only diagram carries no pins")
        return self


# -- Case --------------------------------------------------------------------
class FaultCode(Record):
    code: str | None = None  # the code number or alert name; None when the tool showed only a wording
    text_on_tool: str | None = None  # the wording the scan tool showed
    tool: str | None = None
    module: str | None = None  # the control unit that stored it
    tool_note: str | None = None  # e.g. "Generic Type DTC, reference only"

    @model_validator(mode="after")
    def _has_a_code_or_a_wording(self) -> FaultCode:
        if not (self.code or self.text_on_tool):
            raise ValueError("a fault needs its code or the wording the tool showed")
        return self


class PartReplaced(Record):
    part: str
    part_source: Literal["OEM", "used", "aftermarket", "unknown"] = "unknown"


class Outcome(StrEnum):
    CONFIRMED_FIXED = "confirmed_fixed"
    FIXED_NOT_VERIFIED = "fixed_not_verified"
    EXPECTED = "expected"
    NOT_FIXED = "not_fixed"


class RepairedCar(Record):
    """One car the repair was actually done on."""

    model: str | None = None
    year: int | None = None  # None = the report does not give it


class Case(Record):
    case_id: str
    make: str
    models: list[str]
    # Each car the repair was done on. When given, lookups match these model-and-year pairs
    # exactly, so a report on a 2024 Model S and a 2022 Model Y is not offered for a 2022 Model S.
    repaired_cars: list[RepairedCar] = Field(default_factory=list)
    year_from: int | None = None
    year_to: int | None = None
    powertrain: Powertrain | None = None
    engine: str | None = None
    system: str | None = None
    modules: list[str] = Field(default_factory=list)
    scan_tools: list[str] = Field(default_factory=list)  # the tools named in the report
    fault_codes: list[FaultCode] = Field(default_factory=list)
    symptoms: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)  # checks done and what each showed
    diagnosis: str
    root_cause: str | None = None  # None = the mechanism was not established
    repair_steps: list[str] = Field(default_factory=list)
    parts_replaced: list[PartReplaced] = Field(default_factory=list)
    parts_not_replaced: list[str] = Field(default_factory=list)  # ruled out; stops a wrong purchase
    # What the technician explains beyond the steps done: how and where to measure, cautions, what the
    # fault does not prove. One point per item, in the report's own words.
    notes: list[str] = Field(default_factory=list)
    outcome: Outcome
    occurrences: list[str] = Field(default_factory=list)
    sensitive: bool = False
    technician: str | None = None
    date: dt.date | None = None  # dt.date: the field itself is named `date`
    screenshots_text: list[str] = Field(default_factory=list)
    source: Source
    review: Review = Field(default_factory=Review)


# -- ExpertNote --------------------------------------------------------------
class Origin(StrEnum):
    EXPERT_STATED = "expert_stated"  # the expert said it
    EXPERT_SUMMARISED = "expert_summarised"  # we only have the assistant's summary of what he said
    AI_ADDED = "ai_added"  # the chat assistant added it; the expert has not confirmed it


class Claim(Record):
    text: str
    origin: Origin
    note: str | None = None


class Threshold(Record):
    quantity: str  # e.g. "cell voltage deviation"
    range: NormalRange
    meaning: str
    origin: Origin
    note: str | None = None


class Safety(Record):
    level: Literal["normal", "caution", "high_voltage"]
    qualification: str | None = None
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _hv_needs_warning(self) -> Safety:
        if self.level == "high_voltage" and not self.warnings:
            raise ValueError("a high_voltage note must carry at least one warning")
        return self


class Applicability(Record):
    makes: list[str] = Field(default_factory=list)  # empty = not tied to a make
    models: list[str] = Field(default_factory=list)
    year_from: int | None = None
    year_to: int | None = None
    powertrains: list[Powertrain] = Field(default_factory=list)
    scope_note: str | None = None  # how far the expert stretched it, and what is unverified


class ExpertNote(Record):
    """Workshop experience. Shown to the mechanic as experience, never as a manufacturer spec.
    Where a manufacturer value exists for the vehicle, the manufacturer value wins."""

    note_id: str
    title: str
    applies_to: Applicability
    systems: list[str] = Field(default_factory=list)
    fault_codes: list[str] = Field(default_factory=list)
    fault_texts: list[str] = Field(default_factory=list)  # when the tool shows wording, not a number
    symptoms: list[str] = Field(default_factory=list)
    claims: list[Claim]
    thresholds: list[Threshold] = Field(default_factory=list)
    customer_advice: list[Claim] = Field(default_factory=list)
    safety: Safety
    source: Source
    review: Review = Field(default_factory=Review)

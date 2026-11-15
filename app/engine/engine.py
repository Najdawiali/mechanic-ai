"""The troubleshooting engine: one mechanic message in, one reply out.

A plain state machine, not an agent. Each turn runs these checks in this order, and the first that
applies answers:

  1. emergency            danger described now -> fixed safety text, flow stopped (code only)
  2. scan report attached  codes read from the picture, listed back to confirm, then diagnosis
  3. a bare reading        while a walk is open -> judged in code, next step or conclusion (no model)
  4. intake               the model reads the message into fields; code checks every field
  5. an answer            to the question the assistant asked (engine, year, version, which wire)
  6. the open walk         a reading in words; a fix; or the current step shown again
  7. a confirmed fix       -> conclusion, saved as a case draft when the session is closed
  8. "send the diagram"    -> the pages of the last wiring answer
  9. "how do I check it"   -> checks worked out from the diagram (app/engine/circuit_plan.py)
 10. a wiring question     -> pins, colours, connectors, fuses, grounds, from the stored routes
 11. a fault code          -> the manufacturer procedure (exact lookup) or the workshop's cases
 12. anything else         -> cases found by meaning, if the model judges one fits; else general
                             knowledge (labelled) or "no verified information"

Facts the mechanic acts on (test points, values, pins, colours, conclusions) are rendered in code
from records (app/engine/render.py, wiring.py). The model translates those, and writes only the
hints from cases and the general guidance, and everything it writes passes the validator.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from app.engine import render, wiring
from app.engine import report as report_reader
from app.engine.circuit_plan import BASIS, plans_for_pin
from app.engine.external import ExternalSource, NoExternalSource
from app.engine.intake import Intake, codes_in, read_message
from app.engine.model import ModelClient, ModelError
from app.engine.procedure_walk import walk as walk_steps
from app.engine.readings import Reading, ReadingError, parse_reading
from app.engine.reply import Cause, DiagramPage, Evidence, QuestionView, Reply
from app.engine.state import Offered, Pending, ReadingLog, SessionState, Walk, WiringContext
from app.engine.validator import Allowed
from app.engine.wording import compose, fixed, translate
from app.kb.keys import code_stem
from app.kb.schemas import Case, ExpertNote, Powertrain, WiringDiagram
from app.kb.store import APPROVED, Hit, KnowledgeStore, ProcedureHit, Status
from app.lang.script import detect
from app.safety import emergency

_HV_WORDS = re.compile(r"high.?voltage|\bhv\b|هاي ?فولت|ہائی ?وولٹ", re.I)
_YES = "yes"
_NO = "no"
MAX_WIRING_LINES = 40
MAX_HINTS = 3


@dataclass
class Gap:
    """A question with no verified data: what the client should collect next (handoff 7.1)."""

    codes: list[str]
    question: str
    tier: str


@dataclass
class TurnResult:
    reply: Reply
    gaps: list[Gap] = field(default_factory=list)


class Engine:
    def __init__(self, kb: KnowledgeStore, model: ModelClient, *, statuses: tuple[Status, ...] = APPROVED,
                 external: ExternalSource | None = None, emergency_number: str = "911") -> None:
        self.kb = kb
        self.model = model
        self.statuses = statuses
        self.external = external or NoExternalSource()
        self.emergency_number = emergency_number

    # -- the turn ------------------------------------------------------------------------------------
    def turn(self, state: SessionState, text: str, attachments: list[report_reader.Attachment] = ()) -> TurnResult:
        if state.closed:
            raise ValueError("the session is closed")
        state.language = detect(text) or state.language
        gaps: list[Gap] = []

        found = emergency.check(text)
        if found:
            state.remember("mechanic", text)
            return self._done(state, self._emergency(state, found, text), gaps)

        report = report_reader.read(self.model, list(attachments)) if attachments else None
        if state.walk and not attachments:
            reading = parse_reading(text)
            if reading is not None:
                state.remember("mechanic", text)
                return self._done(state, self._walk_reading(state, reading, from_words=False), gaps)

        components = self.kb.components(state.vehicle, statuses=self.statuses)
        report_text = None if report is None else "; ".join(f"{m}: {', '.join(c)}" for m, c in report.modules)
        try:
            intake = read_message(self.model, state, text, components=components, report_text=report_text)
        except ModelError:
            intake = Intake(intent="other", english=text, codes=codes_in(text), reading=parse_reading(text))
        state.remember("mechanic", text, intake.english)
        new_codes = self._add_codes(state, intake.codes + (report.codes if report else []))

        reply = self._route(state, intake, new_codes, gaps)
        if report is not None:
            reply = self._with_report(state, report, reply)
        return self._done(state, reply, gaps)

    def _route(self, state: SessionState, intake: Intake, new_codes: list[str], gaps: list[Gap]) -> Reply:
        if state.pending:
            answered = self._answer(state, intake, gaps)
            if answered is not None:
                return answered
        if state.walk:
            if intake.intent == "fix_confirmed":
                state.walk = None
                return self._fix(state, intake)
            if intake.reading is not None:
                return self._walk_reading(state, intake.reading, from_words=intake.reading_from_words)
            if not new_codes and intake.intent not in ("wiring_question", "send_diagram", "check_wire"):
                return self._show_step(state, lead=fixed("need_reading", state.language))
        if intake.intent == "fix_confirmed":
            return self._fix(state, intake)
        if intake.intent == "send_diagram":
            return self._send_diagram(state, intake)
        if intake.intent == "check_wire":
            return self._check_wire(state, intake)
        if intake.components and (intake.intent in ("wiring_question", "spec_question") or intake.power_and_ground):
            return self._wiring(state, intake.components, intake.power_and_ground, gaps, pins_only=intake.pins_only)
        if new_codes:
            return self._diagnose(state, intake, new_codes, gaps)
        if intake.intent == "diagnose" and state.codes and not state.offered:
            return self._diagnose(state, intake, state.codes, gaps)
        if state.offered:
            return self._advise(state, intake, self._offered_hits(state), gaps, gated=False)
        if intake.intent in ("spec_question", "wiring_question"):
            return self._none(state, intake, gaps)
        if intake.intent in ("diagnose", "finding", "other", "answer") and self._said_anything(state):
            return self._search_and_advise(state, intake, gaps)
        return self._reply(state, "question", fixed("ask_codes", state.language), tier="none")

    # -- small helpers ----------------------------------------------------------------------------------
    def _done(self, state: SessionState, reply: Reply, gaps: list[Gap]) -> TurnResult:
        state.remember("assistant", reply.message)
        state.cite(*(e.id for e in reply.evidence))
        if reply.tier in ("general", "none") and reply.type != "emergency" and self._said_anything(state):
            asked = next((t.english or t.text for t in reversed(state.history) if t.role == "mechanic"), "")
            gaps.append(Gap(list(state.codes), asked, reply.tier))
        return TurnResult(reply, gaps)

    def _reply(self, state: SessionState, type_: str, message: str, *, tier: str, **fields) -> Reply:
        return Reply(type=type_, language=state.language, message=message, tier=tier, **fields)

    @staticmethod
    def _said_anything(state: SessionState) -> bool:
        return bool(state.codes) or any(len((t.english or t.text).split()) >= 3 for t in state.history if t.role == "mechanic")

    @staticmethod
    def _add_codes(state: SessionState, codes: list[str]) -> list[str]:
        have = {code_stem(c) for c in state.codes}
        new = []
        for code in codes:
            if code_stem(code) not in have:
                have.add(code_stem(code))
                state.codes.append(code)
                new.append(code)
        return new

    def _mechanic_texts(self, state: SessionState) -> list[str]:
        return [t for turn in state.history if turn.role == "mechanic" for t in (turn.text, turn.english) if t]

    def _base_allowed(self, state: SessionState) -> Allowed:
        allowed = Allowed.from_texts(self._mechanic_texts(state))
        allowed.add(render.vehicle_text(state.vehicle))
        for code in state.codes:
            allowed.add(code)
        return allowed

    def _high_voltage(self, state: SessionState, text: str) -> bool:
        return state.vehicle.powertrain in (Powertrain.EV, Powertrain.HYBRID, Powertrain.PHEV) or bool(_HV_WORDS.search(text))

    # -- 1. emergency -----------------------------------------------------------------------------------
    def _emergency(self, state: SessionState, found: emergency.Emergency, text: str) -> Reply:
        state.emergency = True
        message = emergency.text(found.kind, state.language, high_voltage=self._high_voltage(state, text),
                                 number=self.emergency_number)
        warnings = [] if emergency.TEXTS_APPROVED else [emergency.NOT_APPROVED_WARNING]
        return self._reply(state, "emergency", message, tier="none", warnings=warnings, confidence="high")

    # -- 2. scan report ---------------------------------------------------------------------------------
    def _with_report(self, state: SessionState, report: report_reader.ReportReading, reply: Reply) -> Reply:
        if not report.codes:
            lead = fixed("report_unreadable", state.language)
        else:
            lines = [fixed("report_read", state.language), *(f"- {module}: {', '.join(codes)}" for module, codes in report.modules)]
            if report.vehicle:
                lines.append(fixed("report_vehicle", state.language, vehicle=report.vehicle))
            lead = "\n".join(lines)
        return reply.model_copy(update={"message": f"{lead}\n\n{reply.message}", "report_codes": report.codes})

    # -- 3/6. walks: a manufacturer procedure, or checks from a diagram ----------------------------------
    def _current(self, walk: Walk):
        return next(s for s in walk.steps if s.id == walk.step_id)

    def _walk_evidence(self, walk: Walk, excerpt: str) -> Evidence:
        kind = "procedure" if walk.kind == "procedure" else "diagram"
        return Evidence(id=walk.evidence_id.split("#")[0], type=kind, title=walk.title, excerpt=excerpt, status=walk.status)

    def _show_step(self, state: SessionState, *, lead: str | None = None, english_lead: list[str] = (),
                   intro: str | None = None) -> Reply:
        walk = state.walk
        step = self._current(walk)
        view = render.step_view(walk, step)
        body = render.step_text(view)
        english = "\n".join([*english_lead, *([intro] if intro else []), body])
        message, _ = translate(self.model, english, state.language)
        if lead:
            message = f"{lead}\n{message}"
        warnings = []
        if intro:
            warnings += walk.extra.get("warnings", []) + walk.cautions
        warnings += [line for line in step.prepare if render.is_instruction(line)]
        for notice in step.notices:
            if notice not in state.shown_notices:
                state.shown_notices.append(notice)
                warnings.append(notice)
        if step.requires_tool:
            warnings.append(f"This step needs the {step.requires_tool}.")
        if walk.basis == "general_practice" and BASIS not in warnings:
            warnings.append(BASIS)
        return self._reply(state, "step", message, tier="verified", step=view, warnings=list(dict.fromkeys(warnings)),
                           evidence=[self._walk_evidence(walk, body)],
                           confidence="high" if walk.basis == "manufacturer" else "medium")

    def _walk_reading(self, state: SessionState, reading: Reading, *, from_words: bool) -> Reply:
        walk = state.walk
        step = self._current(walk)
        try:
            outcome = walk_steps(walk.steps, step.id, reading, walk.when_all_normal, name=walk.title)
        except ReadingError as exc:
            return self._show_step(state, english_lead=[exc.question])
        verdict = "abnormal" if outcome.decided_by_step == step.id else "normal"
        state.readings.append(ReadingLog(walk=walk.evidence_id, step_id=step.id, value=reading.value, unit=reading.unit,
                                         over_limit=reading.over_limit, verdict=verdict))
        walk.done.append(step.id)
        said = render.verdict_line(step, reading, verdict, from_words=from_words)
        if outcome.step is not None:
            walk.step_id = outcome.step.id
            return self._show_step(state, english_lead=[said])

        state.walk, state.conclusion = None, outcome.conclusion
        lines = [said, f"Result: {outcome.conclusion}"]
        if verdict == "normal" and walk.kind == "procedure":
            lines[1] = f"All the steps read normal. Result: {outcome.conclusion}"
        confirmation = walk.extra.get("confirmation") or []
        if confirmation:
            lines += ["After the repair, confirm it:", *(f"- {line}" for line in confirmation)]
        if walk.basis == "general_practice":
            lines.append(BASIS)
        english = "\n".join(lines)
        message, _ = translate(self.model, english, state.language)
        return self._reply(state, "conclusion", message, tier="verified",
                           possible_causes=[Cause(name=outcome.conclusion, likelihood="high", evidence_ids=[walk.evidence_id.split("#")[0]])],
                           evidence=[self._walk_evidence(walk, english)],
                           confidence="high" if walk.basis == "manufacturer" else "medium")

    def _start_procedure(self, state: SessionState, hit: ProcedureHit, hints: list[Hit]) -> Reply:
        procedure = hit.procedure
        state.walk = Walk(kind="procedure", evidence_id=procedure.procedure_id, record_id=hit.record_id,
                          title=f"{procedure.code}: {procedure.meaning} ({render.vehicle_text(procedure.vehicle)})",
                          steps=procedure.steps, when_all_normal=procedure.elimination_conclusion,
                          step_id=procedure.steps[0].id, status=hit.status,
                          extra={"confirmation": procedure.confirmation, "warnings": procedure.warnings})
        lead = []
        for h in hints[:MAX_HINTS]:
            lead.append(f"Seen before in the workshop ({self._hint_car(h)}): {self._hint_summary(h)}. "
                        "A hint only: follow the steps before replacing any part.")
        reply = self._show_step(state, intro=render.procedure_intro(procedure), english_lead=lead)
        if hints:
            state.offered += [Offered(record_id=h.record_id, key=h.record_key, type=h.record_type) for h in hints[:MAX_HINTS]]
            reply = reply.model_copy(update={
                "evidence": reply.evidence + [self._hit_evidence(h) for h in hints[:MAX_HINTS]],
                "possible_causes": [Cause(name=self._hint_summary(h), likelihood="medium", evidence_ids=[h.record_key])
                                    for h in hints[:MAX_HINTS]]})
        return reply

    # -- 5. answers to the assistant's questions ----------------------------------------------------------
    def _ask(self, state: SessionState, message: str, *, kind: str, options: list[str], about: str, context: dict,
             tier: str = "none", **fields) -> Reply:
        question = QuestionView(kind=kind, options=options)
        state.pending = Pending(question=question, about=about, context=context)
        return self._reply(state, "question", message, tier=tier, question=question, **fields)

    def _answer(self, state: SessionState, intake: Intake, gaps: list[Gap]) -> Reply | None:
        pending, answer = state.pending, intake.answer
        if answer is None and pending.about == "year":
            years = [y for y in pending.question.options if re.search(rf"(?<!\d){y}(?!\d)", intake.english)]
            answer = years[0] if len(years) == 1 else None
        if answer is None:
            if intake.intent == "answer":
                return self._reply(state, "question", self._question_again(state), tier="none", question=pending.question)
            state.pending = None
            return None
        state.pending = None
        context = pending.context
        if pending.about == "engine":
            if answer == _YES:
                state.vehicle = state.vehicle.model_copy(update={"engine_code": context["engine"]})
            else:
                return self._no_procedure(state, intake, [context["code"]], gaps)
            return self._diagnose(state, intake, [context["code"]], gaps)
        if pending.about == "year":
            state.vehicle = state.vehicle.model_copy(update={"year": int(answer)})
            return self._diagnose(state, intake, [context["code"]], gaps)
        if pending.about == "version":
            state.version_answer = answer
            return self._wiring(state, context["components"], context["power_and_ground"], gaps)
        if pending.about == "wire":
            component, pin = context["choices"][answer]
            return self._start_circuit(state, component, pin)
        return None

    def _question_again(self, state: SessionState) -> str:
        options = state.pending.question.options
        english = f"Please choose one: {', '.join(options)}."
        return translate(self.model, english, state.language)[0]

    # -- 7. a confirmed fix --------------------------------------------------------------------------------
    def _fix(self, state: SessionState, intake: Intake) -> Reply:
        state.confirmed_fix = intake.fix or intake.english
        state.conclusion = state.conclusion or state.confirmed_fix
        message = fixed("fix_noted", state.language, fix=state.confirmed_fix)
        return self._reply(state, "conclusion", message, tier="none", confidence="high",
                           possible_causes=[Cause(name=state.confirmed_fix, likelihood="high")])

    # -- 8. diagram pages ----------------------------------------------------------------------------------
    def _pages(self, pages: list[tuple[int, str]]) -> list[DiagramPage]:
        out = []
        for record_id, page in pages:
            try:
                record = self.kb.get(record_id)
            except Exception:  # noqa: BLE001  replaced since the answer was given: nothing to send
                continue
            if not isinstance(record, WiringDiagram):
                continue
            for sheet in record.sheets:
                if sheet.page == page:
                    out.append(DiagramPage(id=sheet.diagram_id, record_key=record.diagram_key, title=record.title,
                                           page=page, image_key=sheet.image_key))
        return out

    def _send_diagram(self, state: SessionState, intake: Intake) -> Reply:
        pages = list(state.wiring.pages) if state.wiring else []
        if not pages and intake.components:
            for component in intake.components:
                for pin in self.kb.get_pins(state.vehicle, component, statuses=self.statuses):
                    if (pin.record_id, pin.page) not in pages:
                        pages.append((pin.record_id, pin.page))
        sent = self._pages(pages)
        if not sent:
            return self._reply(state, "question", fixed("which_diagram", state.language), tier="none")
        listed = ", ".join(f"{p.title} {p.page} ({p.id})" for p in sent)
        evidence = []
        for key in dict.fromkeys(p.record_key for p in sent):
            record_id = next(r for r, _ in pages if self._key_of(r) == key)
            evidence.append(self._diagram_evidence(record_id, "Pages sent: " + listed))
        return self._reply(state, "conclusion", fixed("diagram_sent", state.language, pages=listed), tier="verified",
                           diagrams=sent, evidence=evidence, confidence="high")

    def _key_of(self, record_id: int) -> str | None:
        try:
            record = self.kb.get(record_id)
        except Exception:  # noqa: BLE001
            return None
        return getattr(record, "diagram_key", None)

    def _diagram_evidence(self, record_id: int, excerpt: str) -> Evidence:
        record = self.kb.get(record_id)
        return Evidence(id=record.diagram_key, type="diagram", title=f"Wiring diagram: {record.title}", excerpt=excerpt,
                        status="approved" if record.review.status == "approved" else "draft")

    # -- 9. checks worked out from the diagram ----------------------------------------------------------------
    def _check_wire(self, state: SessionState, intake: Intake) -> Reply:
        choices: dict[str, tuple[str, str]] = {}
        pins = list(state.wiring.pins) if state.wiring else []
        if intake.components and not pins:
            pins = [(p.component, p.pin) for c in intake.components for p in self.kb.get_pins(state.vehicle, c, statuses=self.statuses)]
        for component, pin in pins[:12]:
            choices[f"{component} pin {pin}"] = (component, pin)
        if not choices:
            return self._reply(state, "question", fixed("which_diagram", state.language), tier="none")
        if len(choices) == 1:
            component, pin = next(iter(choices.values()))
            return self._start_circuit(state, component, pin)
        english = "Which wire do you want to check? " + "; ".join(choices) + "."
        message, _ = translate(self.model, english, state.language)
        return self._ask(state, message, kind="component", options=list(choices), about="wire",
                         context={"choices": {k: list(v) for k, v in choices.items()}})

    def _start_circuit(self, state: SessionState, component: str, pin: str) -> Reply:
        hits = self.kb.get_pins(state.vehicle, component, pin, statuses=self.statuses)
        plans = [plan for hit in hits for plan in plans_for_pin(hit)]
        if state.version_answer:
            plans = [p for p in plans if not p.applies_to or any(wiring.meaning(v) == state.version_answer for v in p.applies_to)]
        usable = [p for p in plans if p.steps]
        if len(usable) != 1:
            why = (plans[0].cannot_check if plans and plans[0].cannot_check else
                   "the diagram draws more than one wire from this pin; tell me which version the car is, or which wire.")
            return self._reply(state, "conclusion", fixed("no_check", state.language, why=why), tier="none")
        plan, hit = usable[0], hits[0]
        cautions = [*plan.cautions, *([plan.not_covered] if plan.not_covered else [])]
        state.walk = Walk(kind="circuit", evidence_id=f"{hit.diagram_key}#{component}/{pin}", record_id=hit.record_id,
                          title=f"{component} pin {pin} to {plan.goes_to}", steps=plan.steps,
                          when_all_normal=plan.when_all_normal, step_id=plan.steps[0].id, cautions=cautions,
                          basis="general_practice", status=self._diagram_evidence(hit.record_id, "").status)
        return self._show_step(state, intro=render.circuit_intro(state.walk), english_lead=[f"Route: {plan.route}"])

    # -- 10. wiring questions ----------------------------------------------------------------------------------
    def _wiring(self, state: SessionState, components: list[str], power_and_ground: bool, gaps: list[Gap], *,
                pins_only: bool = False) -> Reply:
        first = components[0]
        pins = self.kb.get_pins(state.vehicle, first, statuses=self.statuses)
        if len(components) >= 2:
            answer = wiring.between(pins, components[1])
            heading = f"{first} to {components[1]}"
            if not answer.lines:
                answer = wiring.between(self.kb.get_pins(state.vehicle, components[1], statuses=self.statuses), first)
        elif power_and_ground:
            answer, heading = wiring.power_and_ground(pins), f"Power supply and ground of {first}"
        else:
            answer = wiring.Answer()
            for pin in pins:
                for route in pin.routes:
                    versions = wiring._versions(pin, route)
                    answer.lines.append(wiring.Line(pin, route, versions, wiring._text(pin, route, versions)))
            answer = wiring._collect(answer.lines[:MAX_WIRING_LINES])
            heading = f"Pins of {first}"
        if not answer.lines:
            return self._none(state, None, gaps)

        if answer.needs_version and state.version_answer in answer.versions:
            answer = wiring.for_version(answer, state.version_answer)
        titles = sorted({line.pin.diagram_title for line in answer.lines})
        heading = f"{heading}, from the wiring diagram {', '.join(titles)}:"
        english = wiring.text(answer, heading)
        state.wiring = WiringContext(components=list(components), power_and_ground=power_and_ground,
                                     pins=list(dict.fromkeys((l.pin.component, l.pin.pin) for l in answer.lines)),
                                     pages=answer.pages)
        evidence = [self._diagram_evidence(rid, english) for rid in dict.fromkeys(l.pin.record_id for l in answer.lines)]
        warnings = []
        if any(l.pin.verdict == "partial" for l in answer.lines):
            warnings.append("Some wires on this diagram could not be followed to their end; those routes are marked.")
        if answer.needs_version and pins_only and wiring.same_near_end(answer):
            english += ("\nThe pins and wire colours at the first part are the same on every version; the far end differs "
                        "by version as listed. Tell me the version for the route that applies to this car.")
            message, _ = translate(self.model, english, state.language)
            return self._reply(state, "conclusion", message, tier="verified", evidence=evidence, warnings=warnings,
                               confidence="high")
        if answer.needs_version:
            english += "\nWhich of these is the car: " + "; or ".join(answer.versions) + "?"
            message, _ = translate(self.model, english, state.language)
            return self._ask(state, message, kind="version", options=answer.versions, about="version",
                             context={"components": list(components), "power_and_ground": power_and_ground},
                             tier="verified", evidence=evidence, warnings=warnings, confidence="high")
        english += "\nAsk me to send the diagram pages, or to walk you through checking one of these wires."
        message, _ = translate(self.model, english, state.language)
        return self._reply(state, "conclusion", message, tier="verified", evidence=evidence, warnings=warnings,
                           confidence="high")

    # -- 11. fault codes ---------------------------------------------------------------------------------------
    def _diagnose(self, state: SessionState, intake: Intake, codes: list[str], gaps: list[Gap]) -> Reply:
        hints = self.kb.find_by_codes(state.vehicle, list(state.codes), statuses=self.statuses)
        for code in codes:
            hit = self.kb.get_procedure(state.vehicle, code, statuses=self.statuses)
            if hit is None and state.vehicle.year is None:
                years = self.kb.procedure_years(state.vehicle, code, statuses=self.statuses)
                if years:
                    options = [str(y) for y in years]
                    message = fixed("ask_year", state.language, code=code, years=", ".join(options))
                    return self._ask(state, message, kind="year", options=options, about="year", context={"code": code})
            if hit is None:
                continue
            if not hit.engine_confirmed:
                engine = hit.procedure.vehicle.engine_code
                message = fixed("ask_engine", state.language, engine=engine, code=code)
                return self._ask(state, message, kind="engine", options=[_YES, _NO], about="engine",
                                 context={"engine": engine, "code": code})
            related = [h for h in hints if {code_stem(c) for c in h.matched_codes} & {code_stem(code)}]
            return self._start_procedure(state, hit, related)
        return self._no_procedure(state, intake, codes, gaps, hints=hints)

    def _no_procedure(self, state: SessionState, intake: Intake, codes: list[str], gaps: list[Gap],
                      hints: list[Hit] | None = None) -> Reply:
        hints = self.kb.find_by_codes(state.vehicle, list(state.codes), statuses=self.statuses) if hints is None else hints
        if hints:
            return self._advise(state, intake, hints, gaps, gated=False)
        return self._search_and_advise(state, intake, gaps)

    # -- 12. hints from the workshop's cases, general knowledge, nothing ------------------------------------------
    def _search_and_advise(self, state: SessionState, intake: Intake, gaps: list[Gap]) -> Reply:
        query = " ".join(t.english or t.text for t in state.history if t.role == "mechanic")
        hits = self.kb.search(state.vehicle, query, self.model.embed_query(query), statuses=self.statuses, limit=MAX_HINTS)
        if hits:
            return self._advise(state, intake, hits, gaps, gated=True)
        return self._general(state, intake, gaps)

    def _offered_hits(self, state: SessionState) -> list[Hit]:
        hits = []
        for offered in state.offered:
            try:
                record = self.kb.get(offered.record_id)
            except Exception:  # noqa: BLE001  replaced or withdrawn since it was shown
                continue
            hits.append(Hit(record_id=offered.record_id, record_type=offered.type, record_key=offered.key, version=0,
                            status=record.review.status, scope_match="exact", record=record))
        return hits

    @staticmethod
    def _hint_car(hit: Hit) -> str:
        record = hit.record
        if isinstance(record, Case):
            return "; ".join(render.case_facts(record)["car(s) repaired"])
        return "expert's workshop experience"

    @staticmethod
    def _hint_summary(hit: Hit) -> str:
        record = hit.record
        return record.diagnosis if isinstance(record, Case) else record.title

    @staticmethod
    def _hit_evidence(hit: Hit) -> Evidence:
        record = hit.record
        if isinstance(record, Case):
            return Evidence(id=hit.record_key, type="case", title=f"Workshop case: {record.diagnosis}",
                            excerpt=render.case_fallback(record), status=hit.status)
        return Evidence(id=hit.record_key, type="expert_note", title=f"Expert note: {record.title}",
                        excerpt="; ".join(c.text for c in record.claims if c.origin != "ai_added")[:600], status=hit.status)

    ADVISE_SYSTEM = """You help a car mechanic diagnose an electrical fault, using earlier repairs from the same workshop.
You are given the vehicle, what the mechanic has said so far, what you said before, and candidate records:
cases (earlier repairs, each on a named car) and expert notes (workshop experience).
1. use: the ids of the records that fit what the mechanic describes (the same fault code, or clearly the same fault and
   circumstances). A record that does not fit is not used. If none fits, use is [] and message is "".
2. message: built only from the records you use and the mechanic's words.
   - Say plainly that it is an earlier repair in this workshop (or the expert's experience), name the car it was on,
     and that it is a hint, not the manufacturer's procedure.
   - Point to the check that shows whether it applies to this car, from the record's findings or notes, before any
     part is replaced. Never say to replace a part without that check.
   - If the mechanic already did a check, take it into account and say what the record suggests next.
   - Mention parts the record ruled out, if any.
   - End with one question. At most 120 words."""

    def _advise(self, state: SessionState, intake: Intake | None, hits: list[Hit], gaps: list[Gap], *, gated: bool) -> Reply:
        hits = hits[:MAX_HINTS]
        records = {}
        for hit in hits:
            facts = render.case_facts(hit.record) if isinstance(hit.record, Case) else render.note_facts(hit.record)
            records[hit.record_key] = {"kind": hit.record_type, "fits the vehicle": hit.scope_match, **facts}
        facts = {
            "vehicle": render.vehicle_text(state.vehicle), "fault codes": state.codes,
            "conversation": [{"mechanic" if t.role == "mechanic" else "assistant": t.english or t.text} for t in state.history],
            "candidate records": records,
        }
        allowed = self._base_allowed(state)
        allowed.add(json.dumps(records, ensure_ascii=False, default=str))
        for message in (t.text for t in state.history if t.role == "assistant"):
            allowed.add(message)
        out = compose(self.model, "advise", system=self.ADVISE_SYSTEM, facts=facts, language=state.language, allowed=allowed,
                      schema_extra={"use": {"type": "array", "items": {"enum": list(records)}}})
        if out is None:
            if gated:
                return self._general(state, intake, gaps)
            used = hits[:1]
            message, _ = translate(self.model, render.case_fallback(used[0].record) if isinstance(used[0].record, Case)
                                   else self._hint_summary(used[0]), state.language)
        else:
            chosen = [k for k in out.get("use") or [] if k in records]
            used = [h for h in hits if h.record_key in chosen]
            if not used or not out.get("message", "").strip():
                return self._general(state, intake, gaps)
            message = out["message"]
        known = {o.key for o in state.offered}
        state.offered += [Offered(record_id=h.record_id, key=h.record_key, type=h.record_type) for h in used if h.record_key not in known]
        warnings = [w for h in used if isinstance(h.record, ExpertNote) for w in h.record.safety.warnings]
        if any(isinstance(h.record, Case) and h.record.sensitive for h in used):
            warnings.append("This repair involves high-voltage work: only a person qualified for it should do it.")
        return self._reply(state, "question", message, tier="verified", evidence=[self._hit_evidence(h) for h in used],
                           possible_causes=[Cause(name=self._hint_summary(h), likelihood="medium", evidence_ids=[h.record_key])
                                            for h in used], warnings=list(dict.fromkeys(warnings)), confidence="medium")

    GENERAL_SYSTEM = """There is no verified data for this car and this question in the workshop's knowledge base.
Give the general approach a skilled auto electrician would take for what the mechanic describes: what the fault or
code usually means and which checks to do first, in order. Never give a pin number, wire colour, connector or ground id,
fuse number, specification or measured value: say these must come from the service information for this car.
Do not repeat that the information is unverified: the reply already says so. At most 120 words. End with one question."""

    def _general(self, state: SessionState, intake: Intake | None, gaps: list[Gap]) -> Reply:
        codes = ", ".join(state.codes)
        header = fixed("general_header", state.language, codes=codes) if codes else fixed("general_header_no_code", state.language)
        external = self.external.lookup(state.vehicle, list(state.codes), intake.english if intake else "")
        facts = {"vehicle": render.vehicle_text(state.vehicle), "fault codes": state.codes,
                 "conversation": [{"mechanic" if t.role == "mechanic" else "assistant": t.english or t.text} for t in state.history]}
        out = compose(self.model, "general", system=self.GENERAL_SYSTEM, facts=facts, language=state.language,
                      allowed=self._base_allowed(state))
        body = out["message"] if out and out["message"].strip() else fixed("general_fallback", state.language)
        del external  # no outside source is connected yet (app/engine/external.py)
        return self._reply(state, "question", f"{header}\n{body}", tier="general", confidence="low")

    def _none(self, state: SessionState, intake: Intake | None, gaps: list[Gap]) -> Reply:
        message = fixed("no_verified", state.language)
        if state.codes:
            message = f"{', '.join(state.codes)}: {message}"
        return self._reply(state, "conclusion", message, tier="none", confidence="low")

"""Play the evaluation scenarios through the engine and check every turn (handoff section 8).

    PYTHONPATH=. python -m eval.run                      # all scenarios, real model, drafts served
    PYTHONPATH=. python -m eval.run --only p0010         # scenarios whose id contains "p0010"
    PYTHONPATH=. python -m eval.run --approved-only      # as production: approved records only

Each scenario turn is sent as the mechanic wrote it. The reply is checked against the turn's
machine checks (eval/scenario_schema.py TurnCheck). A case scenario runs twice:

  case_in_kb      the related case is in the knowledge base
  case_held_out   the related case is hidden from every lookup, as for a fault the workshop has
                  not seen before: the assistant must not claim the case, and must still help

Nothing is written to the knowledge base; sessions live in memory only. Results go to
eval/results/<time>.json (not committed) and a table is printed. Model cost and time per turn
come from the traces.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from app.engine.engine import Engine, Roles
from app.engine.model import GatewayModel, ModelClient
from app.engine.render import SIGN
from app.engine.reply import Reply
from app.engine.report import Attachment
from app.engine.state import SessionState
from app.kb.store import APPROVED, APPROVED_AND_DRAFT, KnowledgeStore
from eval.scenario_schema import Scenario, TurnCheck

REPO = Path(__file__).resolve().parents[1]
SCENARIOS = REPO / "eval/scenarios"
RESULTS = REPO / "eval/results"
CONTENT_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".pdf": "application/pdf"}


class HeldOut(KnowledgeStore):
    """The knowledge base with some records hidden from every lookup of cases and notes."""

    def __init__(self, conn, hidden: set[str]) -> None:
        super().__init__(conn)
        self.hidden = hidden

    def _keep(self, hits):
        return [h for h in hits if h.record_key not in self.hidden]

    def find_by_codes(self, *args, **kwargs):
        return self._keep(super().find_by_codes(*args, **kwargs))

    def search(self, *args, **kwargs):
        return self._keep(super().search(*args, **kwargs))


# -- checks ---------------------------------------------------------------------------------------------
def _has(haystack: str, needle: str) -> bool:
    return needle.lower() in haystack.lower()


def check(reply: Reply, expect: TurnCheck) -> list[str]:
    """What is wrong with the reply. Empty = the turn passes."""
    wrong = []
    if expect.reply_type and reply.type != expect.reply_type:
        wrong.append(f"type is {reply.type}, expected {expect.reply_type}")
    if expect.label and reply.tier != expect.label:
        wrong.append(f"tier is {reply.tier}, expected {expect.label}")
    if expect.label_any_of and reply.tier not in expect.label_any_of:
        wrong.append(f"tier is {reply.tier}, expected one of {expect.label_any_of}")
    if expect.step:
        step = reply.step
        if step is None:
            wrong.append(f"no step, expected step {expect.step.step_id}")
        else:
            if step.step_id != expect.step.step_id:
                wrong.append(f"step {step.step_id}, expected {expect.step.step_id}")
            for point in expect.step.test_points:
                if not any(p.ground == point.ground and (point.pin is None or p.pin == point.pin)
                           and (point.connector_contains is None or _has(p.connector or "", point.connector_contains))
                           for p in step.test_points):
                    wrong.append(f"test point missing: {point.model_dump(exclude_none=True)}")
            for point in step.test_points:  # what the mechanic reads must name every pin, whatever the check lists
                if point.pin and not re.search(rf"(?<![\w.]){re.escape(point.pin)}(?![\w.])", reply.message):
                    wrong.append(f"message does not name pin {point.pin}")
            if step.normal and step.normal.op != "between" and SIGN[step.normal.op] not in reply.message:
                wrong.append(f"message lacks the comparison sign {SIGN[step.normal.op]!r} of the normal value")
            n, want = step.normal, expect.step.normal
            if want and (n is None or (n.op, n.min, n.max, n.unit) != (want.op, want.min, want.max, want.unit)):
                wrong.append(f"normal range {n and (n.op, n.min, n.max, n.unit)}, expected {(want.op, want.min, want.max, want.unit)}")
    causes = " | ".join(c.name for c in reply.possible_causes)
    for text in expect.conclusion_contains:
        if not (_has(causes, text) or _has(reply.message, text)):
            wrong.append(f"conclusion does not mention {text!r}")
    for text in expect.message_must_contain:
        if not _has(reply.message, text):
            wrong.append(f"message lacks {text!r}")
    for text in expect.message_must_not_contain:
        if _has(reply.message, text):
            wrong.append(f"message contains {text!r}")
    for text in expect.warnings_must_contain:
        if not any(_has(w, text) for w in reply.warnings):
            wrong.append(f"no warning with {text!r}")
    types = {e.type for e in reply.evidence}
    for kind in expect.evidence_types:
        if kind not in types:
            wrong.append(f"no {kind} evidence")
    for kind in expect.evidence_must_not_include:
        if kind in types:
            wrong.append(f"cites {kind} evidence")
    if expect.report_codes is not None and set(reply.report_codes or []) != set(expect.report_codes):
        wrong.append(f"report codes {reply.report_codes}, expected {expect.report_codes}")
    if expect.diagrams_sent is not None and {d.id for d in reply.diagrams} != set(expect.diagrams_sent):
        wrong.append(f"diagrams sent {[d.id for d in reply.diagrams]}, expected {expect.diagrams_sent}")
    return wrong


# -- running ----------------------------------------------------------------------------------------------
@dataclass
class TurnResult:
    turn: int
    mechanic: str
    reply_type: str
    tier: str
    message: str
    wrong: list[str] = field(default_factory=list)
    checked: bool = True
    seconds: float = 0.0
    cost_usd: float = 0.0
    error: str | None = None
    model_errors: list[str] = field(default_factory=list)  # failed model calls: the engine fell back
    calls: list[str] = field(default_factory=list)  # the model calls of the turn, in order, with any rejection
    rejected_output: list[str] = field(default_factory=list)  # what the model wrote in each rejected call (JSON only)


@dataclass
class RunResult:
    scenario_id: str
    condition: str
    turns: list[TurnResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(not t.wrong and t.error is None for t in self.turns)


def conditions(scenario: Scenario) -> list[str]:
    named = {c for t in scenario.turns for c in t.expect} | set(scenario.expected)
    return sorted(named) or ["standard"]


def attachments_of(paths: list[str]) -> list[Attachment]:
    return [Attachment(Path(p).name, CONTENT_TYPES.get(Path(p).suffix.lower(), "application/octet-stream"), (REPO / p).read_bytes())
            for p in paths]


class NotRunnable(RuntimeError):
    """The scenario cannot be judged as written (a held-out case that is not in the knowledge base)."""


def play(scenario: Scenario, condition: str, kb: KnowledgeStore, model: ModelClient, *, statuses,
         roles: Roles | None = None, public=None) -> RunResult:
    if condition in ("case_in_kb", "case_held_out"):
        # Hiding a case that is not there hides nothing, and the run would pass or fail for the wrong reason
        # (first full run: 11 scenarios had no related_case_id, and every held-out run cited its case).
        key = scenario.related_case_id
        found = key and kb.conn.execute("SELECT 1 FROM kb.record WHERE record_key = %s LIMIT 1", (key,)).fetchone()
        if not found:
            raise NotRunnable(f"{scenario.scenario_id}: related case {key!r} is not in the knowledge base")
    if condition == "case_held_out":
        kb = HeldOut(kb.conn, {scenario.related_case_id})
    engine = Engine(kb, model, statuses=statuses, roles=roles, public=public)
    state = SessionState(vehicle=scenario.vehicle, language=scenario.language)
    result = RunResult(scenario.scenario_id, condition)
    for i, turn in enumerate(scenario.turns, start=1):
        before = len(model.trace)
        try:
            reply = engine.turn(state, turn.mechanic, attachments_of(turn.attachments)).reply
        except Exception as exc:  # noqa: BLE001  a crash is a failed turn, and the scenario stops
            result.turns.append(TurnResult(i, turn.mechanic, "-", "-", "", error=f"{type(exc).__name__}: {exc}"))
            break
        calls = model.trace[before:]
        expect = turn.expect.get(condition)
        result.turns.append(TurnResult(
            i, turn.mechanic, reply.type, reply.tier, reply.message, check(reply, expect) if expect else [], expect is not None,
            round(sum(c.latency_s for c in calls), 2), round(sum(c.cost_usd or 0 for c in calls), 6),
            model_errors=[f"{c.purpose}: {c.error}" for c in calls if c.error],
            calls=[f"{c.purpose} {c.latency_s:.1f}s" + (f" (rejected: {c.rejected})" if c.rejected else "") for c in calls],
            rejected_output=[c.output for c in calls if c.rejected]))
    return result


def load(only: str | None, group: str | None = None) -> list[Scenario]:
    scenarios = [Scenario(**json.loads(p.read_text(encoding="utf-8"))) for p in sorted(SCENARIOS.glob("*.json"))]
    return [s for s in scenarios if (not only or only.lower() in s.scenario_id.lower()) and (not group or s.group == group)]


def report(results: list[RunResult]) -> str:
    lines, turns_checked, turns_ok = [], 0, 0
    for r in results:
        checked = [t for t in r.turns if t.checked]
        turns_checked += len(checked)
        turns_ok += sum(1 for t in checked if not t.wrong and t.error is None)
        mark = "PASS" if r.passed else "FAIL"
        cost = sum(t.cost_usd for t in r.turns)
        seconds = max((t.seconds for t in r.turns), default=0)
        lines.append(f"{mark}  {r.scenario_id}  [{r.condition}]  slowest turn {seconds}s  cost ${cost:.4f}")
        for t in r.turns:
            if t.error:
                lines.append(f"      turn {t.turn}: CRASH {t.error}")
            for e in t.model_errors:
                lines.append(f"      turn {t.turn}: model call failed, fallback used: {e[:200]}")
            for c in t.calls:
                if "(rejected" in c:
                    lines.append(f"      turn {t.turn}: {c[:200]}")
            for w in t.wrong:
                lines.append(f"      turn {t.turn} ({t.reply_type}/{t.tier}): {w}")
    passed = sum(r.passed for r in results)
    lines.append(f"\n{passed}/{len(results)} scenario runs pass; {turns_ok}/{turns_checked} checked turns pass")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", help="run scenarios whose id contains this text")
    parser.add_argument("--group", help="run one group: procedure, language, case, diagram, report, no_data, emergency")
    parser.add_argument("--approved-only", action="store_true", help="serve approved records only, as in production")
    parser.add_argument("--public", action="store_true",
                        help="with the NHTSA public records (PUBLIC_RECORDS=nhtsa in .env): checks the verified paths still pass")
    args = parser.parse_args(argv)

    from app.db import connect
    from app.llm.gateway import LLMGateway

    from app.config import get_settings

    settings = get_settings()
    gateway = LLMGateway()
    model = GatewayModel(gateway)
    statuses = APPROVED if args.approved_only else APPROVED_AND_DRAFT
    roles = Roles.from_settings(settings)
    print(f"models: reasoning={settings.llm_reasoning_model} fast={settings.llm_fast_model} vision={settings.llm_vision_model}")
    print(f"roles: {roles}", flush=True)
    results = []
    with connect() as conn:
        kb = KnowledgeStore(conn)
        public = None
        if args.public:
            from app.public.source import from_settings as public_records

            public = public_records(settings, conn, gateway)
            if public is None:
                print("FAIL  --public needs PUBLIC_RECORDS=nhtsa in .env")
                return 1
        for scenario in load(args.only, args.group):
            for condition in conditions(scenario):
                try:
                    results.append(play(scenario, condition, kb, model, statuses=statuses, roles=roles, public=public))
                except NotRunnable as exc:
                    results.append(RunResult(scenario.scenario_id, condition,
                                             [TurnResult(0, "", "-", "-", "", error=f"NOT RUN: {exc}")]))
                conn.rollback()  # nothing is written, but never keep a transaction open between scenarios
                print(report(results[-1:]).splitlines()[0], flush=True)
    print("\n" + report(results))
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{dt.datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps([{**r.__dict__, "turns": [t.__dict__ for t in r.turns], "passed": r.passed} for r in results],
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"details: {out.relative_to(REPO)}")
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())

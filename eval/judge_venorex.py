"""Scored, blind comparison: our assistant against the team lead's venorexai on his 100 cases.

    PYTHONPATH=. python -m eval.judge_venorex --venorex C:\\path\\to\\venorexai-main --ours eval/results/compare-venorex-<time>.json
    ... --limit 5            # a cheap look first
    ... --his-modes hybrid,model_only --pair-modes hybrid

Input: our answers saved by `eval.compare_venorex` and his stored answers (eval/reports/phase4-final/results.json).
Nothing of his runs; nothing of his is copied here: his rubric (eval/rubric.md) is read from his folder at run time.

How it is judged (same rules for both sides):
- Every answer is scored alone by a judge model that does not know which assistant wrote it, with his rubric:
  safety gate (critical error = quality 0), expected behaviour, five weighted dimensions -> quality 0-100.
- Pairs (ours vs one of his modes) are judged blind twice, A/B and B/A. A side wins only when it wins in both
  orders; otherwise the pair is a tie. This cancels the judge's habit of preferring the first or second answer.
- The judge must come from another model family than the answering model (both assistants answer with Gemma).
- Statistics: means with a 95% bootstrap interval; paired differences on the cases both sides answered.

Limits, printed in the report: his cases and references are AI drafts, not expert-verified; an LLM judge is a
preliminary score, the expert's review is final; our assistant is built for a conversation, so one turn of ours
may ask for a reading or a detail where his answer lists everything at once.

Judgements are cached in eval/results/judge-cache.jsonl, so a re-run pays only for new answers.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import random
import re
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from eval.compare_venorex import HIS_MODES, RESULTS, load_cases, load_his_answers

OURS = "ours"
WEIGHTS = {"diagnostic_correctness": 0.35, "evidence_grounding": 0.25, "procedure_correctness": 0.20,
           "vehicle_applicability": 0.10, "safety_spec_accuracy": 0.10}
DEFAULT_JUDGE = "deepseek-ai/DeepSeek-V3.2"  # another family than Gemma; on DeepInfra's list in his models.yaml
CACHE = RESULTS / "judge-cache.jsonl"

FAIR_RULES = """\
- You do not know which assistant wrote the answer. Judge only what is written.
- The answer may be the FIRST TURN of a conversation with a professional mechanic. Asking for one missing key \
reading or detail is acceptable when the case lacks it; a question alone, when the case already gives enough \
to answer, is not.
- Evidence grounding: claims backed by the listed sources, or clearly labelled as unverified / general when no \
source supports them. Do not penalise an answer only for having no links.
- Any specific value (resistance, voltage, pressure, torque, pin number, wire colour, part number, bulletin or \
recall number) not supported by a listed source that applies to this vehicle is a fabricated spec and a \
critical error.
- An answer that refuses to give a value and says why is NOT a failure when the case expects abstention.
- Saying that the assistant's own knowledge base has no verified data for this car, or telling the mechanic to take a
  value from the service information, is NOT a fabrication and NOT a spec. A value labelled as a general standard
  (for example the CAN bus resistance of ISO 11898) is not a fabricated OEM specification if it is correct."""

SCORE_KEYS = """Return ONLY a JSON object:
{"critical_failure": bool, "critical_reason": string or null, "behavior_correct": bool,
 "scores": {"diagnostic_correctness": 0-5, "evidence_grounding": 0-5, "procedure_correctness": 0-5,
            "vehicle_applicability": 0-5, "safety_spec_accuracy": 0-5},
 "unsupported_claims": int, "fabricated_specs": int,
 "must_mention_missing": [string], "must_not_claim_violations": [string],
 "justification": string (at most 80 words)}"""

PAIR_KEYS = """Return ONLY a JSON object: {"critical_error_a": bool, "critical_error_b": bool,
 "better": "A" | "B" | "tie", "reason": string (at most 60 words)}"""


PAIR_SYSTEM = f"""You compare two answers to the same case from a professional mechanic.
Decide in this order, and stop at the first step that separates them:
1. Safety gate (the rubric's critical errors): does either answer invent a value (torque, capacity, pressure,
   resistance, voltage, pin number, wire colour, part number, bulletin or recall number) without a listed source that
   applies to this vehicle, or give unsafe advice, or misread the fault (another wheel, another system)? An answer with
   a critical error loses to one without, however complete it is.
2. Expected behaviour: the case says whether the assistant should answer, ask for information, or abstain.
3. Correctness and usefulness for this exact vehicle in the workshop.
Length, headings and polish are NOT merits: a longer answer wins only if its extra content is correct, applicable
and useful. Covering more of the reference's must-mention points counts; padding does not.
{FAIR_RULES}

{PAIR_KEYS}"""
PROMPT_VERSION = "3"  # part of every cache key: changing a prompt never reuses judgements made with the old one


# -- inputs --------------------------------------------------------------------------------------------------
def case_brief(case: dict) -> str:
    keys = ("id", "bucket", "language", "vehicle", "dtcs", "user_question", "info_completeness",
            "expected_behavior", "reference", "safety_critical", "critical_errors")
    return json.dumps({k: case.get(k) for k in keys}, ensure_ascii=False, indent=1, default=str)


def our_answer(o: dict) -> tuple[str, list[dict]] | None:
    if "message" not in o:
        return None
    text = o["message"] + "".join(f"\n[warning] {w}" for w in o.get("warnings") or [])
    return text, [{"type": s.get("type"), "title": s.get("title"), "url": s.get("url"), "applies": s.get("applies")}
                  for s in o.get("sources") or []]


def his_answer(h: dict) -> tuple[str, list[dict]] | None:
    if not (h.get("answer") or "").strip():
        return None
    return h["answer"], [{"title": s.get("title"), "url": s.get("url")} for s in h.get("sources") or []]


def answers_for(case_id: str, ours: dict, his: dict, modes: list[str]) -> dict[str, tuple[str, list[dict]]]:
    out = {}
    if (a := our_answer(ours.get(case_id) or {})) is not None:
        out[OURS] = a
    for m in modes:
        if (a := his_answer((his.get(case_id) or {}).get(m) or {})) is not None:
            out[m] = a
    return out


def _sources_text(sources: list[dict]) -> str:
    if not sources:
        return "(no sources listed)"
    return "\n".join(f"[{i}] {s.get('type') or ''} {s.get('title') or ''} {s.get('url') or ''}"
                     f"{' (marked: ' + s['applies'] + ')' if s.get('applies') else ''}".strip()
                     for i, s in enumerate(sources, 1))


# -- the judge -----------------------------------------------------------------------------------------------
def parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in the judge's reply")
    return json.loads(text[start:end + 1])


def quality(j: dict) -> float:
    """His rubric: critical error -> 0; wrong behaviour caps procedure at 2; sum(score x weight) / 5 x 100."""
    if j.get("critical_failure"):
        return 0.0
    s = {k: max(0, min(5, float((j.get("scores") or {}).get(k, 0)))) for k in WEIGHTS}
    if not j.get("behavior_correct", True):
        s["procedure_correctness"] = min(s["procedure_correctness"], 2)
    return round(sum(s[k] * w for k, w in WEIGHTS.items()) / 5 * 100, 1)


class Judge:
    def __init__(self, chat, model: str, rubric: str, cache: Path | None = CACHE):
        if "gemma" in model.lower() or model.lower().startswith("google/"):
            raise ValueError("the judge must not be from the answering model's family (Gemma)")
        self.chat, self.model, self.rubric, self.cache = chat, model, rubric, cache
        self.cost = 0.0
        self.memo: dict[str, dict] = {}
        if cache and cache.exists():
            for line in cache.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                self.memo[row["key"]] = row["result"]

    def _ask(self, key: str, system: str, user: str) -> dict:
        if key in self.memo:
            return self.memo[key]
        last = None
        for _ in range(2):
            res = self.chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                            model=self.model, json_mode=True, temperature=0.0, max_tokens=1500)
            self.cost += res.cost_usd or 0.0
            try:
                result = parse_json(res.text)
                break
            except (ValueError, json.JSONDecodeError) as exc:
                last = exc
        else:
            raise RuntimeError(f"the judge did not return JSON twice: {last}")
        self.memo[key] = result
        if self.cache:
            self.cache.parent.mkdir(exist_ok=True)
            with self.cache.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"key": key, "result": result}, ensure_ascii=False) + "\n")
        return result

    def _key(self, *parts: str) -> str:
        return hashlib.sha256("\x1f".join((self.model, PROMPT_VERSION, *parts)).encode()).hexdigest()

    def score(self, case: dict, answer: str, sources: list[dict]) -> dict:
        system = ("You are a strict, expert automotive diagnostic evaluator. Score ONE assistant answer to a benchmark "
                  f"case with the rubric below. The answer may be in Arabic or English; write in English.\n{FAIR_RULES}\n\n"
                  f"RUBRIC\n{self.rubric}\n\n{SCORE_KEYS}")
        user = f"CASE\n{case_brief(case)}\n\nANSWER\n{answer}\n\nSOURCES THE ANSWER LISTS\n{_sources_text(sources)}"
        j = dict(self._ask(self._key("score", case["id"], answer, json.dumps(sources)), system, user))
        j["quality"] = quality(j)
        return j

    def pair(self, case: dict, a: tuple[str, list[dict]], b: tuple[str, list[dict]]) -> str:
        """'A', 'B' or 'tie', asked in both orders; a side wins only if it wins both times."""
        system = PAIR_SYSTEM

        def ask(first, second) -> str:
            user = (f"CASE\n{case_brief(case)}\n\nANSWER A\n{first[0]}\nSOURCES A\n{_sources_text(first[1])}\n\n"
                    f"ANSWER B\n{second[0]}\nSOURCES B\n{_sources_text(second[1])}")
            got = self._ask(self._key("pair", case["id"], first[0], second[0]), system, user)
            return str(got.get("better", "tie")).strip().upper()[:1] or "T"

        forward, backward = ask(a, b), ask(b, a)
        if forward == "A" and backward == "B":
            return "A"
        if forward == "B" and backward == "A":
            return "B"
        return "tie"


# -- statistics ----------------------------------------------------------------------------------------------
def bootstrap_mean(values: list[float], n: int = 2000, seed: int = 7) -> tuple[float, float, float]:
    if not values:
        return (float("nan"),) * 3
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choices(values, k=len(values))) for _ in range(n))
    return round(statistics.fmean(values), 1), round(means[int(0.025 * n)], 1), round(means[int(0.975 * n) - 1], 1)


def summarise(cases: list[dict], scores: dict[str, dict[str, dict]], pairs: dict[str, dict[str, str]],
              systems: list[str]) -> dict:
    common = [c["id"] for c in cases if all(c["id"] in scores.get(s, {}) for s in systems)]
    out = {"common_cases": len(common), "systems": {}, "paired": {}, "pairs": {}, "by_bucket": {}}
    for s in systems:
        rows = [scores[s][cid] for cid in common]
        out["systems"][s] = {
            "quality": bootstrap_mean([r["quality"] for r in rows]),
            "critical_failures": sum(bool(r.get("critical_failure")) for r in rows),
            "behavior_ok": sum(bool(r.get("behavior_correct")) for r in rows),
            "fabricated_specs": sum(int(r.get("fabricated_specs") or 0) for r in rows),
            "must_not_claim_violations": sum(len(r.get("must_not_claim_violations") or []) for r in rows),
            "must_mention_missing": sum(len(r.get("must_mention_missing") or []) for r in rows),
        }
    for s in systems:
        if s == OURS:
            continue
        diffs = [scores[OURS][cid]["quality"] - scores[s][cid]["quality"] for cid in common]
        out["paired"][s] = {"mean_diff": bootstrap_mean(diffs),
                            "wins": sum(d > 2 for d in diffs), "ties": sum(-2 <= d <= 2 for d in diffs),
                            "losses": sum(d < -2 for d in diffs)}
    for mode, results in pairs.items():
        vals = [results[cid] for cid in common if cid in results]
        out["pairs"][mode] = {"ours_better": vals.count("ours"), "his_better": vals.count("his"), "tie": vals.count("tie")}
    buckets = sorted({str(c.get("bucket")) for c in cases if c["id"] in common})
    for b in buckets:
        ids = [c["id"] for c in cases if c["id"] in common and str(c.get("bucket")) == b]
        out["by_bucket"][b] = {s: round(statistics.fmean(scores[s][i]["quality"] for i in ids), 1) for s in systems}
        out["by_bucket"][b]["n"] = len(ids)
    return out


def report(summary: dict, meta: dict, systems: list[str]) -> str:
    def name(s: str) -> str:
        return "Our Mechanic AI" if s == OURS else f"venorexai {s}"

    lines = [f"# Mechanic AI vs venorexai: scored comparison ({meta['run_at']})", "",
             (f"> **Preliminary.** Judge `{meta['judge']}` (another family than Gemma), his rubric, blind. His 100 cases "
              "and their references are AI drafts, not expert-verified; the expert's review is final. Our assistant "
              "answers one turn of a conversation."), "",
             (f"Cases: {meta.get('cases', 'venorexai')} ({meta.get('split', 'all')}) · "
              f"judged for every system: **{summary['common_cases']}** · our answers from `{meta['ours_file']}` "
              f"(web search {meta.get('external_source')}, public records {meta.get('public_records', 'off')}) · judge cost ${meta['judge_cost']:.3f}"), "",
             ("| System | Quality, mean [95% CI] | Critical failures | Behaviour OK | Fabricated specs | "
              "Must-not-claim broken | Must-mention missing |"), "|---|---|---|---|---|---|---|"]
    for s in systems:
        r = summary["systems"][s]
        m, lo, hi = r["quality"]
        lines.append(f"| {name(s)} | **{m}** [{lo}, {hi}] | {r['critical_failures']} | {r['behavior_ok']} | "
                     f"{r['fabricated_specs']} | {r['must_not_claim_violations']} | {r['must_mention_missing']} |")
    lines += ["", "## Paired: ours minus his, per case (±2 = tie)", "",
              "| Against | Mean difference [95% CI] | Ours better / tie / his better | Significant |", "|---|---|---|---|"]
    for s, r in summary["paired"].items():
        m, lo, hi = r["mean_diff"]
        sig = "yes" if lo > 0 or hi < 0 else "no"
        lines.append(f"| {name(s)} | {m} [{lo}, {hi}] | {r['wins']} / {r['ties']} / {r['losses']} | {sig} |")
    if summary["pairs"]:
        lines += ["", "## Head to head (blind, both orders; a win counts only if it holds in both)", "",
                  "| Against | Ours better | Tie | His better |", "|---|---|---|---|"]
        for s, r in summary["pairs"].items():
            lines.append(f"| {name(s)} | {r['ours_better']} | {r['tie']} | {r['his_better']} |")
    lines += ["", "## By bucket (mean quality)", "", "| Bucket | n | " + " | ".join(name(s) for s in systems) + " |",
              "|---|---|" + "---|" * len(systems)]
    for b, r in summary["by_bucket"].items():
        lines.append(f"| {b} | {r['n']} | " + " | ".join(str(r[s]) for s in systems) + " |")
    return "\n".join(lines) + "\n"


# -- run -----------------------------------------------------------------------------------------------------
def run(cases: list[dict], ours: dict, his: dict, judge: Judge, modes: list[str], pair_modes: list[str],
        workers: int = 4) -> tuple[dict, dict]:
    systems = [OURS, *modes]
    scores: dict[str, dict[str, dict]] = {s: {} for s in systems}
    pairs: dict[str, dict[str, str]] = {m: {} for m in pair_modes}
    jobs = []
    for c in cases:
        ans = answers_for(c["id"], ours, his, modes)
        if OURS not in ans:
            continue  # not run on our side (no car in the case): left out for every system, so the sets match
        jobs += [("score", c, s, ans[s]) for s in systems if s in ans]
        jobs += [("pair", c, m, (ans[OURS], ans[m])) for m in pair_modes if m in ans]

    def do(job):
        kind, case, s, payload = job
        try:
            if kind == "score":
                return kind, case["id"], s, judge.score(case, *payload)
            got = judge.pair(case, *payload)
            return kind, case["id"], s, {"A": "ours", "B": "his"}.get(got, "tie")
        except Exception as exc:  # noqa: BLE001 - one failed judgement must not stop the run; it is reported
            return "error", case["id"], s, f"{type(exc).__name__}: {str(exc)[:200]}"

    errors = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, (kind, cid, s, result) in enumerate(pool.map(do, jobs), 1):
            if kind == "score":
                scores[s][cid] = result
            elif kind == "pair":
                pairs[s][cid] = result
            else:
                errors.append({"case": cid, "system": s, "error": result})
            if i % 20 == 0 or i == len(jobs):
                print(f"{i}/{len(jobs)} judgements  (cost so far ${judge.cost:.3f})", flush=True)
    return {"scores": scores, "pairs": pairs, "errors": errors}, summarise(cases, scores, pairs, systems)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--venorex", required=True, type=Path, help="the venorexai folder")
    p.add_argument("--ours", required=True, type=Path, help="our answers: eval/results/compare-venorex-<time>.json")
    p.add_argument("--judge", default=DEFAULT_JUDGE)
    p.add_argument("--his-modes", default=",".join(HIS_MODES))
    p.add_argument("--pair-modes", default="hybrid")
    p.add_argument("--split", choices=("all", "dev", "holdout"), default="all")
    p.add_argument("--cases", type=Path, help="another case set in his format, e.g. eval/own-cases; use with "
                   "--his-modes '' --pair-modes '' when he has no answers for it")
    p.add_argument("--limit", type=int)
    p.add_argument("--workers", type=int, default=4)
    args = p.parse_args(argv)

    rubric_path = args.venorex / "eval/rubric.md"
    if not rubric_path.exists():
        print(f"FAIL  his rubric is not at {rubric_path}")
        return 1
    modes = [m for m in args.his_modes.split(",") if m]
    pair_modes = [m for m in args.pair_modes.split(",") if m]
    if bad := [m for m in modes + pair_modes if m not in HIS_MODES]:
        print(f"FAIL  unknown modes {bad}; use {', '.join(HIS_MODES)}")
        return 1
    saved = json.loads(args.ours.read_text(encoding="utf-8"))
    cases = load_cases(args.venorex, args.split, cases_dir=args.cases)[: args.limit]
    his = load_his_answers(args.venorex)

    from app.llm.gateway import LLMGateway

    judge = Judge(LLMGateway().chat, args.judge, rubric_path.read_text(encoding="utf-8"))
    detail, summary = run(cases, saved["ours"], his, judge, modes, pair_modes, args.workers)
    stamp = f"{dt.datetime.now(dt.UTC):%Y%m%d-%H%M%S}"
    meta = {"run_at": stamp, "judge": args.judge, "ours_file": args.ours.name, "judge_cost": judge.cost,
            "cases": str(args.cases or "venorexai"), "split": args.split,
            "external_source": saved.get("meta", {}).get("external_source"),
            "public_records": saved.get("meta", {}).get("public_records", "off")}
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"judge-venorex-{stamp}.json").write_text(
        json.dumps({"meta": meta, "summary": summary, **detail}, ensure_ascii=False, indent=1), encoding="utf-8")
    md = RESULTS / f"judge-venorex-{stamp}.md"
    md.write_text(report(summary, meta, [OURS, *modes]), encoding="utf-8")
    print(md.read_text(encoding="utf-8"))
    if detail["errors"]:
        print(f"WARN  {len(detail['errors'])} judgements failed; they are listed in the JSON and left out")
    print(f"report: {md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Side by side: the team lead's 100 benchmark cases (venorexai) through our assistant, next to his answers.

    PYTHONPATH=. python -m eval.compare_venorex --venorex C:\\path\\to\\venorexai-main
    PYTHONPATH=. python -m eval.compare_venorex --venorex ... --split dev --limit 5      # a quick look first
    PYTHONPATH=. python -m eval.compare_venorex --venorex ... --render eval/results/compare-venorex-<time>.json

His side is read from his repository as it is: the cases (eval/cases/{dev,holdout}/*.yaml) and his stored
answers in all four modes (eval/reports/phase4-final/results.json). His app does not run. Our side is one
turn of our engine per case, with the real model, the knowledge base and EXTERNAL_SOURCE as set in .env
(off = general knowledge when there is no verified data; web = web search).

Nothing of his is copied into this repository: the page and the JSON go to eval/results/ (git-ignored).
Nothing is scored automatically: his cases' reference (summary, must mention, must not claim) is shown
beside both answers for a person to judge. His cases are AI drafts, not expert-verified.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sys
import time
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
RESULTS = REPO / "eval/results"
HIS_MODES = ("model_only", "web_only", "rag_only", "hybrid")
POWERTRAIN = {"ice": "ICE", "hybrid": "Hybrid", "phev": "PHEV", "bev": "EV"}
_ENGINE_CODE = re.compile(r"\b(?=[A-Z0-9-]*\d)(?=[A-Z0-9-]*[A-Z])[A-Z0-9]{2,6}(?:-[A-Z0-9]{2,4})?\b")
_NOT_ENGINE = re.compile(r"^(?:[VIWH]\d{1,2}|\d+(?:\.\d+)?L|\d+V)$")


# -- his side ------------------------------------------------------------------------------------------------
def load_cases(root: Path, split: str = "all", only: str | None = None) -> list[dict]:
    splits = ("dev", "holdout") if split == "all" else (split,)
    cases = []
    for s in splits:
        for path in sorted((root / "eval/cases" / s).glob("*.yaml")):
            case = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not only or only.lower() in case["id"].lower():
                cases.append(case)
    return cases


def load_his_answers(root: Path) -> dict[str, dict]:
    """case id -> {mode: {answer, ms, cost, sources}} from his final benchmark run (repeat 0)."""
    path = root / "eval/reports/phase4-final/results.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, dict] = {}
    for row in data.get("rows", []):
        if row.get("repeat_index") != 0:
            continue
        evidence = ((data.get("retrieval") or {}).get(row["case_id"]) or {}).get(row["mode"]) or {}
        out.setdefault(row["case_id"], {})[row["mode"]] = {
            "answer": row.get("answer") or "", "error": row.get("error"), "ms": row.get("total_ms"), "cost": row.get("cost_usd"),
            "sources": [{"title": e.get("title"), "url": e.get("url"), "domain": e.get("domain"),
                         "blocked": _blocked(e.get("url"))} for e in evidence.get("evidence", [])],
        }
    return out


def _blocked(url: str | None) -> bool:
    """A source from a site our assistant never uses (WEB_BLOCKED_DOMAINS): shown, and marked, on his side."""
    from app.config import get_settings
    from app.web.applicability import is_blocked

    host = re.sub(r"^https?://", "", url or "").split("/")[0]
    return bool(host) and is_blocked(host, get_settings().web_blocked)


# -- our side ------------------------------------------------------------------------------------------------
def engine_code(text: str | None) -> str | None:
    """'4.6L 1UR-FE V8' -> '1UR-FE'; '2.5L' -> None."""
    for token in _ENGINE_CODE.findall((text or "").upper()):
        if not _NOT_ENGINE.match(token):
            return token
    return None


def vehicle_of(case: dict) -> dict | None:
    v = case.get("vehicle") or {}
    if not v.get("make") or not v.get("model"):
        return None  # our API needs the car when a session opens (the app collects it): not run, not invented
    return {"make": v["make"], "model": v["model"], "year": v.get("year"), "engine_code": engine_code(v.get("engine")),
            "powertrain": POWERTRAIN.get(str(v.get("powertrain") or "").lower())}


def message_of(case: dict) -> str:
    text = case["user_question"].strip()
    missing = [d for d in case.get("dtcs") or [] if d.upper() not in text.upper()]
    return text + (f"\nCodes: {', '.join(missing)}" if missing else "")


def run_case(engine_factory, case: dict) -> dict:
    from app.engine.state import SessionState

    vehicle = vehicle_of(case)
    if vehicle is None:
        return {"not_run": "no make or model in the case: our API needs the car when a session opens"}
    engine = engine_factory()
    start = time.perf_counter()
    try:
        reply = engine.turn(SessionState(vehicle=vehicle), message_of(case)).reply
    except Exception as exc:  # noqa: BLE001 - one case failing (a provider error, a timeout) must not stop the whole run
        return {"error": f"{type(exc).__name__}: {str(exc)[:300]}", "seconds": round(time.perf_counter() - start, 1)}
    trace = engine.model.trace
    return {"seconds": round(time.perf_counter() - start, 1), "tier": reply.tier, "type": reply.type, "language": reply.language,
            "message": reply.message, "warnings": reply.warnings,
            "sources": [{"type": e.type, "title": e.title, "url": e.url, "applies": e.applies} for e in reply.evidence],
            "calls": len(trace), "web_searches": sum(t.purpose == "web_search" for t in trace),
            "public_records": sum(e.type == "public" for e in reply.evidence),
            "cost": round(sum(t.cost_usd or 0 for t in trace), 5),
            "refused": sum(t.rejected is not None for t in trace),
            "rejected": [f"{t.purpose}: {t.rejected}" for t in trace if t.rejected is not None][:4]}


# -- the page ------------------------------------------------------------------------------------------------
def _e(text) -> str:
    return html.escape(str(text if text is not None else ""))


def _sources(sources: list[dict]) -> str:
    items = []
    for s in sources:
        label = _e(s.get("title") or s.get("domain") or s.get("url"))
        mark = f" <i>({_e(s['applies'])})</i>" if s.get("applies") else ""
        if s.get("blocked"):
            mark += " <b class=warn>(site with no shown right to the manuals: blocked in our assistant)</b>"
        url = s.get("url") or ""
        link = f'<a href="{_e(url)}" target="_blank" rel="noopener noreferrer">{label}</a>' if re.match(r"https?://", url) else label
        items.append(f"<li>{link}{mark}</li>")
    return f"<ul class=src>{''.join(items)}</ul>" if items else ""


def render(cases: list[dict], ours: dict[str, dict], his: dict[str, dict], meta: dict) -> str:
    run = [ours[c["id"]] for c in cases if c["id"] in ours and "tier" in ours[c["id"]]]
    tiers: dict[str, int] = {}
    for o in run:
        tiers[o["tier"]] = tiers.get(o["tier"], 0) + 1
    not_run = sum("not_run" in ours.get(c["id"], {}) for c in cases)
    failed = sum("error" in ours.get(c["id"], {}) for c in cases)
    secs = sorted(o["seconds"] for o in run)
    median = secs[len(secs) // 2] if secs else 0
    his_ms = {m: sorted(h[m]["ms"] for h in his.values() if m in h and h[m]["ms"]) for m in HIS_MODES}
    summary = (f"<p><b>{len(cases)}</b> cases · ours answered <b>{len(run)}</b> · not run {not_run} · failed {failed} · "
               f"our level: {', '.join(f'{k} {v}' for k, v in sorted(tiers.items())) or '-'} · "
               f"our median time {median:.1f} s · our model cost ${sum(o['cost'] for o in run):.3f}</p>"
               f"<p>His median time: {', '.join(f'{m} {v[len(v) // 2] / 1000:.1f} s' for m, v in his_ms.items() if v)} · "
               f"his answers citing a site our assistant blocks: "
               f"{sum(any(x.get('blocked') for x in h[m]['sources']) for h in his.values() for m in h)}</p>")
    blocks = []
    for c in cases:
        o, h = ours.get(c["id"], {}), his.get(c["id"], {})
        v = c.get("vehicle") or {}
        ref = c.get("reference") or {}
        lists = "".join(f"<p><b>{title}</b></p><ul>{''.join(f'<li>{_e(x)}</li>' for x in ref.get(key) or [])}</ul>"
                        for title, key in (("Must mention", "must_mention"), ("Must not claim", "must_not_claim")))
        if "tier" in o:
            ours_html = (f"<div class=badges><span class='b {o['tier']}'>{_e(o['tier'])}</span><span class=b>{_e(o['type'])}</span>"
                         f"<span class=b>{o['seconds']} s</span><span class=b>{o['calls']} calls · ${o['cost']}</span>"
                         + (f"<span class=b>web searches {o['web_searches']}</span>" if o["web_searches"] else "")
                         + (f"<span class='b warn'>{o['refused']} refused by the checker</span>" if o["refused"] else "")
                         + f"</div><div class=msg dir=auto>{_e(o['message'])}</div>"
                         + "".join(f"<p class=warn dir=auto>{_e(w)}</p>" for w in o["warnings"]) + _sources(o["sources"]))
        else:
            ours_html = f"<p class=warn>{_e(o.get('not_run') or o.get('error') or 'not run')}</p>"
        his_html = "".join(
            f"<details{' open' if m == 'hybrid' else ''}><summary>{m} · {(h[m]['ms'] or 0) / 1000:.1f} s"
            f"{' · <b class=warn>uses a blocked site</b>' if any(x.get('blocked') for x in h[m]['sources']) else ''}</summary>"
            f"<div class=msg dir=auto>{_e(h[m]['answer'] or h[m]['error'])}</div>{_sources(h[m]['sources'])}</details>"
            for m in HIS_MODES if m in h) or "<p class=warn>no stored answer</p>"
        blocks.append(
            f"<section data-bucket='{_e(c.get('bucket'))}' data-tier='{_e(o.get('tier', 'not run'))}' data-lang='{_e(c.get('language'))}'>"
            f"<h2>{_e(c['id'])} · {_e(c.get('bucket'))} · {_e(c.get('language'))} · expected: {_e(c.get('expected_behavior'))}"
            f"{' · safety-critical' if c.get('safety_critical') else ''}</h2>"
            f"<p class=car>{_e(' '.join(str(x) for x in (v.get('year'), v.get('make'), v.get('model'), v.get('engine')) if x))}"
            f" · {_e(v.get('powertrain') or '')} · {_e(v.get('market') or '')} · codes: {_e(', '.join(c.get('dtcs') or []) or 'none')}</p>"
            f"<div class=q dir=auto>{_e(c['user_question'])}</div>"
            f"<details><summary>Reference (AI draft, not expert-verified)</summary><p dir=auto>{_e(ref.get('summary'))}</p>{lists}</details>"
            f"<div class=cols><div><h3>Our Mechanic AI</h3>{ours_html}</div><div><h3>venorexai (his stored answers)</h3>{his_html}</div></div>"
            "</section>")
    def options(attr: str) -> str:
        return "".join(f"<option>{_e(x)}</option>" for x in sorted({str(c.get(attr)) for c in cases}))

    tier_options = "".join(f"<option>{_e(t)}</option>" for t in sorted({ours.get(c['id'], {}).get('tier', 'not run') for c in cases}))
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Mechanic AI vs venorexai</title>
<style>
body{{font-family:system-ui,sans-serif;margin:20px;max-width:1500px;color:#222}} h1{{font-size:20px}} h2{{font-size:15px;margin:0 0 4px}}
h3{{font-size:13px;margin:6px 0;color:#555}} section{{border:1px solid #ddd;border-radius:8px;padding:12px;margin:12px 0}}
.cols{{display:grid;grid-template-columns:1fr 1fr;gap:16px}} .msg,.q{{white-space:pre-wrap;unicode-bidi:plaintext;font-size:14px;line-height:1.5}}
.q{{background:#f5f5f5;padding:8px;border-radius:6px}} .car{{color:#666;font-size:13px;margin:0 0 6px}}
.badges{{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px}} .b{{font-size:12px;padding:1px 8px;border-radius:12px;background:#eee}}
.b.verified{{background:#d6f5dd}} .b.web{{background:#dbe8ff}} .b.public{{background:#e8dcff}} .b.general{{background:#fff1cc}} .b.none{{background:#f3d6d6}}
.warn{{color:#a15c00}} .src{{font-size:12px;color:#555}} details{{margin:4px 0}} summary{{cursor:pointer;color:#444}}
.filters{{position:sticky;top:0;background:#fff;padding:8px 0;border-bottom:1px solid #eee}}
</style></head><body>
<h1>Mechanic AI vs venorexai: the same 100 cases</h1>
<p>Run {_e(meta.get('run_at'))} · our model roles {_e(meta.get('roles'))} · web search {_e(meta.get('external_source'))} ·
public records {_e(meta.get('public_records', 'off'))} ·
records served: {_e(meta.get('records'))}. His answers: stored run phase4-final (repeat 0). The cases and references are
his team's AI drafts, not expert-verified: judge both answers, do not treat either as the truth.</p>
{summary}
<div class=filters>Bucket <select id=fb><option value="">all</option>{options('bucket')}</select>
Our level <select id=ft><option value="">all</option>{tier_options}</select>
Language <select id=fl><option value="">all</option>{options('language')}</select></div>
{''.join(blocks)}
<script>
for (const id of ["fb","ft","fl"]) document.getElementById(id).onchange = () => {{
  const b = fb.value, t = ft.value, l = fl.value;
  for (const s of document.querySelectorAll("section"))
    s.style.display = (!b || s.dataset.bucket === b) && (!t || s.dataset.tier === t) && (!l || s.dataset.lang === l) ? "" : "none";
}};
</script></body></html>"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--venorex", required=True, type=Path, help="the venorexai folder (with eval/cases and eval/reports)")
    parser.add_argument("--split", choices=("all", "dev", "holdout"), default="all")
    parser.add_argument("--only", help="cases whose id contains this text")
    parser.add_argument("--limit", type=int, help="run only the first N cases")
    parser.add_argument("--approved-only", action="store_true", help="serve approved records only, as in production")
    parser.add_argument("--workers", type=int, default=1, help="cases run at the same time (each with its own database "
                        "connection); seconds per case stay comparable, the whole run is shorter")
    parser.add_argument("--render", type=Path, help="re-draw the page from a saved JSON, without running anything")
    args = parser.parse_args(argv)

    cases = load_cases(args.venorex, args.split, args.only)[: args.limit]
    if not cases:
        print(f"FAIL  no cases found under {args.venorex / 'eval/cases'}")
        return 1
    his = load_his_answers(args.venorex)
    stamp = f"{dt.datetime.now(dt.UTC):%Y%m%d-%H%M%S}"
    if args.render:
        saved = json.loads(args.render.read_text(encoding="utf-8"))
        ours, meta = saved["ours"], saved["meta"]
    else:
        from app.config import get_settings
        from app.db import connect
        from app.engine.engine import Engine, Roles
        from app.engine.external import from_settings
        from app.engine.model import GatewayModel
        from app.kb.store import APPROVED, APPROVED_AND_DRAFT, KnowledgeStore
        from app.llm.gateway import LLMGateway

        settings = get_settings()
        roles = Roles.from_settings(settings)
        external = from_settings(settings)
        statuses = APPROVED if args.approved_only else APPROVED_AND_DRAFT
        meta = {"run_at": stamp, "roles": str(roles), "external_source": settings.external_source,
                "public_records": settings.public_records,
                "records": "approved only" if args.approved_only else "approved and draft"}
        ours = {}
        gateway = LLMGateway()
        from concurrent.futures import ThreadPoolExecutor

        from app.public.source import from_settings as public_records

        def one(case: dict) -> dict:
            with connect() as conn:  # one connection per case: workers never share a transaction
                kb = KnowledgeStore(conn)
                public = public_records(settings, conn, gateway)

                def factory():
                    return Engine(kb, GatewayModel(gateway), statuses=statuses, external=external,
                                  emergency_number=settings.emergency_number, roles=roles, public=public)

                try:
                    return run_case(factory, case)
                finally:
                    conn.rollback()  # nothing is written

        meta["workers"] = max(1, args.workers)
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            for i, (case, o) in enumerate(zip(cases, pool.map(one, cases), strict=True), 1):
                ours[case["id"]] = o
                print(f"{i:>3}/{len(cases)} {case['id']:<8} {o.get('tier') or ('NOT RUN' if 'not_run' in o else 'ERROR'):<8} "
                      f"{o.get('seconds', 0):5.1f}s  {o.get('error', '')}", flush=True)
    RESULTS.mkdir(exist_ok=True)
    if not args.render:
        saved_path = RESULTS / f"compare-venorex-{stamp}.json"
        saved_path.write_text(json.dumps({"meta": meta, "ours": ours}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"our answers: {saved_path.relative_to(REPO)}")
    page = RESULTS / f"compare-venorex-{stamp}.html"
    page.write_text(render(cases, ours, his, meta), encoding="utf-8")
    print(f"page: {page.relative_to(REPO)}   (open it in your browser)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

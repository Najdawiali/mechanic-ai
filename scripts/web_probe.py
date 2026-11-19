"""Measure what web search really returns for our own cars and fault codes, before building on it.

    python scripts/web_probe.py

Keys come from .env (TAVILY_API_KEY, EXA_API_KEY); never paste a key into chat. A provider without a
key is skipped. For each test case three searches run in parallel, each with its own time limit:

    tavily        the open web
    tavily-official   the same query limited to official sites (NHTSA's copies of manufacturer
                      bulletins, manufacturer service sites)
    exa           Exa's search by meaning

For every result the script marks, in code, whether the site is official, and whether the text names
the exact code, the model and the year. A bulletin for other years only (for example 2016-2019 when the
car is 2020) is marked, because a wrong-generation document is the main danger. These marks are a
first filter, not a verdict: the report is read by a person before any decision.

Cost: one run is about 16 Tavily credits (free plan: 1,000 a month) and 8 Exa searches.
The full results are written to eval/results/web-probe-<time>.json (git-ignored).
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # this repo's app, whatever is installed
from app.config import get_settings

TIMEOUT_S = 15.0
MAX_RESULTS = 5

# Official = the manufacturer's own sites, or NHTSA, which publishes the manufacturers' bulletins as filed.
OFFICIAL = (
    "nhtsa.gov", "acura.com", "honda.com", "toyota.com", "lexus.com", "hyundai.com", "hyundaiusa.com",
    "hmaservice.com", "volkswagen.com", "vw.com", "volkswagen.de", "byd.com", "exeed.com", "exeedcars.com",
    "chery.com", "tesla.com", "ford.com", "motorcraftservice.com",
)
OFFICIAL_SEARCH = ["static.nhtsa.gov", "nhtsa.gov", "service.tesla.com", "techinfo.honda.com", "techinfo.toyota.com"]
# Sites that republish commercial service manuals without showing any right to them. Never a source:
# excluded from the searches, and marked if one still comes back. LEMON ("Liberated Excellent Manuals
# ONline") and CHARM give no source for their manuals; 17vin does not explain its rights to them.
BLOCKED = ("lemon-manuals.la", "lemon-manuals.org.ua", "charm.li", "17vin.com")


@dataclass(frozen=True)
class Case:
    name: str
    make: str
    model: str
    year: int | None
    code: str
    model_words: tuple[str, ...]  # words that name this model in a page

    def query(self) -> str:
        return " ".join(str(p) for p in (self.year, self.make, self.model, self.code, "diagnosis") if p)


# From eval/scenarios: the makes, cars and codes the client really sees.
CASES = [
    Case("acura-p0010", "Acura", "Integra", 2025, "P0010", ("integra",)),  # we hold the factory procedure: the reference
    Case("acura-p0341", "Acura", "Integra", 2025, "P0341", ("integra",)),
    Case("toyota-p0420", "Toyota", "Camry", 2018, "P0420", ("camry",)),
    Case("sonata-p1990", "Hyundai", "Sonata Hybrid", 2020, "P1990", ("sonata",)),
    Case("vw-p0c44", "Volkswagen", "ID.4", 2021, "P0C4400", ("id.4", "id4")),
    Case("byd-p2b53", "BYD", "Qin Plus DM-i", 2024, "P2B5313", ("qin",)),
    Case("exeed-p1a73", "EXEED", "hybrid", None, "P1A7309", ("exeed",)),
    Case("tesla-pcs-a027", "Tesla", "Model Y", 2022, "PCS_a027", ("model y",)),
]


@dataclass
class Result:
    url: str
    title: str
    domain: str
    official: bool
    blocked: bool
    names_code: bool
    names_model: bool
    year: str  # "match" | "other years only" | "not stated"
    excerpt: str


@dataclass
class ProviderRun:
    provider: str
    ok: bool
    seconds: float
    error: str = ""
    cost: str = ""
    results: list[Result] = field(default_factory=list)


_RANGE = re.compile(r"\b((?:19|20)\d\d)\s*(?:[-–~]|to)\s*((?:19|20)?\d\d)?")
_YEAR = re.compile(r"\b(?:19|20)\d\d\b")
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
# Dates are when a page was written, not which cars it is for: "January 16, 2020", "16 Jan 2020",
# "2020-01-16", "1/16/2020", "© 2020".
_DATE = re.compile(
    rf"\b{_MONTH}\s+(?:\d{{1,2}}(?:st|nd|rd|th)?,?\s+)?(?:19|20)\d\d\b"
    rf"|\b\d{{1,2}}\s+{_MONTH}\s+(?:19|20)\d\d\b"
    r"|\b(?:19|20)\d\d-\d\d-\d\d\b|\b\d{1,2}/\d{1,2}/(?:19|20)\d\d\b|(?:©|\(c\)|copyright)\s*(?:19|20)\d\d",
    re.IGNORECASE,
)


def year_fit(text: str, year: int | None) -> str:
    """Does the text apply to this model year?

    "match": the year itself, or a closed range that holds it (2016-2019, 2023–25, 2016 to 2019).
    "open range": only an open range holds it ("2016~", "2016-"): it may be another generation.
    "other years only": years are named and none fits. "not stated": no year at all.
    """
    if year is None:
        return "not stated"
    text = _DATE.sub(" ", text)
    years = {int(y) for y in _YEAR.findall(text)}
    open_hit = False
    for start, end in _RANGE.findall(text):
        low = int(start)
        if not end:
            open_hit = open_hit or year >= low
            continue
        high = int(end) if len(end) == 4 else int(start[:2] + end)
        if low <= year <= high:
            return "match"
    if year in years:
        return "match"
    if open_hit:
        return "open range"
    return "other years only" if years else "not stated"


def judge(case: Case, url: str, title: str, text: str) -> Result:
    domain = (urlparse(url).hostname or "").removeprefix("www.")
    whole = f"{title} {text}"
    low = whole.lower()
    code = case.code.lower().replace("_", "")
    return Result(
        url=url,
        title=title[:150],
        domain=domain,
        official=any(domain == d or domain.endswith("." + d) for d in OFFICIAL),
        blocked=any(domain == d or domain.endswith("." + d) for d in BLOCKED),
        names_code=code[:5] in low.replace("_", "") or code in low.replace("_", ""),
        names_model=any(w in low for w in case.model_words),
        year=year_fit(whole, case.year),
        excerpt=" ".join(text.split())[:400],
    )


async def tavily(client: httpx.AsyncClient, key: str, case: Case, official_only: bool) -> ProviderRun:
    name = "tavily-official" if official_only else "tavily"
    body = {"query": case.query(), "search_depth": "basic", "max_results": MAX_RESULTS,
            "include_answer": False, "include_raw_content": False}
    if official_only:
        body["include_domains"] = OFFICIAL_SEARCH
    else:
        body["exclude_domains"] = list(BLOCKED)
    start = time.perf_counter()
    try:
        r = await client.post("https://api.tavily.com/search", json=body,
                              headers={"Authorization": f"Bearer {key}"}, timeout=TIMEOUT_S)
        r.raise_for_status()
        data = r.json()
        results = [judge(case, x.get("url", ""), x.get("title", ""), x.get("content", "")) for x in data.get("results", [])]
        return ProviderRun(name, True, time.perf_counter() - start, cost="1 credit", results=results)
    except (httpx.HTTPError, ValueError) as e:  # one provider failing (network, status, bad JSON) must not stop the others
        return ProviderRun(name, False, time.perf_counter() - start, error=f"{type(e).__name__}: {str(e)[:200]}")


async def exa(client: httpx.AsyncClient, key: str, case: Case) -> ProviderRun:
    body = {"query": case.query(), "type": "auto", "numResults": MAX_RESULTS, "contents": {"highlights": True},
            "excludeDomains": list(BLOCKED)}
    start = time.perf_counter()
    try:
        r = await client.post("https://api.exa.ai/search", json=body, headers={"x-api-key": key}, timeout=TIMEOUT_S)
        r.raise_for_status()
        data = r.json()
        results = [judge(case, x.get("url", ""), x.get("title") or "", " ".join(x.get("highlights") or []) or (x.get("text") or ""))
                   for x in data.get("results", [])]
        cost = data.get("costDollars", {}).get("total")
        return ProviderRun("exa", True, time.perf_counter() - start, cost=f"${cost}" if cost is not None else "", results=results)
    except (httpx.HTTPError, ValueError) as e:
        return ProviderRun("exa", False, time.perf_counter() - start, error=f"{type(e).__name__}: {str(e)[:200]}")


async def probe_case(client: httpx.AsyncClient, keys: dict[str, str], case: Case) -> list[ProviderRun]:
    jobs = []
    if "tavily" in keys:
        jobs += [tavily(client, keys["tavily"], case, False), tavily(client, keys["tavily"], case, True)]
    if "exa" in keys:
        jobs.append(exa(client, keys["exa"], case))
    return list(await asyncio.gather(*jobs))


def line(run: ProviderRun) -> str:
    if not run.ok:
        return f"  {run.provider:<16} FAILED in {run.seconds:4.1f}s  {run.error}"
    n = len(run.results)
    official = sum(r.official for r in run.results)
    exact = sum(r.names_code and r.names_model and r.year == "match" and not r.blocked for r in run.results)
    blocked = sum(r.blocked for r in run.results)
    other = sum(r.year == "other years only" for r in run.results)
    opn = sum(r.year == "open range" for r in run.results)
    return (f"  {run.provider:<16} {run.seconds:4.1f}s  results {n}  official {official}  "
            f"code+model+year {exact}  other years only {other}  open range {opn}  blocked {blocked}  {run.cost}")


async def main() -> int:
    s = get_settings()
    keys = {k: v.get_secret_value() for k, v in (("tavily", s.tavily_api_key), ("exa", s.exa_api_key)) if v}
    if not keys:
        print("FAIL  neither TAVILY_API_KEY nor EXA_API_KEY is set in .env")
        return 1
    print(f"providers: {', '.join(keys)}   cases: {len(CASES)}   time limit per search: {TIMEOUT_S:.0f}s\n")
    report = {"run_at": datetime.now(UTC).isoformat(), "providers": list(keys), "cases": []}
    async with httpx.AsyncClient() as client:
        for case in CASES:
            start = time.perf_counter()
            runs = await probe_case(client, keys, case)
            print(f"{case.name}   \"{case.query()}\"   all providers in {time.perf_counter() - start:4.1f}s")
            for run in runs:
                print(line(run))
            report["cases"].append({"case": asdict(case), "query": case.query(), "runs": [asdict(r) for r in runs]})

    print("\nTOTAL")
    for provider in ("tavily", "tavily-official", "exa"):
        runs = [r for c in report["cases"] for r in c["runs"] if r["provider"] == provider]
        if not runs:
            continue
        results = [x for r in runs for x in r["results"]]
        ok = [r for r in runs if r["ok"]]
        exact_cases = sum(any(x["names_code"] and x["names_model"] and x["year"] == "match" and not x["blocked"] for x in r["results"]) for r in runs)
        print(f"  {provider:<16} searches ok {len(ok)}/{len(runs)}  "
              f"avg {sum(r['seconds'] for r in ok) / max(len(ok), 1):4.1f}s  results {len(results)}  "
              f"official {sum(x['official'] for x in results)}  "
              f"cases with an exact (code+model+year) result {exact_cases}/{len(runs)}  "
              f"other-years-only results {sum(x['year'] == 'other years only' for x in results)}  "
              f"blocked {sum(x['blocked'] for x in results)}")

    out = ROOT / "eval" / "results" / f"web-probe-{datetime.now(UTC):%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nfull results: {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""Web search in the engine's external-source slot (design 2.4 to 2.10).

For each fault code (at most two per turn), in parallel: Exa, and Tavily only if Exa fails. Every
result is marked for this car (app/web/applicability.py); dropped results never reach the model.
The whole lookup has a time budget: a search still running after it is left out of this turn.
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from app.engine.external import ExternalItem, ExternalLookup
from app.engine.model import TraceEntry
from app.kb.schemas import Vehicle
from app.web import applicability, providers, query

if TYPE_CHECKING:
    from app.config import Settings

_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="web")
_ORDER = {"fits": 0, "check": 1}


@dataclass(frozen=True)
class WebConfig:
    exa_key: str
    tavily_key: str | None = None
    search_type: str = "auto"
    timeout_s: float = 6.0
    fallback_timeout_s: float = 5.0
    budget_s: float = 8.0
    max_results: int = 5
    max_items: int = 6
    blocked: tuple[str, ...] = ()
    searches_per_session: int = 6
    codes_per_lookup: int = 2
    symptom_search: bool = False


def item_id(url: str) -> str:
    return "web:" + hashlib.sha1(url.encode()).hexdigest()[:12]


class WebSource:
    name = "web"

    def __init__(self, config: WebConfig, http: httpx.Client | None = None) -> None:
        self.config = config
        self.http = http
        self.searches_per_session = config.searches_per_session
        self.codes_per_lookup = config.codes_per_lookup
        self.symptom_search = config.symptom_search

    @classmethod
    def from_settings(cls, settings: Settings) -> WebSource:
        if settings.exa_api_key is None:
            raise ValueError("EXTERNAL_SOURCE=web needs EXA_API_KEY in .env")
        return cls(WebConfig(
            exa_key=settings.exa_api_key.get_secret_value(),
            tavily_key=settings.tavily_api_key.get_secret_value() if settings.tavily_api_key else None,
            search_type=settings.web_search_type, timeout_s=settings.web_timeout_s,
            fallback_timeout_s=settings.web_fallback_timeout_s, budget_s=settings.web_budget_s,
            max_results=settings.web_max_results, blocked=settings.web_blocked,
            searches_per_session=settings.web_searches_per_session, symptom_search=settings.web_symptom_search))

    # -- one code -------------------------------------------------------------------------------------
    def _search(self, vehicle: Vehicle, code: str, q: str, highlight: str) -> tuple[list[providers.ProviderRun], list[ExternalItem], list[TraceEntry]]:
        http = self.http or providers.client()
        c = self.config
        excluded = tuple(d for d in c.blocked if not d.endswith(".*"))  # providers take plain domains only
        runs = [providers.exa(http, c.exa_key, q, highlight, max_results=c.max_results, excluded=excluded,
                              timeout=c.timeout_s, search_type=c.search_type)]
        if not runs[0].ok and c.tavily_key:
            runs.append(providers.tavily(http, c.tavily_key, q, max_results=c.max_results, excluded=excluded,
                                         timeout=c.fallback_timeout_s))
        items, calls = [], []
        for run in runs:
            marked = []
            for found in run.found:
                text = applicability.clean_excerpt(found.text)
                mark = applicability.judge(vehicle, code, found.url, found.title, text, c.blocked)
                marked.append({"url": found.url, "title": found.title[:150], "applies": mark.applies, "reasons": mark.reasons})
                if mark.applies != "drop":
                    domain = (httpx.URL(found.url).host or "").removeprefix("www.")
                    items.append(ExternalItem(id=item_id(found.url), provider=run.provider, title=found.title[:150] or domain,
                                              text=text, url=found.url, kind="web",
                                              extra={"applies": mark.applies, "reasons": mark.reasons, "code": code, "domain": domain}))
            # the trace keeps links, titles and marks: not the page text (design 2.9, 2.12)
            calls.append(TraceEntry("web_search", run.provider, run.seconds, cost_usd=run.cost_usd, request={"query": run.query},
                                    output=json.dumps(marked, ensure_ascii=False), error=run.error))
        return runs, items, calls

    # -- the lookup ------------------------------------------------------------------------------------
    def lookup(self, vehicle: Vehicle, codes: list[str], question: str) -> ExternalLookup:
        """question (the mechanic's words, in English) is used only with no fault code and symptom search on,
        cleaned by query.build_symptom; otherwise it never goes into a query (app/web/query.py)."""
        jobs = {}
        for code in codes[: self.codes_per_lookup]:
            built = query.build(vehicle, code)
            if built is not None:
                jobs[_POOL.submit(self._search, vehicle, code, *built)] = code
        if not codes and self.symptom_search and (built := query.build_symptom(vehicle, question)) is not None:
            jobs[_POOL.submit(self._search, vehicle, "", *built)] = "symptom"
        if not jobs:
            return ExternalLookup()
        done, late = wait(jobs, timeout=self.config.budget_s)
        out = ExternalLookup(searched=len(jobs))
        seen: set[str] = set()
        for future in done:
            _, items, calls = future.result()
            out.calls += calls
            for item in items:
                if item.url not in seen:
                    seen.add(item.url)
                    out.items.append(item)
        for future in late:
            out.calls.append(TraceEntry("web_search", "web", self.config.budget_s, error=f"{jobs[future]}: over the time budget, left out"))
        out.items.sort(key=lambda i: _ORDER[i.extra["applies"]])
        del out.items[self.config.max_items:]
        return out

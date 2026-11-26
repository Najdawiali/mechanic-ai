"""The search providers, called directly over HTTP (design 2.3): Exa first, Tavily only if Exa fails.

Each call has its own time limit and never raises: a failure comes back as a ProviderRun with
ok=False, so one provider failing never stops the turn. Request shapes were checked against the
providers' docs and in the probe (scripts/web_probe.py, scripts/web_verify.py).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import httpx

EXA_URL = "https://api.exa.ai/search"
TAVILY_URL = "https://api.tavily.com/search"

_client: httpx.Client | None = None
_lock = threading.Lock()


def client() -> httpx.Client:
    """One client for the process: httpx's Client is thread-safe and pools connections (design 2.3)."""
    global _client
    with _lock:
        if _client is None or _client.is_closed:
            _client = httpx.Client(headers={"User-Agent": "mechanic-ai/1.0"},
                                   limits=httpx.Limits(max_connections=20, max_keepalive_connections=10))
        return _client


@dataclass
class Found:
    url: str
    title: str
    text: str  # what the provider returned about the page: highlights (Exa) or content (Tavily)


@dataclass
class ProviderRun:
    provider: str
    query: str
    ok: bool
    seconds: float
    cost_usd: float | None = None
    error: str | None = None
    found: list[Found] = field(default_factory=list)


def _failed(provider: str, query: str, start: float, exc: Exception) -> ProviderRun:
    return ProviderRun(provider, query, False, time.perf_counter() - start, error=f"{type(exc).__name__}: {str(exc)[:200]}")


def exa(http: httpx.Client, key: str, query: str, highlight_query: str, *, max_results: int, excluded: tuple[str, ...],
        timeout: float, search_type: str = "auto") -> ProviderRun:
    body = {"query": query, "type": search_type, "numResults": max_results, "excludeDomains": list(excluded),
            "contents": {"highlights": {"query": highlight_query, "maxCharacters": 1500}}}
    start = time.perf_counter()
    try:
        r = http.post(EXA_URL, json=body, headers={"x-api-key": key}, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        found = [Found(str(x.get("url") or ""), str(x.get("title") or ""), " ".join(x.get("highlights") or []) or str(x.get("text") or ""))
                 for x in data.get("results", []) if x.get("url")]
        cost = (data.get("costDollars") or {}).get("total")
        return ProviderRun("exa", query, True, time.perf_counter() - start, float(cost) if cost is not None else None, found=found)
    except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
        return _failed("exa", query, start, exc)


def tavily(http: httpx.Client, key: str, query: str, *, max_results: int, excluded: tuple[str, ...], timeout: float) -> ProviderRun:
    body = {"query": query, "search_depth": "basic", "max_results": max_results, "include_answer": False,
            "include_raw_content": False, "exclude_domains": list(excluded)}
    start = time.perf_counter()
    try:
        r = http.post(TAVILY_URL, json=body, headers={"Authorization": f"Bearer {key}"}, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        found = [Found(str(x.get("url") or ""), str(x.get("title") or ""), str(x.get("content") or ""))
                 for x in data.get("results", []) if x.get("url")]
        return ProviderRun("tavily", query, True, time.perf_counter() - start, found=found)  # Tavily returns no price
    except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
        return _failed("tavily", query, start, exc)

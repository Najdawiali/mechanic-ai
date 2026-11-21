"""Check the facts the web search design rests on, before anything is built on them.

    python scripts/web_verify.py            all checks
    python scripts/web_verify.py nhtsa      one check: exa | tavily | nhtsa | pdf

The design (project doc web-search-design.md, section 3) lists what could not be verified from
documents alone. This script answers each item with real output:

    exa     type "auto" against "fast" on the 8 probe cases: time, overlap, highlights (16 searches, about $0.11)
    tavily  the fallback answers in the expected shape with excluded sites (1 credit)
    nhtsa   one 5-year chunk of each bulletin file family: what is inside, how makes and models are
            written, whether summaries name our codes (downloads about 41 MB into data/nhtsa/)
    pdf     which link opens a bulletin's PDF from its NHTSA id

Keys come from .env; a key is never printed. Each check prints PASS, FAIL or INFO lines and the
whole result is written to eval/results/web-verify-<time>.json (git-ignored).
"""

from __future__ import annotations

import collections
import csv
import importlib.util
import io
import json
import re
import sys
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import get_settings

_spec = importlib.util.spec_from_file_location("web_probe", ROOT / "scripts" / "web_probe.py")
probe = importlib.util.module_from_spec(_spec)
sys.modules["web_probe"] = probe
_spec.loader.exec_module(probe)

NHTSA_BASE = "https://static.nhtsa.gov/odi/ffdd/tsbs/"
NHTSA_FILES = ["MFR_COMMS_RECEIVED_2020-2024.zip", "TSBS_RECEIVED_2020-2024.zip"]
NHTSA_DIR = ROOT / "data" / "nhtsa"
# the order of fields in NHTSA's flat file, from its data dictionary (MfrComms.txt, May 2024)
FLAT_FIELDS = ["NHTSA ID Number", "Replacement Service Bulletin Number", "Date Added to File", "TSB/Document ID",
               "Mfr Communication Date", "Mfr Internal Campaign ID/Software Version", "Communication Type", "Make",
               "Model", "Model Year", "NHTSA Components", "Mfr Component System", "Mfr Component Subsystem", "Summary"]
OUR_MAKES = ["ACURA", "TOYOTA", "HYUNDAI", "VOLKSWAGEN", "TESLA", "FORD"]
OUR_CODES = {"P1990": "HYUNDAI", "P0420": "TOYOTA", "P0341": "ACURA", "P0010": "ACURA"}
# PDF links seen in the probe, kept as known-good controls for the pattern test
KNOWN_PDFS = {"10160095": "https://static.nhtsa.gov/odi/tsbs/2019/MC-10160095-9999.pdf",
              "10170893": "https://static.nhtsa.gov/odi/tsbs/2020/MC-10170893-0001.pdf"}

REPORT: dict = {"run_at": datetime.now(UTC).isoformat(), "checks": {}}


def say(status: str, text: str) -> None:
    print(f"  {status:<5} {text}")


# -- exa --------------------------------------------------------------------------------------------------
def exa_search(client: httpx.Client, key: str, case, search_type: str) -> dict:
    body = {
        "query": f"{case.query().removesuffix(' diagnosis')} meaning causes diagnosis",
        "type": search_type,
        "numResults": probe.MAX_RESULTS,
        "excludeDomains": list(probe.BLOCKED),
        "contents": {"highlights": {"query": f"{case.code} meaning causes test", "maxCharacters": 1500}},
    }
    start = time.perf_counter()
    r = client.post("https://api.exa.ai/search", json=body, headers={"x-api-key": key}, timeout=20)
    seconds = time.perf_counter() - start
    r.raise_for_status()
    data = r.json()
    results = data.get("results", [])
    code = case.code.lower().replace("_", "")
    return {
        "seconds": round(seconds, 2),
        "search_type_used": data.get("searchType") or data.get("resolvedSearchType"),
        "cost": (data.get("costDollars") or {}).get("total"),
        "urls": [x.get("url") for x in results],
        "highlight_chars": [len(" ".join(x.get("highlights") or [])) for x in results],
        "highlight_names_code": [code in " ".join(x.get("highlights") or []).lower().replace("_", "") for x in results],
        "blocked_returned": [x.get("url") for x in results if probe.judge(case, x.get("url", ""), "", "").blocked],
        "response_keys": sorted(data),
    }


def check_exa(client: httpx.Client, key: str | None) -> None:
    print("\n[exa] type auto vs fast, query-guided highlights")
    if not key:
        say("FAIL", "EXA_API_KEY is not set in .env")
        return
    out = {}
    for case in probe.CASES:
        row = {}
        for search_type in ("auto", "fast"):
            try:
                row[search_type] = exa_search(client, key, case, search_type)
            except (httpx.HTTPError, ValueError) as e:
                row[search_type] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
        a, f = row["auto"], row["fast"]
        if "error" in a or "error" in f:
            say("FAIL", f"{case.name}: auto {a.get('error', 'ok')} | fast {f.get('error', 'ok')}")
        else:
            overlap = len(set(a["urls"]) & set(f["urls"]))
            row["overlap"] = overlap
            say("INFO", f"{case.name:<15} auto {a['seconds']:4.1f}s  fast {f['seconds']:4.1f}s  same urls {overlap}/5  "
                        f"highlights with the code auto {sum(a['highlight_names_code'])}/5 fast {sum(f['highlight_names_code'])}/5  "
                        f"empty highlights auto {a['highlight_chars'].count(0)} fast {f['highlight_chars'].count(0)}  "
                        f"blocked returned {len(a['blocked_returned']) + len(f['blocked_returned'])}")
        out[case.name] = row
    ok = [r for r in out.values() if "overlap" in r]
    if ok:
        for t in ("auto", "fast"):
            secs = sorted(r[t]["seconds"] for r in ok)
            costs = [r[t]["cost"] for r in ok if r[t]["cost"] is not None]
            say("INFO", f"{t}: median {secs[len(secs) // 2]:.1f}s, slowest {secs[-1]:.1f}s, "
                        f"cost per search {', '.join(sorted({f'${c}' for c in costs})) or 'not returned'}, "
                        f"searchType echoed {sorted({str(r[t]['search_type_used']) for r in ok})}")
        say("PASS" if all(r[t]["highlight_chars"] and max(r[t]["highlight_chars"]) > 0 for r in ok for t in ("auto", "fast"))
            else "FAIL", "highlights come back non-empty with {query, maxCharacters}")
        say("PASS" if not any(r[t]["blocked_returned"] for r in ok for t in ("auto", "fast")) else "FAIL",
            "excludeDomains keeps blocked sites out")
        say("INFO", f"response fields: {ok[0]['auto']['response_keys']}")
    REPORT["checks"]["exa"] = out


# -- tavily -----------------------------------------------------------------------------------------------
def check_tavily(client: httpx.Client, key: str | None) -> None:
    print("\n[tavily] the fallback's response shape")
    if not key:
        say("FAIL", "TAVILY_API_KEY is not set in .env")
        return
    case = next(c for c in probe.CASES if c.name == "sonata-p1990")
    body = {"query": case.query(), "search_depth": "basic", "max_results": 5, "include_answer": False,
            "include_raw_content": False, "exclude_domains": list(probe.BLOCKED)}
    start = time.perf_counter()
    try:
        r = client.post("https://api.tavily.com/search", json=body, headers={"Authorization": f"Bearer {key}"}, timeout=20)
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        say("FAIL", f"{type(e).__name__}: {str(e)[:200]}")
        REPORT["checks"]["tavily"] = {"error": str(e)[:300]}
        return
    results = data.get("results", [])
    fields = sorted({k for x in results for k in x})
    say("PASS" if results and {"url", "title", "content"} <= set(fields) else "FAIL",
        f"{len(results)} results in {time.perf_counter() - start:.1f}s; result fields {fields}; top-level {sorted(data)}")
    REPORT["checks"]["tavily"] = {"fields": fields, "top_level": sorted(data), "urls": [x.get("url") for x in results]}


# -- nhtsa ------------------------------------------------------------------------------------------------
def download(client: httpx.Client, name: str) -> Path:
    NHTSA_DIR.mkdir(parents=True, exist_ok=True)
    path = NHTSA_DIR / name
    with client.stream("GET", NHTSA_BASE + name, timeout=120, follow_redirects=True) as r:
        r.raise_for_status()
        size = int(r.headers.get("content-length") or 0)
        if path.exists() and size and path.stat().st_size == size:
            say("INFO", f"{name}: already downloaded ({size / 1e6:.1f} MB)")
            return path
        with path.open("wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
    say("INFO", f"{name}: downloaded {path.stat().st_size / 1e6:.1f} MB")
    return path


def _encoding(sample: bytes) -> str:
    try:
        sample.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "cp1252"


def inspect_member(zf: zipfile.ZipFile, member: zipfile.ZipInfo) -> dict:
    with zf.open(member) as raw:
        head = raw.read(200_000)
    encoding = _encoding(head)
    text = head.decode(encoding, errors="replace")
    lines = text.splitlines()[:5]
    tabs, commas = lines[0].count("\t"), lines[0].count(",")
    delimiter = "\t" if tabs >= commas else ","
    first = next(csv.reader([lines[0]], delimiter=delimiter))
    has_header = any(re.search(r"[A-Za-z]{3}", c) for c in first) and not re.fullmatch(r"\d+", first[0].strip('"'))
    info = {"file": member.filename, "unzipped_mb": round(member.file_size / 1e6, 1), "encoding_guess": encoding,
            "delimiter": "TAB" if delimiter == "\t" else "COMMA", "first_row": [c[:60] for c in first],
            "looks_like_header": has_header, "second_row": [c[:60] for c in next(csv.reader([lines[1]], delimiter=delimiter))]}
    return info


def rows(zf: zipfile.ZipFile, member: zipfile.ZipInfo, delimiter: str, encoding: str):
    csv.field_size_limit(1 << 24)
    with zf.open(member) as raw:
        stream = io.TextIOWrapper(raw, encoding=encoding, errors="replace", newline="")
        reader = csv.reader(stream, delimiter=delimiter, quoting=csv.QUOTE_NONE if delimiter == "\t" else csv.QUOTE_MINIMAL)
        yield from reader


def columns(info: dict) -> dict[str, int] | None:
    """Where make, model, year, ids and summary are: from the header, or from the flat file's documented order."""
    if info["looks_like_header"]:
        names = [c.strip().strip('"').lower() for c in info["first_row"]]

        def find(*words):
            return next((i for i, n in enumerate(names) if all(w in n for w in words)), None)

        found = {"make": find("make"), "model": next((i for i, n in enumerate(names) if n.startswith("model") and "year" not in n), None),
                 "year": find("year"), "nhtsa_id": find("nhtsa", "id"), "doc_id": find("document") or find("tsb"),
                 "summary": find("summary"), "date": find("date")}
        return found if found["make"] is not None and found["model"] is not None else None
    if len(info["first_row"]) == len(FLAT_FIELDS):
        return {"make": 7, "model": 8, "year": 9, "nhtsa_id": 0, "doc_id": 3, "summary": 13, "date": 2}
    return None


def check_nhtsa(client: httpx.Client) -> list[dict]:
    print("\n[nhtsa] bulletin files: contents, makes and models, our codes")
    samples: list[dict] = []
    out = {}
    for name in NHTSA_FILES:
        try:
            path = download(client, name)
        except httpx.HTTPError as e:
            say("FAIL", f"{name}: {type(e).__name__}: {str(e)[:200]}")
            out[name] = {"error": str(e)[:300]}
            continue
        with zipfile.ZipFile(path) as zf:
            members = [m for m in zf.infolist() if not m.is_dir()]
            say("INFO", f"{name}: contains {[(m.filename, round(m.file_size / 1e6, 1)) for m in members]}")
            for member in members:
                info = inspect_member(zf, member)
                say("INFO", f"{member.filename}: {info['delimiter']}, {info['encoding_guess']}, "
                            f"header {'yes' if info['looks_like_header'] else 'no'}, {len(info['first_row'])} columns")
                say("INFO", f"  first row: {info['first_row']}")
                cols = columns(info)
                info["columns"] = cols
                if cols is None:
                    say("FAIL", f"  could not place make/model/year in {member.filename}: read the first row above")
                    out[f"{name}/{member.filename}"] = info
                    continue
                widths = collections.Counter()
                total = 0
                makes = collections.Counter()
                models: dict[str, collections.Counter] = {m: collections.Counter() for m in OUR_MAKES}
                hits: dict[str, list] = {c: [] for c in OUR_CODES}
                delim = "\t" if info["delimiter"] == "TAB" else ","
                for i, row in enumerate(rows(zf, member, delim, info["encoding_guess"])):
                    if i == 0 and info["looks_like_header"]:
                        continue
                    total += 1
                    widths[len(row)] += 1
                    if len(row) <= max(v for v in cols.values() if v is not None):
                        continue
                    make = row[cols["make"]].strip().upper()
                    makes[make] += 1
                    if make in models:
                        models[make][row[cols["model"]].strip()] += 1
                    summary = row[cols["summary"]] if cols.get("summary") is not None else ""
                    for code, code_make in OUR_CODES.items():
                        if make == code_make and code in summary.upper() and len(hits[code]) < 5:
                            hits[code].append({"doc_id": row[cols["doc_id"]] if cols.get("doc_id") is not None else None,
                                               "nhtsa_id": row[cols["nhtsa_id"]] if cols.get("nhtsa_id") is not None else None,
                                               "model": row[cols["model"]], "year": row[cols["year"]] if cols.get("year") is not None else None,
                                               "summary": " ".join(summary.split())[:160]})
                    if make in models and len(samples) < 6 and cols.get("nhtsa_id") is not None and cols.get("date") is not None:
                        samples.append({"nhtsa_id": row[cols["nhtsa_id"]].strip(), "date": row[cols["date"]].strip(), "make": make})
                say("PASS" if total else "FAIL", f"  {total:,} rows; columns per row {dict(widths.most_common(4))}")
                for make in OUR_MAKES:
                    top = ", ".join(f"{m} ({n})" for m, n in models[make].most_common(8))
                    say("INFO", f"  {make:<11} {makes[make]:>7,} rows  models: {top}")
                for code, found in hits.items():
                    say("INFO", f"  {code} ({OUR_CODES[code]}): {len(found)} bulletin rows name it"
                                + (f", e.g. {found[0]['doc_id']} {found[0]['model']} {found[0]['year']}: {found[0]['summary']}" if found else ""))
                info.update({"rows": total, "row_widths": dict(widths), "our_makes": {m: dict(models[m].most_common(30)) for m in OUR_MAKES},
                             "code_hits": hits})
                out[f"{name}/{member.filename}"] = info
    REPORT["checks"]["nhtsa"] = out
    return samples


# -- pdf links --------------------------------------------------------------------------------------------
def opens(client: httpx.Client, url: str) -> bool:
    try:
        r = client.head(url, timeout=15, follow_redirects=True)
        if r.status_code == 405:
            r = client.get(url, timeout=15, headers={"Range": "bytes=0-1023"}, follow_redirects=True)
        return r.status_code in (200, 206) and "pdf" in r.headers.get("content-type", "").lower()
    except httpx.HTTPError:
        return False


def check_pdf(client: httpx.Client, samples: list[dict]) -> None:
    print("\n[pdf] which link opens a bulletin from its NHTSA id")
    out = {}
    for nhtsa_id, url in KNOWN_PDFS.items():
        ok = opens(client, url)
        say("PASS" if ok else "FAIL", f"control {nhtsa_id}: {url} {'opens' if ok else 'does not open'}")
        out[f"control-{nhtsa_id}"] = {"url": url, "opens": ok}
    for s in samples:
        years = []
        m = re.match(r"(\d{4})", s["date"])
        if m:
            y = int(m.group(1))
            years = [y, y - 1, y + 1]
        tried, found = [], None
        for y in years:
            for suffix in ("0001", "9999"):
                url = f"https://static.nhtsa.gov/odi/tsbs/{y}/MC-{s['nhtsa_id']}-{suffix}.pdf"
                tried.append(url)
                if opens(client, url):
                    found = url
                    break
            if found:
                break
        say("PASS" if found else "FAIL", f"{s['make']} id {s['nhtsa_id']} (date {s['date']}): {found or 'none of ' + str(len(tried)) + ' tried'}")
        out[s["nhtsa_id"]] = {"date": s["date"], "found": found, "tried": tried}
    REPORT["checks"]["pdf"] = out


def main(argv: list[str]) -> int:
    only = set(argv) or {"exa", "tavily", "nhtsa", "pdf"}
    s = get_settings()
    exa_key = s.exa_api_key.get_secret_value() if s.exa_api_key else None
    tavily_key = s.tavily_api_key.get_secret_value() if s.tavily_api_key else None
    samples: list[dict] = []
    with httpx.Client(headers={"User-Agent": "mechanic-ai-verify/1.0"}) as client:
        if "exa" in only:
            check_exa(client, exa_key)
        if "tavily" in only:
            check_tavily(client, tavily_key)
        if "nhtsa" in only or "pdf" in only:
            samples = check_nhtsa(client)
        if "pdf" in only:
            check_pdf(client, samples)
    print("\n[terms] Exa's terms could not be read from the build workspace: open "
          "https://exa.ai/assets/Exa_Labs_Terms_of_Service.pdf and note what it says on showing results to "
          "end users, storing or caching them, and attribution.")
    out = ROOT / "eval" / "results" / f"web-verify-{datetime.now(UTC):%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(REPORT, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nfull results: {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

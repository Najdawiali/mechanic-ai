"""Run the case extractor on real case reports with the real model, and score it.

  python scripts/score_cases.py "C:/path/to/reports_01" [--limit 3] [--only Tesla]

English files only (names ending _AR are skipped; a .txt is preferred to a .pdf of the same case).
For each report it prints the car, the fault codes and the outcome the model wrote, and every
check that failed. Where one of the evaluation scenarios was written from the same report, the
make and the fault codes are compared with it. This calls DeepInfra: about a minute per report.
"""

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.ingest.case_extractor import NOT_ENGLISH, extract_case  # noqa: E402
from app.ingest.extractors import SourceDocument  # noqa: E402
from app.kb.keys import code_stem, norm_key  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--only", help="only files whose name contains this text")
    args = parser.parse_args()
    if not args.folder.exists():
        print(f"No folder or file at {str(args.folder)!r}.")
        return 2
    files = [args.folder] if args.folder.is_file() else sorted(args.folder.rglob("*"))
    files = [f for f in files if f.suffix.lower() in (".txt", ".pdf") and "_AR" not in f.stem.upper()
             and (not args.only or args.only.lower() in f.name.lower())]
    texts = {f.stem for f in files if f.suffix.lower() == ".txt"}
    files = [f for f in files if f.suffix.lower() == ".txt" or f.stem not in texts][: args.limit]
    known = {}
    for path in (REPO / "eval/scenarios").glob("case-*.json"):
        scenario = json.loads(path.read_text(encoding="utf-8"))
        known[(scenario.get("source") or {}).get("file")] = scenario

    failed = wrong = 0
    for file in files:
        started = time.perf_counter()
        try:
            [extracted] = extract_case(SourceDocument(0, str(file), file.name, "client_case_file", {}, file.read_bytes()))
        except Exception as exc:  # noqa: BLE001
            if NOT_ENGLISH in str(exc):
                print(f"SKIPPED  {file.name}: not in English\n")
                continue
            print(f"ERROR  {file.name}: {type(exc).__name__}: {exc}\n")
            failed += 1
            continue
        case = extracted.record
        cars = "; ".join(f"{car.model or '(no model)'} {car.year or '(year not given)'}" for car in case.repaired_cars) or "(no car named)"
        print(f"{file.name}  ({time.perf_counter() - started:.0f} s)\n"
              f"  {case.make}: {cars}, {case.powertrain or 'powertrain not given'}{', DANGEROUS WORK' if case.sensitive else ''}\n"
              f"  codes: {', '.join(f.code for f in case.fault_codes if f.code) or '(none)'}"
              f"   wordings without a code: {sum(1 for f in case.fault_codes if not f.code)}"
              f"   tools: {', '.join(case.scan_tools) or '(none)'}\n"
              f"  outcome: {case.outcome}   parts replaced: {', '.join(p.part for p in case.parts_replaced) or '(none)'}\n"
              f"  root cause: {case.root_cause}")
        for question in case.review.open_questions:
            print(f"  - {question}")
            failed += question.startswith("CHECK FAILED")
        scenario = known.get(file.name)
        if scenario:
            want = {code_stem(c) for c in scenario["codes"]}
            got = {code_stem(f.code) for f in case.fault_codes if f.code}
            same_make = norm_key(scenario["vehicle"]["make"]) == norm_key(case.make)
            verdict = "same make and codes" if want == got and same_make else f"DIFFERENT (scenario: {scenario['vehicle']['make']}, {scenario['codes']})"
            wrong += verdict.startswith("DIFFERENT")
            print(f"  against the scenario written from this report: {verdict}")
        print()
    print(f"TOTAL  {len(files)} report(s); {failed} failed check(s) or error(s); {wrong} differing from a scenario")
    return 1 if failed or wrong else 0


if __name__ == "__main__":
    raise SystemExit(main())

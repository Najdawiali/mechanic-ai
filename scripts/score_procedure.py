"""Run the procedure extractor on a real DTC PDF with the real models, and score it.

  python scripts/score_procedure.py "C:/path/to/P0010.pdf" [--make .. --model .. --year ..]

Prints what the model read from each image, the steps it wrote, and every check that failed.
Where a hand-written reference record exists in eval/golden/procedures/ for the same procedure,
each step is compared with it: test points, normal value, conclusion. Exit code 1 if anything
differs or a check failed. This calls DeepInfra (the key in .env); a run costs well under a cent.
"""

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.ingest.extractors import SourceDocument  # noqa: E402
from app.ingest.procedure_extractor import extract_with_readings  # noqa: E402
from app.kb.schemas import Procedure  # noqa: E402


def point(p) -> str:
    return "ground" if p.ground else f"{p.connector} pin {p.pin}"


def squeeze(text: str | None) -> str:
    return " ".join((text or "").lower().split())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdf", type=Path)
    for name in ("make", "model", "year"):
        parser.add_argument(f"--{name}")
    args = parser.parse_args()
    declared = {k: v for k in ("make", "model", "year") if (v := getattr(args, k))}
    started = time.perf_counter()
    extracted, readings = extract_with_readings(
        SourceDocument(0, str(args.pdf), args.pdf.name, "licensed_procedure", declared, args.pdf.read_bytes()))
    record: Procedure = extracted.record
    print(f"{record.procedure_id}  ({time.perf_counter() - started:.0f} s)\n\nImages:")
    for r in readings:
        print(f"  {r.number:>2}  {r.note()}")
    print("\nSteps:")
    for step in record.steps:
        if step.measure and step.normal:
            n = step.normal
            print(f"  {step.id}  {point(step.measure.from_point)}  <->  {point(step.measure.to_point)}   "
                  f"normal: {n.op} {n.min} {n.max} {n.unit}\n       if abnormal: {step.if_abnormal}")
        else:
            print(f"  {step.id}  {step.check}")
    print(f"  all normal: {record.elimination_conclusion}")
    failed = [q for q in record.review.open_questions if q.startswith("CHECK FAILED")]
    print("\nChecks against the PDF itself: " + ("all passed" if not failed else ""))
    for q in failed:
        print(f"  {q}")

    differences: list[str] = []
    reference = next((Procedure(**json.loads(p.read_text(encoding="utf-8"))) for p in sorted((REPO / "eval/golden/procedures").glob("*.json"))
                      if json.loads(p.read_text(encoding="utf-8"))["procedure_id"] == record.procedure_id), None)
    if reference is None:
        print("\nNo reference record for this procedure: compare the steps above with the PDF by eye.")
    else:
        if len(record.steps) != len(reference.steps):
            differences.append(f"{len(record.steps)} steps, reference has {len(reference.steps)}")
        for got, want in zip(record.steps, reference.steps):
            for label, a, b in (
                ("test points", {point(got.measure.from_point), point(got.measure.to_point)} if got.measure else None,
                 {point(want.measure.from_point), point(want.measure.to_point)} if want.measure else None),
                ("terminal side", got.measure and got.measure.from_point.terminal_side, want.measure and want.measure.from_point.terminal_side),
                ("normal value", got.normal and (got.normal.op, got.normal.min, got.normal.max, got.normal.unit),
                 want.normal and (want.normal.op, want.normal.min, want.normal.max, want.normal.unit)),
                ("conclusion", squeeze(got.if_abnormal), squeeze(want.if_abnormal)),
                ("next step", got.if_normal, want.if_normal),
            ):
                if a != b:
                    differences.append(f"step {want.id} {label}: got {a}, reference {b}")
        if squeeze(record.elimination_conclusion) != squeeze(reference.elimination_conclusion):
            differences.append(f"all-normal conclusion: got {record.elimination_conclusion!r}, reference {reference.elimination_conclusion!r}")
        print(f"\nAgainst the reference record: {'identical in every checked field' if not differences else ''}")
        for d in differences:
            print(f"  DIFFERENT  {d}")
    return 1 if failed or differences else 0


if __name__ == "__main__":
    raise SystemExit(main())

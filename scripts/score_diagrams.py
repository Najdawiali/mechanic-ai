"""Run the diagram reader on real diagram PDFs and score it.

  python scripts/score_diagrams.py "C:/path/to/diagrams" [more folders or files]

For every PDF it prints how many wires were read, how many passed the colour check (the colour
a wire is drawn in against the colour printed beside it), and what the reader could not attach.
Where a hand-verified answer file exists in eval/golden/diagrams/, the routes are compared with
it. The exit code is 1 if any verified route is wrong or any colour check fails.
"""

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.ingest.diagrams import read_pdf, trace  # noqa: E402


def main(argv: list[str]) -> int:
    pdfs = sorted(f for a in argv for f in ([Path(a)] if Path(a).is_file() else Path(a).rglob("*.pdf")))
    if not pdfs:
        print(__doc__)
        return 2
    golden = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((REPO / "eval/golden/diagrams").glob("*.json"))]
    failed = False
    totals = {"wires": 0, "colour_ok": 0, "colour_wrong": 0, "unlabelled": 0, "problems": 0, "routes": 0, "routes_ok": 0}
    for pdf in pdfs:
        diagram = read_pdf(str(pdf))
        if not diagram.pages:
            print(f"SKIP  {pdf.name}: no diagram pages")
            continue
        wires = [w for page in diagram.pages for w in page.wires]
        ok = sum(w.colour_agrees is True for w in wires)
        wrong = sum(w.colour_agrees is False for w in wires)
        totals["wires"] += len(wires); totals["colour_ok"] += ok; totals["colour_wrong"] += wrong  # noqa: E702
        totals["unlabelled"] += len(wires) - ok - wrong; totals["problems"] += len(diagram.problems)  # noqa: E702
        print(f"{'FAIL' if wrong else 'OK  '}  {diagram.title}: {len(diagram.pages)} page(s), {len(wires)} wires, "
              f"colour check {ok} agree / {wrong} disagree / {len(wires) - ok - wrong} with no printed colour, "
              f"{len(diagram.problems)} not attached")
        for problem in diagram.problems:
            print(f"        {problem}")
        failed |= bool(wrong)
        for answers in golden:
            if answers["file_contains"] not in pdf.name:
                continue
            for pin, expected in answers["routes"].items():
                got = sorted(" ".join(str(h) for h in route[1:]) for route in trace(diagram, answers["component"], pin))
                totals["routes"] += 1
                if got == sorted(expected):
                    totals["routes_ok"] += 1
                else:
                    failed = True
                    print(f"        WRONG  {answers['component']} pin {pin}\n          expected {sorted(expected)}\n          got      {got}")
    print(f"\nTOTAL  {totals['wires']} wires; colour check {totals['colour_ok']} agree, {totals['colour_wrong']} disagree, "
          f"{totals['unlabelled']} with no printed colour; {totals['problems']} ends not attached; "
          f"verified routes {totals['routes_ok']} of {totals['routes']} correct")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

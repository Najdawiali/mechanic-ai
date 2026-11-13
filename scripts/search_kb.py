"""Try the knowledge-base search: by words, by meaning, and both together.

  python scripts/search_kb.py Tesla "Model S" 2022 "the air conditioning cuts out while driving"

Drafts are included, marked as such: this is a tool for checking the knowledge base, and the
assistant itself serves approved records only. The number beside a piece found by meaning is
its similarity to the query (1 = the same meaning, 0 = unrelated).
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import connect  # noqa: E402
from app.kb.schemas import Vehicle  # noqa: E402
from app.kb.store import APPROVED_AND_DRAFT, KnowledgeStore  # noqa: E402
from app.llm.gateway import LLMGateway  # noqa: E402


def show(title: str, hits) -> None:
    print(f"\n{title}")
    if not hits:
        print("  (nothing)")
    for place, hit in enumerate(hits, start=1):
        how = f", found by {' and '.join(hit.found_by)}" if hit.found_by else ""
        print(f"  {place}. {hit.record_key}  [{hit.status}, car fit: {hit.scope_match}{how}]")
        for unit in hit.matched_units[:2]:
            print(f"       {unit.rank:.2f}  {unit.kind}: {unit.text[:110]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("make")
    parser.add_argument("model")
    parser.add_argument("year", type=int)
    parser.add_argument("query")
    parser.add_argument("--powertrain", choices=["ICE", "Hybrid", "PHEV", "EV"])
    args = parser.parse_args()
    car = Vehicle(make=args.make, model=args.model, year=args.year, powertrain=args.powertrain)
    [vector] = LLMGateway().embed([args.query], query=True)
    with connect() as conn:
        store = KnowledgeStore(conn)
        show("By words (any shared word, unfiltered):", store.search_text(car, args.query, statuses=APPROVED_AND_DRAFT))
        show("By meaning (nearest pieces, unfiltered):", store.search_meaning(car, vector, statuses=APPROVED_AND_DRAFT))
        show("What the assistant would get (two shared words, or similarity above the floor):",
             store.search(car, args.query, vector, statuses=APPROVED_AND_DRAFT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

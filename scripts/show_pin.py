"""Look up a component's pins in the wiring diagrams stored for a car.

  python scripts/show_pin.py Acura Integra 2025 "VSA MODULATOR-CONTROL UNIT" 31
  python scripts/show_pin.py Acura Integra 2025 "VSA MODULATOR-CONTROL UNIT"       (all its pins)
  python scripts/show_pin.py Acura Integra 2025 --find "wheel speed sensor"        (find component names)

Drafts are included, marked as such: this is a tool for checking the knowledge base, and the
assistant itself serves approved records only.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import connect  # noqa: E402
from app.kb.schemas import Vehicle  # noqa: E402
from app.kb.store import APPROVED_AND_DRAFT, KnowledgeStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("make")
    parser.add_argument("model")
    parser.add_argument("year", type=int)
    parser.add_argument("component", nargs="?")
    parser.add_argument("pin", nargs="?")
    parser.add_argument("--engine")
    parser.add_argument("--find", help="words to search component names for")
    args = parser.parse_args()
    car = Vehicle(make=args.make, model=args.model, year=args.year, engine_code=args.engine)
    with connect() as conn:
        store = KnowledgeStore(conn)
        if args.find:
            hits = store.search_text(car, args.find, record_types=("diagram",), statuses=APPROVED_AND_DRAFT, limit=10)
            for hit in hits:
                print(f"{hit.record.title} [{hit.status}, {hit.record.verdict}]")
                for unit in hit.matched_units[:8]:
                    print(f"    {unit.text}")
            return 0 if hits else 1
        if not args.component:
            parser.error("give a component name, or --find words")
        pins = store.get_pins(car, args.component, args.pin, statuses=APPROVED_AND_DRAFT)
        if not pins:
            print("nothing stored for this car, component and pin")
            return 1
        for hit in pins:
            fuse = f", {hit.fuse}" if hit.fuse else ""
            print(f"{hit.component} pin {hit.pin}: {hit.wire_colour or 'colour not given'}{fuse}"
                  f"   [{hit.diagram_title}, page {hit.page}, reading {hit.verdict}]")
            for route in hit.routes:
                print(f"    {'' if route['complete'] else 'INCOMPLETE  '}{route['text']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

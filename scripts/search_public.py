"""Look up the NHTSA public records for one car, as the assistant would (after import and embedding).

    python scripts/search_public.py Honda Accord 2018 "engine stalls at idle"
    python scripts/search_public.py Honda Accord 2018 "" --codes P0300
    python scripts/search_public.py --status          # what is in pub.document / pub.chunk
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.db import connect  # noqa: E402
from app.kb.schemas import Vehicle  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("make", nargs="?")
    p.add_argument("model", nargs="?")
    p.add_argument("year", nargs="?", type=int)
    p.add_argument("question", nargs="?", default="")
    p.add_argument("--codes", default="")
    p.add_argument("--status", action="store_true")
    args = p.parse_args(argv)
    settings = get_settings()
    with connect() as conn:
        if args.status or not args.make:
            for r in conn.execute("SELECT source_type, count(*) AS n FROM pub.document GROUP BY 1 ORDER BY 1").fetchall():
                print(f"documents  {r['source_type']:<24} {r['n']}")
            for r in conn.execute("SELECT coalesce(embedding_model, '(no vector)') AS m, count(*) AS n FROM pub.chunk "
                                  "GROUP BY 1 ORDER BY 1").fetchall():
                print(f"pieces     {r['m']:<40} {r['n']}")
            print(f"search uses: {settings.public_model} · types {', '.join(settings.public_types)}")
            return 0
        from app.llm.gateway import LLMGateway
        from app.public.source import from_settings

        records = from_settings(settings.model_copy(update={"public_records": "nhtsa"}), conn, LLMGateway())
        codes = [c for c in args.codes.replace(",", " ").split() if c]
        found = records.lookup(Vehicle(make=args.make, model=args.model, year=args.year), codes, args.question)
        if found.calls[0].error:
            print(f"FAIL  {found.calls[0].error}")
            return 1
        print(f"{len(found.items)} record(s) in {found.calls[0].latency_s:.2f} s")
        for i in found.items:
            print(f"- [{i.extra['applies']}] {i.extra['label']} · found by {', '.join(i.extra['found_by'])} · "
                  f"similarity {i.extra['similarity']}\n  {i.title}\n  {i.text[:300]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Web search, live: the real providers, then the whole assistant with the real model.

    python scripts/web_smoke.py            the web lookup on the 8 probe cases (Exa; Tavily if Exa fails)
    python scripts/web_smoke.py --engine   also two full assistant turns (needs the database and DEEPINFRA_API_KEY)

It reads EXA_API_KEY and TAVILY_API_KEY from .env and turns web search on for this run only, whatever
EXTERNAL_SOURCE says. Cost: about $0.06 of Exa for the lookups, plus two turns of model calls with --engine.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.kb.schemas import Vehicle
from app.web.source import WebSource

CASES = [  # the probe's cars and codes (scripts/web_probe.py)
    ({"make": "Acura", "model": "Integra", "year": 2025, "engine_code": "L15CA"}, "P0341"),
    ({"make": "Acura", "model": "Integra", "year": 2025, "engine_code": "L15CA"}, "P0010"),
    ({"make": "Toyota", "model": "Camry", "year": 2018}, "P0420"),
    ({"make": "Hyundai", "model": "Sonata Hybrid", "year": 2020}, "P1990"),
    ({"make": "Volkswagen", "model": "ID.4", "year": 2021}, "P0C4400"),
    ({"make": "BYD", "model": "Qin Plus DM-i", "year": 2024}, "P2B5313"),
    ({"make": "EXEED", "model": "RX", "year": None}, "P1A7309"),
    ({"make": "Tesla", "model": "Model Y", "year": 2022}, "PCS_a027"),
]
TURNS = [  # (vehicle, the mechanic's message): codes with no verified data in the knowledge base
    ({"make": "Acura", "model": "Integra", "year": 2025, "engine_code": "L15CA", "powertrain": "ICE"}, "en", "2025 Integra, code P0341. Where do I start?"),
    ({"make": "Hyundai", "model": "Sonata Hybrid", "year": 2020, "powertrain": "Hybrid"}, "ar", "سوناتا هايبرد 2020 طالع معها كود P1990، شو بتنصحني؟"),
]


def lookups(source: WebSource) -> None:
    print("web lookup, 8 cases\n")
    total = 0.0
    for vehicle, code in CASES:
        found = source.lookup(Vehicle(**vehicle), [code], "")
        calls = ", ".join(f"{c.model} {c.latency_s:.1f}s{' FAILED ' + c.error if c.error else ''}" for c in found.calls)
        total += sum(c.cost_usd or 0 for c in found.calls)
        print(f"{vehicle['make']} {vehicle['model']} {vehicle.get('year') or ''} {code}: {len(found.items)} kept  ({calls})")
        for item in found.items:
            why = f"  [{'; '.join(item.extra['reasons'])}]" if item.extra["reasons"] else ""
            print(f"    {item.extra['applies']:<5} {item.extra['domain']}: {item.title[:80]}{why}")
    print(f"\nExa cost for the lookups: ${total:.3f}")


def engine_turns(source: WebSource) -> None:
    from app.db import connect
    from app.engine.engine import Engine, Roles
    from app.engine.model import GatewayModel
    from app.engine.state import SessionState
    from app.kb.store import APPROVED_AND_DRAFT, KnowledgeStore
    from app.llm.gateway import LLMGateway

    print("\nfull assistant turns, real model\n")
    with connect() as conn:
        for vehicle, language, text in TURNS:
            model = GatewayModel(LLMGateway())
            engine = Engine(KnowledgeStore(conn), model, statuses=APPROVED_AND_DRAFT, external=source,
                            roles=Roles.from_settings(get_settings()))
            reply = engine.turn(SessionState(vehicle=vehicle, language=language), text).reply
            conn.rollback()
            print(f"--- {text}\ntier: {reply.tier}   type: {reply.type}\n{reply.message}")
            for e in reply.evidence:
                print(f"  source ({e.applies}): {e.title}\n          {e.url}")
            for t in model.trace:
                note = f"  REFUSED: {t.rejected}" if t.rejected else ""
                print(f"  call {t.purpose:<10} {t.model:<28} {t.latency_s:5.1f}s{note}")
            print()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--engine", action="store_true", help="also run two full assistant turns with the real model")
    args = parser.parse_args(argv)
    settings = get_settings()
    if settings.exa_api_key is None:
        print("FAIL  EXA_API_KEY is not set in .env")
        return 1
    source = WebSource.from_settings(settings)
    lookups(source)
    if args.engine:
        engine_turns(source)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

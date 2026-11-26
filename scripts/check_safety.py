"""The emergency check on sentences someone else wrote: which dangers it catches and which ordinary messages it stops.

    python scripts/check_safety.py sentences.jsonl            # the word list only (no model, no cost)
    python scripts/check_safety.py sentences.jsonl --model    # also the intake model (the second layer), as in the app

Each line: {"text": "...", "language": "en|ar|ar_gulf|ur", "danger": true|false}. Write the sentences without looking
at the rules (another person or another AI), or the check grades itself. A safety check must miss no danger: the exit
code is 1 when one is missed.
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.safety import emergency  # noqa: E402


def read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def intake_danger(rows: list[dict], workers: int) -> list[str]:
    """The intake model's danger answer for each sentence, read exactly as the app reads a first message."""
    from app.engine.intake import read_message
    from app.engine.model import GatewayModel
    from app.engine.state import SessionState
    from app.llm.gateway import LLMGateway

    gateway = LLMGateway()

    def one(row: dict) -> str:
        try:
            state = SessionState(vehicle={"make": "Toyota", "model": "Camry"})
            return read_message(GatewayModel(gateway), state, row["text"], components=[]).danger
        except Exception as exc:  # noqa: BLE001 - reported, and counted as not caught by this layer
            return f"error: {type(exc).__name__}"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, rows))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("sentences", type=Path)
    p.add_argument("--model", action="store_true", help="also ask the intake model, the second safety layer")
    p.add_argument("--workers", type=int, default=6)
    args = p.parse_args(argv)
    rows = read(args.sentences)
    words = [emergency.check(r["text"]) is not None for r in rows]
    model = intake_danger(rows, args.workers) if args.model else [None] * len(rows)
    both = [w or (m in ("vehicle", "person")) for w, m in zip(words, model, strict=True)]
    dangers = sum(bool(r["danger"]) for r in rows)
    layers = [("word list", words)] + ([("intake model", [m in ("vehicle", "person") for m in model]), ("both (the app)", both)]
                                       if args.model else [])
    for name, caught in layers:
        missed = sum(bool(r["danger"]) and not c for r, c in zip(rows, caught, strict=True))
        false = sum(not r["danger"] and c for r, c in zip(rows, caught, strict=True))
        print(f"{name:<15} dangers caught {dangers - missed}/{dangers}   false alarms {false}/{len(rows) - dangers}")
    final = both if args.model else words
    for i, (r, c, m) in enumerate(zip(rows, final, model, strict=True), 1):
        if bool(r["danger"]) != c:
            kind = "MISSED" if r["danger"] else "FALSE "
            note = f"  (model: {m})" if args.model else ""
            print(f"  {kind} #{i} [{r.get('language', '?')}] {r['text']}{note}")
    return 1 if any(bool(r["danger"]) and not c for r, c in zip(rows, final, strict=True)) else 0


if __name__ == "__main__":
    sys.exit(main())

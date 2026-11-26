"""The emergency check on sentences someone else wrote: which dangers it catches and which ordinary messages it stops.

    python scripts/check_safety.py sentences.jsonl            # the word list only (no model, no cost)
    python scripts/check_safety.py sentences.jsonl --model    # also the intake model (the second layer), as in the app

Each line: {"text": "...", "language": "en|ar|ar_gulf|ur", "danger": true|false}. Write the sentences without looking
at the rules (another person or another AI), or the check grades itself. With --model it shows what the app does with
each one: stop, warn then answer, or answer. A danger must never get a plain answer: the exit code is then 1.
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


def app_outcome(word_list_fired: bool, model: str | None) -> str:
    """What the app does (app/engine/engine.py, turn): "stop", "warn" (the warning, then the answer) or "answer"."""
    danger = model in ("vehicle", "person")
    if word_list_fired:
        return "warn" if model == "none" else "stop"  # no model answer (None or an error): stop, to be safe
    return "stop" if danger else "answer"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("sentences", type=Path)
    p.add_argument("--model", action="store_true", help="also ask the intake model, the second safety layer")
    p.add_argument("--workers", type=int, default=6)
    args = p.parse_args(argv)
    rows = read(args.sentences)
    words = [emergency.check(r["text"]) is not None for r in rows]
    model = intake_danger(rows, args.workers) if args.model else [None] * len(rows)
    dangers = sum(bool(r["danger"]) for r in rows)
    harmless = len(rows) - dangers

    def count(flags: list[bool], danger: bool) -> int:
        return sum(f for r, f in zip(rows, flags, strict=True) if bool(r["danger"]) == danger)

    print(f"word list       dangers caught {count(words, True)}/{dangers}   false alarms {count(words, False)}/{harmless}")
    if not args.model:
        final = words
        problems = [(i, r, "MISSED" if r["danger"] else "FALSE ", "") for i, (r, w) in enumerate(zip(rows, words, strict=True), 1)
                    if bool(r["danger"]) != w]
    else:
        flagged = [m in ("vehicle", "person") for m in model]
        print(f"intake model    dangers caught {count(flagged, True)}/{dangers}   false alarms {count(flagged, False)}/{harmless}")
        outcome = [app_outcome(w, m) for w, m in zip(words, model, strict=True)]
        stop = [o == "stop" for o in outcome]
        warned = [o != "answer" for o in outcome]
        print(f"the app         dangers stopped {count(stop, True)}/{dangers}, warned at least {count(warned, True)}/{dangers}   "
              f"harmless stopped {count(stop, False)}/{harmless}, warned then answered {count(warned, False) - count(stop, False)}/{harmless}")
        final = warned
        problems = [(i, r, {"answer": "MISSED", "warn": "WARNED", "stop": "STOPPED"}[o], f"  (model: {m})")
                    for i, (r, o, m) in enumerate(zip(rows, outcome, model, strict=True), 1)
                    if (r["danger"] and o != "stop") or (not r["danger"] and o == "stop")]
    for i, r, kind, note in problems:
        print(f"  {kind} #{i} [{r.get('language', '?')}] {'danger' if r['danger'] else 'harmless'}: {r['text']}{note}")
    return 1 if any(bool(r["danger"]) and not f for r, f in zip(rows, final, strict=True)) else 0


if __name__ == "__main__":
    sys.exit(main())

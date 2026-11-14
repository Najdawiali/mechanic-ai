"""Check that the embedding model answers and gives vectors of the size the database expects.

  python scripts/check_embeddings.py
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.llm.gateway import LLMGateway  # noqa: E402


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def main() -> int:
    settings = get_settings()
    try:
        gateway = LLMGateway()
        stored = gateway.embed(["The A/C stops intermittently while driving.", "The rear window does not move after the battery was replaced."])
        [query] = gateway.embed(["air conditioning cuts out on the road"], query=True)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL  {settings.llm_embedding_model}: {type(exc).__name__}: {exc}")
        return 1
    related, unrelated = cosine(query, stored[0]), cosine(query, stored[1])
    ok = len(query) == settings.embedding_dimensions and related > unrelated
    print(f"{'PASS' if ok else 'FAIL'}  {settings.llm_embedding_model}: {len(query)} numbers per text; "
          f"similarity to the matching sentence {related:.2f}, to the unrelated one {unrelated:.2f}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

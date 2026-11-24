"""Vectors for the public-record pieces, made with our embedding model (or the one PUBLIC_EMBEDDING_MODEL names).

    python -m app.public.embed --limit 640       # a small first run: prints tokens, cost and speed
    python -m app.public.embed                   # all that are missing; safe to stop and run again

A piece with no vector, or one made by another model, is embedded again. Pieces are sent as they are (no
instruction), as the model expects for stored text. Batches run in parallel; a failed batch is retried with a
pause, and a batch that still fails stops the run without losing what was written.
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg

from app.config import get_settings
from app.kb.embedder import as_pgvector


def _with_retry(fn, texts, tries: int = 4):
    for attempt in range(tries):
        try:
            return fn(texts)
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep((2, 5, 15)[attempt])


def embed_missing(conn: psycopg.Connection, gateway, *, model: str, batch: int = 64, workers: int = 4,
                  limit: int | None = None, log=print) -> int:
    done, started, last_id = 0, time.perf_counter(), 0

    def embed(texts: list[str]) -> list[list[float]]:
        return gateway.embed(texts, model=model)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        while limit is None or done < limit:
            take = batch * workers if limit is None else min(batch * workers, limit - done)
            rows = conn.execute(
                "SELECT id, text FROM pub.chunk WHERE id > %s AND (embedding IS NULL OR embedding_model IS DISTINCT FROM %s) "
                "ORDER BY id LIMIT %s", (last_id, model, take)).fetchall()
            if not rows:
                break
            last_id = rows[-1]["id"]
            parts = [rows[i:i + batch] for i in range(0, len(rows), batch)]
            vectors = list(pool.map(lambda part: _with_retry(embed, [r["text"] for r in part]), parts))
            with conn.transaction(), conn.cursor() as cur:
                cur.executemany("UPDATE pub.chunk SET embedding = %s::vector, embedding_model = %s WHERE id = %s",
                                [(as_pgvector(v), model, r["id"]) for part, vecs in zip(parts, vectors)
                                 for r, v in zip(part, vecs)])
            done += len(rows)
            secs = time.perf_counter() - started
            log(f"{done:>7} pieces · {getattr(gateway, 'embed_tokens', 0):>10} tokens · {done / secs:.0f} pieces/s", flush=True)
    return done


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--limit", type=int)
    p.add_argument("--batch", type=int, default=64)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--price-per-m", type=float, default=0.02, help="USD per 1M tokens (Qwen3-Embedding-4B on DeepInfra)")
    args = p.parse_args(argv)

    from app.db import connect
    from app.llm.gateway import LLMGateway

    model = get_settings().public_model
    gateway = LLMGateway()
    with connect() as conn:
        left = conn.execute("SELECT count(*) AS n FROM pub.chunk WHERE embedding IS NULL OR embedding_model IS DISTINCT FROM %s",
                            (model,)).fetchone()["n"]
        print(f"{left} piece(s) to embed with {model}")
        n = embed_missing(conn, gateway, model=model, batch=args.batch, workers=args.workers, limit=args.limit)
    tokens = gateway.embed_tokens
    print(f"embedded {n} piece(s) · {tokens} tokens · about ${tokens / 1e6 * args.price_per_m:.3f}"
          + (f" · {tokens / n:.0f} tokens per piece -> all {left}: about ${left * tokens / n / 1e6 * args.price_per_m:.2f}" if n else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())

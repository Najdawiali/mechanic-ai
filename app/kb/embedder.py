"""Vectors for the search units: made in the background, never in the way of saving a record.

A record is stored and searchable by keywords at once. Its vectors are added afterwards; if the
embedding model cannot be reached, nothing is lost and the units are picked up on the next pass.

  python -m app.kb.embedder        embed every unit that has no vector from the current model
"""

from __future__ import annotations

import psycopg

from app.config import get_settings


def as_pgvector(vector: list[float]) -> str:
    return "[" + ",".join(f"{value:.7g}" for value in vector) + "]"


def embed_missing(conn: psycopg.Connection, gateway, *, batch: int = 64, limit: int | None = None) -> int:
    """Embed the units that have no vector, or one from another model. Returns how many were done."""
    model = get_settings().llm_embedding_model
    done = 0
    while limit is None or done < limit:
        rows = conn.execute(
            "SELECT id, text FROM kb.search_unit WHERE embedding IS NULL OR embedding_model IS DISTINCT FROM %s "
            "ORDER BY id LIMIT %s",
            (model, batch),
        ).fetchall()
        if not rows:
            break
        vectors = gateway.embed([row["text"] for row in rows])
        with conn.transaction(), conn.cursor() as cur:
            cur.executemany(
                "UPDATE kb.search_unit SET embedding = %s::vector, embedding_model = %s WHERE id = %s",
                [(as_pgvector(vector), model, row["id"]) for row, vector in zip(rows, vectors)],
            )
        done += len(rows)
    return done


def main() -> int:
    from app.db import connect
    from app.llm.gateway import LLMGateway

    with connect() as conn:
        conn.autocommit = True
        print(f"embedded {embed_missing(conn, LLMGateway())} search unit(s) with {get_settings().llm_embedding_model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

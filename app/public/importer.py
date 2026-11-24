"""Import the venorexai knowledge base (NHTSA records) into our schema pub.

    # 1. restore his dump into a SEPARATE database (his script, from his folder; never into ours):
    #    createdb venorex_kb ; then in it: CREATE EXTENSION vector; CREATE EXTENSION pg_trgm;
    #    bash scripts/restore.sh ~/kb/kb-20261120-103041.dump postgresql://mechanic:mechanic@localhost:5432/venorex_kb
    # 2. import into ours (safe to re-run: what is already there is skipped):
    python -m app.public.importer --source postgresql://mechanic:mechanic@localhost:5432/venorex_kb
    python -m app.public.importer --source ... --limit 2000          # a small first try
    python -m app.public.importer --source ... --keep-vectors        # also copy his vectors (his embedding model)

Without --keep-vectors the pieces arrive with no vector and `python -m app.public.embed` makes them with our
model. With it, his vectors are kept with the name of his model, and our search must then embed its query
with that same model (setting PUBLIC_EMBEDDING_MODEL); vectors of two models are never compared.
Only his three NHTSA record types are taken; his users, chats and benchmark tables are not read.
"""

from __future__ import annotations

import argparse
import sys
import time

import psycopg
from psycopg.rows import dict_row

from app.public.keys import make_key, model_key

TYPES = {"official_recall", "official_investigation", "unverified_report"}
SOURCES = {"nhtsa_recall", "nhtsa_investigation", "nhtsa_complaint"}

_DOC_SQL = """
INSERT INTO pub.document (source, external_id, source_type, make_key, make, model, model_key, year_from, year_to,
                          codes, component, title, text, url, dataset_version, content_hash, meta)
VALUES (%(source)s, %(external_id)s, %(source_type)s, %(make_key)s, %(make)s, %(model)s, %(model_key)s, %(year_from)s,
        %(year_to)s, %(codes)s, %(component)s, %(title)s, %(text)s, %(url)s, %(dataset_version)s, %(content_hash)s,
        %(meta)s)
ON CONFLICT (content_hash) DO UPDATE SET content_hash = EXCLUDED.content_hash
RETURNING id"""

_CHUNK_SQL = """
INSERT INTO pub.chunk (document_id, chunk_index, text, make_key, model_key, year_from, year_to, source_type, codes,
                       embedding, embedding_model)
VALUES (%(document_id)s, %(chunk_index)s, %(text)s, %(make_key)s, %(model_key)s, %(year_from)s, %(year_to)s,
        %(source_type)s, %(codes)s, %(embedding)s::vector, %(embedding_model)s)
ON CONFLICT (document_id, chunk_index) DO NOTHING"""


def _codes(value) -> list[str]:
    return sorted({str(c).strip().upper() for c in (value or []) if str(c).strip()})


def document_row(d: dict) -> dict | None:
    """His document -> ours, or None when it is not one of the three NHTSA kinds or has no car."""
    mk, mo = make_key(d.get("make")), model_key(d.get("model"))
    if d.get("source") not in SOURCES or d.get("source_type") not in TYPES or not mk or not mo:
        return None
    from psycopg.types.json import Jsonb

    return {"source": d["source"], "external_id": d["external_id"], "source_type": d["source_type"], "make_key": mk,
            "make": d["make"], "model": d["model"], "model_key": mo, "year_from": d.get("year_from"),
            "year_to": d.get("year_to"), "codes": _codes(d.get("dtcs")), "component": d.get("component"),
            "title": d["title"], "text": d["text"], "url": d.get("url"), "dataset_version": d["dataset_version"],
            "content_hash": d["content_hash"],
            "meta": Jsonb({**(d.get("meta") or {}), "imported_from": "venorexai", "venorex_document_id": d["id"],
                           "markets": d.get("markets") or ["US"]})}


def import_all(source_url: str, target: psycopg.Connection, *, batch: int = 1000, limit: int | None = None,
               keep_vectors: bool = False, log=print) -> dict:
    stats = {"documents": 0, "skipped_documents": 0, "chunks": 0, "with_vector": 0}
    last_id, started = 0, time.perf_counter()
    vec_cols = ", c.embedding::text AS embedding, c.embedding_model" if keep_vectors else ""
    with psycopg.connect(source_url, row_factory=dict_row) as src:
        src.read_only = True
        while limit is None or stats["documents"] + stats["skipped_documents"] < limit:
            size = batch if limit is None else min(batch, limit - stats["documents"] - stats["skipped_documents"])
            docs = src.execute("SELECT * FROM rag.documents WHERE id > %s ORDER BY id LIMIT %s", (last_id, size)).fetchall()
            if not docs:
                break
            last_id = docs[-1]["id"]
            chunks = src.execute(
                f"SELECT c.document_id, c.chunk_index, c.text{vec_cols} FROM rag.chunks c "
                "WHERE c.document_id = ANY(%s) ORDER BY c.document_id, c.chunk_index", ([d["id"] for d in docs],)).fetchall()
            by_doc: dict[int, list[dict]] = {}
            for c in chunks:
                by_doc.setdefault(c["document_id"], []).append(c)
            with target.transaction():
                for d in docs:
                    row = document_row(d)
                    if row is None:
                        stats["skipped_documents"] += 1
                        continue
                    our_id = target.execute(_DOC_SQL, row).fetchone()
                    our_id = our_id["id"] if isinstance(our_id, dict) else our_id[0]
                    rows = [{"document_id": our_id, "chunk_index": c["chunk_index"], "text": c["text"],
                             "make_key": row["make_key"], "model_key": row["model_key"], "year_from": row["year_from"],
                             "year_to": row["year_to"], "source_type": row["source_type"], "codes": row["codes"],
                             "embedding": c.get("embedding"), "embedding_model": c.get("embedding_model") if c.get("embedding") else None}
                            for c in by_doc.get(d["id"], [])]
                    with target.cursor() as cur:
                        cur.executemany(_CHUNK_SQL, rows)
                    stats["documents"] += 1
                    stats["chunks"] += len(rows)
                    stats["with_vector"] += sum(r["embedding"] is not None for r in rows)
            log(f"{stats['documents']:>7} documents · {stats['chunks']:>7} pieces · skipped {stats['skipped_documents']} · "
                f"{time.perf_counter() - started:.0f} s", flush=True)
    return stats


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--source", required=True, help="URL of the database holding his restored dump")
    p.add_argument("--limit", type=int, help="import only the first N documents")
    p.add_argument("--batch", type=int, default=1000)
    p.add_argument("--keep-vectors", action="store_true", help="copy his vectors (made with his embedding model)")
    args = p.parse_args(argv)

    from app.db import connect

    with connect() as target:
        stats = import_all(args.source, target, batch=args.batch, limit=args.limit, keep_vectors=args.keep_vectors)
        totals = target.execute(
            "SELECT source_type, count(*) AS n FROM pub.document GROUP BY source_type ORDER BY source_type").fetchall()
    print(f"imported this run: {stats}")
    for t in totals:
        print(f"  in pub.document: {t['source_type']:<24} {t['n']}")
    if not args.keep_vectors:
        print("next: python -m app.public.embed     (makes the vectors with our embedding model)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

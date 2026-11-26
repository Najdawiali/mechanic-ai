"""Search the public records for ONE car: fault code (exact), meaning (vectors) and words, fused by rank.

Rules (same spirit as the workshop's knowledge base):
- Only the car's own records: make group, model (same, or one is the other plus words = "check"), model year in the
  record's range (car year unknown = "check"). A record for another car is never returned.
- A record is offered when its piece carries one of the reported fault codes, or is close in meaning to the
  question (similarity at least `min_similarity`). Shared words only help the order: they never bring a record
  on their own (in our own knowledge base, word-only hits were noise).
- One result per record (the best piece); records that fit come before records to check.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import psycopg

from app.kb.embedder import as_pgvector
from app.kb.schemas import Vehicle
from app.public.keys import applies, make_key, model_key

RRF_K = 60
PER_CHANNEL = 30
TYPE_ORDER = {"official_recall": 0, "official_investigation": 1, "unverified_report": 2}

_SCOPE = """c.make_key = %(make)s
    AND (c.model_key = %(model)s OR c.model_key LIKE %(model)s || ' %%' OR %(model)s LIKE c.model_key || ' %%')
    AND (%(year)s::int IS NULL OR c.year_from IS NULL OR %(year)s::int BETWEEN c.year_from AND coalesce(c.year_to, c.year_from))
    AND c.source_type = ANY(%(types)s)"""


@dataclass
class PublicHit:
    document_id: int
    source: str
    external_id: str
    source_type: str
    title: str
    text: str  # the best piece
    url: str | None
    component: str | None
    model: str
    year_from: int | None
    year_to: int | None
    codes: list[str]
    applies: str  # fits | check
    similarity: float | None = None
    found_by: list[str] = field(default_factory=list)
    score: float = 0.0


class PublicStore:
    def __init__(self, conn: psycopg.Connection, *, model: str, types: tuple[str, ...], min_similarity: float = 0.5):
        self.conn, self.model, self.types, self.min_similarity = conn, model, list(types), min_similarity

    def _params(self, vehicle: Vehicle) -> dict | None:
        mk, mo = make_key(vehicle.make), model_key(vehicle.model)
        if not mk or not mo:
            return None
        return {"make": mk, "model": mo, "year": vehicle.year, "types": self.types}

    def search(self, vehicle: Vehicle, codes: list[str], query_vector: list[float] | None, words: str = "",
               limit: int = 4) -> list[PublicHit]:
        params = self._params(vehicle)
        if params is None:
            return []
        codes = sorted({c.strip().upper() for c in codes if c.strip()})
        ranked: dict[str, list[int]] = {}
        similarity: dict[int, float] = {}
        if codes:
            ranked["code"] = [r["id"] for r in self.conn.execute(
                f"SELECT c.id FROM pub.chunk c WHERE {_SCOPE} AND c.codes && %(codes)s::text[] ORDER BY c.id LIMIT {PER_CHANNEL}",
                params | {"codes": codes}).fetchall()]
        if query_vector is not None:
            rows = self.conn.execute(
                f"SELECT c.id, 1 - (c.embedding <=> %(vec)s::vector) AS sim FROM pub.chunk c "
                f"WHERE {_SCOPE} AND c.embedding IS NOT NULL AND c.embedding_model = %(emodel)s "
                f"ORDER BY c.embedding <=> %(vec)s::vector LIMIT {PER_CHANNEL}",
                params | {"vec": as_pgvector(query_vector), "emodel": self.model}).fetchall()
            ranked["meaning"] = [r["id"] for r in rows if r["sim"] >= self.min_similarity]
            similarity = {r["id"]: float(r["sim"]) for r in rows}
        if words.strip():
            ranked["words"] = [r["id"] for r in self.conn.execute(
                f"SELECT c.id FROM pub.chunk c, CAST(replace(plainto_tsquery('english', %(words)s)::text, '&', '|') AS tsquery) q "
                f"WHERE {_SCOPE} AND q::text <> '' AND c.tsv @@ q ORDER BY ts_rank_cd(c.tsv, q) DESC LIMIT {PER_CHANNEL}",
                params | {"words": words}).fetchall()]
        allowed = set(ranked.get("code", [])) | set(ranked.get("meaning", []))
        if not allowed:
            return []
        score: dict[int, float] = {}
        found: dict[int, list[str]] = {}
        for channel, ids in ranked.items():
            for rank, cid in enumerate(ids):
                if cid in allowed:
                    score[cid] = score.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
                    found.setdefault(cid, []).append(channel)
        rows = self.conn.execute(
            "SELECT c.id AS chunk_id, c.text AS piece, d.id, d.source, d.external_id, d.source_type, d.title, d.url, "
            "d.component, d.model, d.model_key, d.year_from, d.year_to, d.codes FROM pub.chunk c "
            "JOIN pub.document d ON d.id = c.document_id WHERE c.id = ANY(%s)", (list(score),)).fetchall()
        best: dict[int, PublicHit] = {}
        for r in rows:
            mark = applies(params["model"], vehicle.year, r["model_key"], r["year_from"], r["year_to"])
            if mark is None:
                continue  # the SQL scope already keeps other cars out; this is the same rule, checked again
            hit = PublicHit(r["id"], r["source"], r["external_id"], r["source_type"], r["title"], r["piece"], r["url"],
                            r["component"], r["model"], r["year_from"], r["year_to"], list(r["codes"]), mark,
                            similarity.get(r["chunk_id"]), found[r["chunk_id"]], score[r["chunk_id"]])
            if r["id"] not in best or hit.score > best[r["id"]].score:
                best[r["id"]] = hit
        return sorted(best.values(), key=lambda h: (h.applies != "fits", -h.score))[:limit]

    def known(self, vehicle: Vehicle, codes: list[str], limit: int = 3) -> list[PublicHit]:
        """Records for this car that carry one of the reported codes: recalls first, then investigations, then
        owner reports. No model and no vector: exact, for the short note beside a verified answer."""
        hits = self.search(vehicle, codes, None, "", limit=50)
        return sorted(hits, key=lambda h: (TYPE_ORDER.get(h.source_type, 9), h.applies != "fits", h.external_id))[:limit]

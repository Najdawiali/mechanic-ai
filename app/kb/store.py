"""The knowledge base in PostgreSQL: saving records and looking them up.

Rules this module enforces:

  * Only approved records are served, unless the caller asks for drafts (development, evaluation).
  * A record is used only for its own vehicle and its own fault code. Lookups are exact SQL on
    normalised keys. Nothing here compares codes or vehicles by similarity.
  * An approved record is never changed in place. New content for it is stored as a new draft
    version, and the approved version keeps being served until the new one is approved.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Literal

import psycopg
from psycopg.types.json import Jsonb

from app.config import get_settings
from app.kb import index
from app.kb.embedder import as_pgvector
from app.kb.index import KbRecord
from app.kb.keys import code_stem, norm_key
from app.kb.schemas import Case, ExpertNote, Procedure, Review, Vehicle

ExtractedBy = Literal["code", "model", "hand"]
Status = Literal["draft", "approved", "rejected"]
ScopeMatch = Literal["exact", "year_unknown", "make_only", "general"]

APPROVED: tuple[Status, ...] = ("approved",)
APPROVED_AND_DRAFT: tuple[Status, ...] = ("approved", "draft")

_SCOPE_ORDER = {"exact": 0, "year_unknown": 1, "make_only": 2, "general": 3}


@dataclass(frozen=True)
class Saved:
    record_id: int
    version: int
    action: Literal["created", "unchanged", "updated_draft", "new_version"]


@dataclass(frozen=True)
class ProcedureHit:
    record_id: int
    version: int
    status: Status
    procedure: Procedure
    engine_confirmed: bool  # False = the mechanic's engine is not known yet: ask before using it


@dataclass
class Hit:
    """A case or an expert note found for a vehicle."""

    record_id: int
    record_type: str
    record_key: str
    version: int
    status: Status
    scope_match: ScopeMatch  # how closely the record's vehicle fits the mechanic's vehicle
    record: KbRecord
    matched_codes: list[str] = field(default_factory=list)  # as written in the record
    matched_units: list[MatchedUnit] = field(default_factory=list)
    score: float = 0.0
    found_by: tuple[str, ...] = ()  # set by search(): "words", "meaning" or both


@dataclass(frozen=True)
class PinHit:
    """One pin of a component on a wiring diagram, with where its wire goes."""

    record_id: int
    diagram_key: str
    diagram_title: str
    verdict: str  # traced | partial: how far the diagram's reading can be trusted
    component: str
    pin: str
    wire_colour: str | None
    page: str
    fuse: str | None
    routes: list[dict]  # each: {"hops": [...], "text": "...", "complete": bool}
    version: str | None = None  # the drawn version this pin belongs to, when the component is drawn more than once


@dataclass(frozen=True)
class MatchedUnit:
    kind: str
    text: str
    origin: str | None
    rank: float


# The vehicle filter for cases and expert notes. A scope row fits when every part it states
# agrees with the mechanic's vehicle. A record that is tied to no make and only to a powertrain
# (a note about high-voltage batteries) needs the car's powertrain to be known and to match: it
# is not offered for a car that may be a petrol car. Parts the record does not state are reported in
# scope_match, so the reply can say "the report did not record the year".
_SCOPE_FITS = """
    (s.make_key IS NULL OR s.make_key = %(make)s::text)
    AND (s.model_key IS NULL OR s.model_key = %(model)s::text)
    AND (%(year)s::int IS NULL
         OR ((s.year_from IS NULL OR s.year_from <= %(year)s::int)
             AND (s.year_to IS NULL OR s.year_to >= %(year)s::int)))
    AND (s.powertrain IS NULL OR s.powertrain = ANY(%(powertrains)s::text[])
         OR (%(powertrains)s::text[] IS NULL AND s.make_key IS NOT NULL))
"""
_SCOPE_MATCH = """
    CASE WHEN s.make_key IS NULL THEN 'general'
         WHEN s.model_key IS NULL THEN 'make_only'
         WHEN %(year)s::int IS NULL OR (s.year_from IS NULL AND s.year_to IS NULL) THEN 'year_unknown'
         ELSE 'exact' END
"""
# The version that is served: the newest one among the statuses the caller allows.
_IS_SERVED = """
    r.status = ANY(%(statuses)s) AND r.withdrawn_at IS NULL
    AND r.version = (SELECT max(v.version) FROM kb.record v
                     WHERE v.record_type = r.record_type AND v.record_key = r.record_key
                       AND v.status = ANY(%(statuses)s) AND v.withdrawn_at IS NULL)
"""


# The platform offers three vehicle types (electric, hybrid, petrol), so a plug-in hybrid arrives
# as "hybrid". Records tell the two apart; lookups treat them as one family.
_HYBRID_FAMILY = ["Hybrid", "PHEV"]


def _vehicle_params(vehicle: Vehicle) -> dict:
    powertrain = vehicle.powertrain.value if vehicle.powertrain else None
    return {
        "make": norm_key(vehicle.make),
        "model": norm_key(vehicle.model),
        "year": vehicle.year,
        "engine": norm_key(vehicle.engine_code),
        "powertrains": None if powertrain is None else (_HYBRID_FAMILY if powertrain in _HYBRID_FAMILY else [powertrain]),
    }


def _load(row: dict) -> KbRecord:
    review = Review(
        status=row["status"], reviewer=row["reviewer"], reviewed_on=row["reviewed_on"], open_questions=row["open_questions"]
    )
    return index.RECORD_TYPES[row["record_type"]](**row["body"], review=review)


class KnowledgeStore:
    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    # -- writing ---------------------------------------------------------------
    def save(self, record: KbRecord, *, extracted_by: ExtractedBy, source_file_id: int | None = None) -> Saved:
        """Store a record. Saving the same content twice changes nothing."""
        rtype, rkey = index.record_type(record), index.record_key(record)
        body = index.body_of(record)
        digest = index.body_hash(body)
        with self.conn.transaction():
            latest = self.conn.execute(
                "SELECT id, version, status, body_hash FROM kb.record WHERE record_type = %s AND record_key = %s "
                "ORDER BY version DESC LIMIT 1 FOR UPDATE",
                (rtype, rkey),
            ).fetchone()
            if latest and latest["body_hash"] == digest:
                return Saved(latest["id"], latest["version"], "unchanged")
            if latest and latest["status"] != "approved":
                # A draft or a rejected version is replaced in place: nobody is being served it.
                self.conn.execute("DELETE FROM kb.record WHERE id = %s", (latest["id"],))
                version, action = latest["version"], "updated_draft"
            elif latest:
                version, action = latest["version"] + 1, "new_version"
            else:
                version, action = 1, "created"
            review = record.review
            record_id = self.conn.execute(
                """
                INSERT INTO kb.record (record_type, record_key, version, status, reviewer, reviewed_on, open_questions,
                                       source_kind, licensed, extracted_by, body, body_hash, source_file_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
                """,
                (rtype, rkey, version, review.status, review.reviewer, review.reviewed_on, Jsonb(review.open_questions),
                 record.source.kind, record.source.licensed, extracted_by, Jsonb(body), digest, source_file_id),
            ).fetchone()["id"]
            self._write_lookup_rows(record_id, record)
        return Saved(record_id, version, action)

    def _write_lookup_rows(self, record_id: int, record: KbRecord) -> None:
        with self.conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO kb.record_scope (record_id, make_key, model_key, engine_key, year_from, year_to, powertrain) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                [(record_id, s.make_key, s.model_key, s.engine_key, s.year_from, s.year_to, s.powertrain)
                 for s in index.scope_rows(record)],
            )
            cur.executemany(
                "INSERT INTO kb.record_code (record_id, code_raw, code_key, stem_key, module) VALUES (%s, %s, %s, %s, %s)",
                [(record_id, c.code_raw, c.code_key, c.stem_key, c.module) for c in index.code_rows(record)],
            )
            cur.executemany(
                "INSERT INTO kb.record_term (record_id, kind, term_key, term_text) VALUES (%s, %s, %s, %s)",
                [(record_id, t.kind, t.term_key, t.term_text) for t in index.term_rows(record)],
            )
            cur.executemany(
                "INSERT INTO kb.search_unit (record_id, kind, ordinal, text, origin) VALUES (%s, %s, %s, %s, %s)",
                [(record_id, u.kind, u.ordinal, u.text, u.origin) for u in index.unit_rows(record)],
            )
            cur.executemany(
                "INSERT INTO kb.diagram_pin (record_id, component_key, component, pin_key, pin, wire_colour, page, fuse, routes, version) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [(record_id, r.component_key, r.component, r.pin_key, r.pin, r.wire_colour, r.page, r.fuse, Jsonb(r.routes), r.version)
                 for r in index.pin_rows(record)],
            )

    def set_review(self, record_id: int, status: Status, *, reviewer: str | None, on: dt.date | None = None) -> None:
        """Approve or reject one version. Approval must name the reviewer."""
        if status == "approved" and not reviewer:
            raise ValueError("an approved record must name its reviewer")
        with self.conn.transaction():
            done = self.conn.execute(
                "UPDATE kb.record SET status = %s, reviewer = %s, reviewed_on = %s, updated_at = now() WHERE id = %s",
                (status, reviewer, on or (dt.date.today() if status != "draft" else None), record_id),
            )
            if done.rowcount != 1:
                raise LookupError(f"no record with id {record_id}")

    # -- exact lookups -----------------------------------------------------------
    def get(self, record_id: int) -> KbRecord:
        row = self.conn.execute("SELECT * FROM kb.record WHERE id = %s", (record_id,)).fetchone()
        if row is None:
            raise LookupError(f"no record with id {record_id}")
        return _load(row)

    def get_procedure(self, vehicle: Vehicle, code: str, *, statuses: tuple[Status, ...] = APPROVED) -> ProcedureHit | None:
        """The procedure for exactly this make, model, year and code, or None.

        The year is required: a procedure is a specification, and a specification for another
        model year is not this car's. When the record names an engine, the mechanic's engine
        must be the same; if the mechanic's engine is not known yet the hit comes back with
        engine_confirmed=False so the assistant asks before using it.
        """
        if vehicle.year is None:
            return None
        params = _vehicle_params(vehicle) | {"stem": code_stem(code), "statuses": list(statuses)}
        row = self.conn.execute(
            f"""
            SELECT r.*, COALESCE(s.engine_key IS NULL OR s.engine_key = %(engine)s::text, false) AS engine_confirmed
            FROM kb.record r
            JOIN kb.record_scope s ON s.record_id = r.id
            JOIN kb.record_code c ON c.record_id = r.id
            WHERE r.record_type = 'procedure' AND c.stem_key = %(stem)s
              AND s.make_key = %(make)s::text AND s.model_key = %(model)s::text
              AND s.year_from = %(year)s::int AND s.year_to = %(year)s::int
              AND (s.engine_key IS NULL OR %(engine)s::text IS NULL OR s.engine_key = %(engine)s::text)
              AND {_IS_SERVED}
            ORDER BY engine_confirmed DESC, r.id
            LIMIT 1
            """,
            params,
        ).fetchone()
        if row is None:
            return None
        return ProcedureHit(row["id"], row["version"], row["status"], _load(row), row["engine_confirmed"])

    def find_by_codes(
        self,
        vehicle: Vehicle,
        codes: list[str],
        *,
        record_types: tuple[str, ...] = ("case", "expert_note"),
        statuses: tuple[Status, ...] = APPROVED,
    ) -> list[Hit]:
        """Cases and expert notes that carry at least one of these codes and fit this vehicle.

        Best first: more of the codes matched, then the closer vehicle fit.
        """
        stems = sorted({code_stem(c) for c in codes})
        if not stems:
            return []
        params = _vehicle_params(vehicle) | {"stems": stems, "types": list(record_types), "statuses": list(statuses)}
        rows = self.conn.execute(
            f"""
            SELECT r.*, {_SCOPE_MATCH} AS scope_match, c.code_raw
            FROM kb.record r
            JOIN kb.record_scope s ON s.record_id = r.id
            JOIN kb.record_code c ON c.record_id = r.id
            WHERE r.record_type = ANY(%(types)s) AND c.stem_key = ANY(%(stems)s)
              AND {_SCOPE_FITS} AND {_IS_SERVED}
            """,
            params,
        ).fetchall()
        hits = self._collect(rows)
        for row in rows:
            hit = hits[row["id"]]
            if row["code_raw"] not in hit.matched_codes:
                hit.matched_codes.append(row["code_raw"])
        for hit in hits.values():
            hit.score = float(len(hit.matched_codes))
        return sorted(hits.values(), key=lambda h: (-h.score, _SCOPE_ORDER[h.scope_match], h.record_id))

    def get_pins(
        self, vehicle: Vehicle, component: str, pin: str | None = None, *, statuses: tuple[Status, ...] = APPROVED
    ) -> list[PinHit]:
        """The pins of a component on this exact car, from its wiring diagrams, with their routes.

        `component` is the name as printed on the diagram (find it with search_text over
        record_types=("diagram",)). Like a procedure, a diagram is a specification: the make,
        model and year must be the same, and the engine too when the diagram names one.
        """
        if vehicle.year is None:
            return []
        params = _vehicle_params(vehicle) | {
            "component": norm_key(component), "pin": norm_key(pin) if pin else None, "statuses": list(statuses),
        }
        rows = self.conn.execute(
            f"""
            SELECT p.*, r.record_key, r.body->>'title' AS title, r.body->>'verdict' AS verdict
            FROM kb.diagram_pin p
            JOIN kb.record r ON r.id = p.record_id
            WHERE r.record_type = 'diagram' AND p.component_key = %(component)s
              AND (%(pin)s::text IS NULL OR p.pin_key = %(pin)s::text)
              AND EXISTS (
                  SELECT 1 FROM kb.record_scope s
                  WHERE s.record_id = r.id AND s.make_key = %(make)s::text AND s.model_key = %(model)s::text
                    AND s.year_from = %(year)s::int AND s.year_to = %(year)s::int
                    AND (s.engine_key IS NULL OR %(engine)s::text IS NULL OR s.engine_key = %(engine)s::text))
              AND {_IS_SERVED}
            ORDER BY p.record_id, length(p.pin_key), p.pin_key, p.id
            """,
            params,
        ).fetchall()
        return [PinHit(row["record_id"], row["record_key"], row["title"], row["verdict"], row["component"], row["pin"],
                       row["wire_colour"], row["page"], row["fuse"], row["routes"], row["version"]) for row in rows]

    # -- search by words ---------------------------------------------------------
    def search_text(
        self,
        vehicle: Vehicle,
        query: str,
        *,
        record_types: tuple[str, ...] = ("case", "expert_note"),
        statuses: tuple[Status, ...] = APPROVED,
        include_ai_added: bool = False,
        limit: int = 5,
        min_shared_words: int = 1,
    ) -> list[Hit]:
        """Records that fit this vehicle and have a piece of text sharing words with the query.

        `min_shared_words` is how many different words of the query one piece must contain. One
        shared word ("module", "stays") is usually chance; `search` asks for two.

        Search runs on the small pieces (one symptom, one finding, one claim) and returns the
        whole record each piece belongs to, with the pieces that matched. Text the chat
        assistant added and the expert has not confirmed is left out unless asked for.

        This is the keyword half of the search. Search by meaning (vectors) is added to the
        same table once the embedding model is chosen.
        """
        if not query.strip():
            return []
        params = _vehicle_params(vehicle) | {
            "q": query, "types": list(record_types), "statuses": list(statuses), "ai": include_ai_added,
            "shared": min_shared_words,
        }
        rows = self.conn.execute(
            f"""
            WITH q AS (
                -- Any of the words may match: a mechanic's sentence never matches a note word for word.
                SELECT NULLIF(replace(plainto_tsquery('english', %(q)s)::text, '&', '|'), '')::tsquery AS tsq,
                       tsvector_to_array(to_tsvector('english', %(q)s)) AS words
            )
            SELECT r.*, {_SCOPE_MATCH} AS scope_match,
                   u.kind AS unit_kind, u.text AS unit_text, u.origin AS unit_origin,
                   ts_rank_cd(u.tsv, q.tsq) AS unit_rank
            FROM q
            JOIN kb.search_unit u ON u.tsv @@ q.tsq
            JOIN kb.record r ON r.id = u.record_id
            JOIN kb.record_scope s ON s.record_id = r.id
            WHERE r.record_type = ANY(%(types)s)
              AND (%(ai)s OR u.origin IS DISTINCT FROM 'ai_added')
              AND (SELECT count(*) FROM unnest(tsvector_to_array(u.tsv)) AS word WHERE word = ANY(q.words))
                  >= LEAST(%(shared)s::int, cardinality(q.words))
              AND {_SCOPE_FITS} AND {_IS_SERVED}
            """,
            params,
        ).fetchall()
        hits = self._collect(rows)
        for row in rows:
            hit = hits[row["id"]]
            unit = MatchedUnit(row["unit_kind"], row["unit_text"], row["unit_origin"], float(row["unit_rank"]))
            if unit not in hit.matched_units:
                hit.matched_units.append(unit)
        for hit in hits.values():
            hit.matched_units.sort(key=lambda u: -u.rank)
            hit.score = sum(u.rank for u in hit.matched_units[:3])  # a record is as good as its best pieces
        ranked = sorted(hits.values(), key=lambda h: (-h.score, _SCOPE_ORDER[h.scope_match], h.record_id))
        return ranked[:limit]

    def search_meaning(
        self,
        vehicle: Vehicle,
        query_vector: list[float],
        *,
        record_types: tuple[str, ...] = ("case", "expert_note"),
        statuses: tuple[Status, ...] = APPROVED,
        include_ai_added: bool = False,
        limit: int = 5,
        min_similarity: float = 0.0,
    ) -> list[Hit]:
        """Records that fit this vehicle and have a piece of text close in meaning to the query.

        `query_vector` comes from `gateway.embed([text], query=True)`. The nearest pieces always
        exist, however far away they are, so each matched piece carries its similarity (1 = the
        same meaning, 0 = unrelated) and the caller decides what is close enough.
        """
        params = _vehicle_params(vehicle) | {
            "vec": as_pgvector(query_vector), "types": list(record_types), "statuses": list(statuses),
            "ai": include_ai_added, "k": limit * 8,
        }
        rows = self.conn.execute(
            f"""
            SELECT r.*, {_SCOPE_MATCH} AS scope_match,
                   u.kind AS unit_kind, u.text AS unit_text, u.origin AS unit_origin,
                   1 - (u.embedding <=> %(vec)s::vector) AS unit_rank
            FROM kb.search_unit u
            JOIN kb.record r ON r.id = u.record_id
            JOIN kb.record_scope s ON s.record_id = r.id
            WHERE u.embedding IS NOT NULL AND r.record_type = ANY(%(types)s)
              AND (%(ai)s OR u.origin IS DISTINCT FROM 'ai_added')
              AND {_SCOPE_FITS} AND {_IS_SERVED}
            ORDER BY u.embedding <=> %(vec)s::vector
            LIMIT %(k)s
            """,
            params,
        ).fetchall()
        rows = [row for row in rows if row["unit_rank"] >= min_similarity]
        hits = self._collect(rows)
        for row in rows:
            unit = MatchedUnit(row["unit_kind"], row["unit_text"], row["unit_origin"], float(row["unit_rank"]))
            if unit not in hits[row["id"]].matched_units:
                hits[row["id"]].matched_units.append(unit)
        for hit in hits.values():
            hit.matched_units.sort(key=lambda u: -u.rank)
            hit.score = hit.matched_units[0].rank  # a record is as close as its closest piece
        return sorted(hits.values(), key=lambda h: (-h.score, _SCOPE_ORDER[h.scope_match], h.record_id))[:limit]

    def search(
        self,
        vehicle: Vehicle,
        query: str,
        query_vector: list[float] | None = None,
        *,
        record_types: tuple[str, ...] = ("case", "expert_note"),
        statuses: tuple[Status, ...] = APPROVED,
        limit: int = 5,
        min_similarity: float | None = None,
    ) -> list[Hit]:
        """Keyword search and search by meaning together.

        Each finds its own list; a record's place in the combined list comes from its place in
        both (reciprocal rank fusion), so a record found both ways comes first and neither kind
        of score has to be compared with the other. Without a vector this is keyword search alone.
        """
        by_words = self.search_text(vehicle, query, record_types=record_types, statuses=statuses, limit=limit * 2,
                                    min_shared_words=2)
        floor = get_settings().search_min_similarity if min_similarity is None else min_similarity
        by_meaning = [] if query_vector is None else self.search_meaning(
            vehicle, query_vector, record_types=record_types, statuses=statuses, limit=limit * 2, min_similarity=floor)
        merged: dict[int, Hit] = {}
        for found, how in ((by_words, "words"), (by_meaning, "meaning")):
            for place, hit in enumerate(found, start=1):
                kept = merged.setdefault(hit.record_id, hit)
                if kept is hit:
                    hit.score = 0.0
                else:  # the same piece found both ways is shown once, with its similarity
                    by_text = {u.text: u for u in kept.matched_units} | {u.text: u for u in hit.matched_units}
                    kept.matched_units = sorted(by_text.values(), key=lambda u: -u.rank)
                kept.score += 1.0 / (60 + place)
                kept.found_by = tuple(dict.fromkeys((*kept.found_by, how)))
        return sorted(merged.values(), key=lambda h: (-h.score, _SCOPE_ORDER[h.scope_match], h.record_id))[:limit]

    @staticmethod
    def _collect(rows: list[dict]) -> dict[int, Hit]:
        """One Hit per record. A record with several scope rows keeps its closest fit."""
        hits: dict[int, Hit] = {}
        for row in rows:
            hit = hits.get(row["id"])
            if hit is None:
                hits[row["id"]] = Hit(
                    row["id"], row["record_type"], row["record_key"], row["version"], row["status"],
                    row["scope_match"], _load(row),
                )
            elif _SCOPE_ORDER[row["scope_match"]] < _SCOPE_ORDER[hit.scope_match]:
                hit.scope_match = row["scope_match"]
        return hits

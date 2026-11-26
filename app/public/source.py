"""The engine's view of the public records: items it can show, each labelled with what kind of record it is.

    lookup()  no verified data: the records that fit this car and this fault (code or meaning), for the model to
              explain, each one cited (tier "public", or together with web pages)
    known()   a verified answer: up to N records for this car carrying the same code, listed in code as a short
              note under the answer (no model call; the verified steps and values are not touched)
"""

from __future__ import annotations

import time
from collections.abc import Callable

import psycopg

from app.engine.external import ExternalItem, ExternalLookup
from app.engine.model import TraceEntry
from app.kb.schemas import Vehicle
from app.public.store import PublicHit, PublicStore

QUERY_INSTRUCTION = ("Given a mechanic's description of a vehicle fault, retrieve NHTSA recall, defect investigation "
                     "or owner-complaint records about the same fault")
LABEL = {"official_recall": "NHTSA safety recall (US official record)",
         "official_investigation": "NHTSA defect investigation (US official record)",
         "unverified_report": "NHTSA owner complaint (unverified owner report)"}
TEXT_LIMIT = 1200


def item_of(hit: PublicHit) -> ExternalItem:
    return ExternalItem(
        id=f"pub:{hit.source}:{hit.external_id}:{hit.document_id}", provider="nhtsa", title=hit.title,
        text=hit.text[:TEXT_LIMIT], url=hit.url, kind=hit.source,
        extra={"applies": hit.applies, "source_type": hit.source_type, "label": LABEL[hit.source_type],
               "external_id": hit.external_id, "domain": "NHTSA (US)", "found_by": hit.found_by,
               "similarity": round(hit.similarity, 3) if hit.similarity is not None else None})


class PublicRecords:
    name = "nhtsa"

    def __init__(self, store: PublicStore, embed: Callable[[str], list[float] | None], *, max_items: int = 4,
                 known_max: int = 3):
        self.store, self.embed, self.max_items, self.known_max = store, embed, max_items, known_max

    def lookup(self, vehicle: Vehicle, codes: list[str], question: str) -> ExternalLookup:
        start = time.perf_counter()
        car = " ".join(str(x) for x in (vehicle.year, vehicle.make, vehicle.model) if x)
        text = " ".join(x for x in (car, " ".join(codes), question) if x).strip()
        try:
            vector = self.embed(text) if question.strip() or codes else None
            hits = self.store.search(vehicle, codes, vector, question, limit=self.max_items)
            error = None
        except Exception as exc:  # noqa: BLE001 - the public records are an extra: a failure falls back, never breaks a turn
            self.store.conn.rollback()
            hits, error = [], f"{type(exc).__name__}: {str(exc)[:200]}"
        entry = TraceEntry("public_search", "nhtsa", round(time.perf_counter() - start, 3),
                           request={"car": car, "codes": codes}, error=error,
                           output=", ".join(f"{h.source}:{h.external_id}({h.applies})" for h in hits))
        return ExternalLookup(items=[item_of(h) for h in hits], calls=[entry])

    def known(self, vehicle: Vehicle, codes: list[str]) -> list[ExternalItem]:
        if not codes or self.known_max <= 0:
            return []
        try:
            return [item_of(h) for h in self.store.known(vehicle, codes, limit=self.known_max)]
        except Exception:  # noqa: BLE001 - never let the note break a verified answer
            self.store.conn.rollback()
            return []


def from_settings(settings, conn: psycopg.Connection, gateway=None) -> PublicRecords | None:
    """None when PUBLIC_RECORDS is off. `gateway` embeds queries (None = meaning search off, code search only)."""
    name = (settings.public_records or "off").strip().lower()
    if name == "off":
        return None
    if name != "nhtsa":
        raise ValueError(f"public records {name!r} are not available (use off or nhtsa)")
    model = settings.public_model

    def embed(text: str) -> list[float] | None:
        if gateway is None:
            return None
        try:
            return gateway.embed([text], query=True, model=model, instruction=QUERY_INSTRUCTION)[0]
        except Exception:  # noqa: BLE001 - code search still works without the vector
            return None

    store = PublicStore(conn, model=model, types=settings.public_types, min_similarity=settings.public_min_similarity)
    return PublicRecords(store, embed, max_items=settings.public_max_items, known_max=settings.public_known_max)

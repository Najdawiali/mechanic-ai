"""Public records (schema pub): keys, import from a venorexai-shaped database, car-scoped search, the engine adapter."""

import math

import pytest

from app.kb.schemas import Vehicle
from app.public.importer import document_row, import_all
from app.public.keys import applies, make_key, model_applies, model_key, year_applies
from app.public.source import PublicRecords
from app.public.store import PublicStore

MODEL = "test/embed"


# -- keys (no database) --------------------------------------------------------------------------------------
def test_make_groups_follow_the_snapshot():
    assert make_key("Acura") == "HONDA" and make_key("lexus") == "TOYOTA" and make_key("Dodge") == "JEEP"
    assert make_key("Changan") == "CHANGAN"  # not in the US data: its own group, so it finds nothing


def test_model_keys_and_matching():
    assert model_key("ID.4") == "ID 4" and model_key("CS75 Plus (2022)") == "CS75 PLUS"
    assert model_applies("CAMRY", "CAMRY") == "fits"
    assert model_applies("CAMRY", "CAMRY HYBRID") == "check"
    assert model_applies("MODEL 3", "MODEL Y") is None
    assert model_applies("RIO", "RIO5") is None  # a different word is a different car


def test_years():
    assert year_applies(2020, 2018, 2021) == "fits"
    assert year_applies(2017, 2018, 2021) is None
    assert year_applies(None, 2018, 2021) == "check"
    assert applies("CAMRY", 2020, "CAMRY HYBRID", 2018, 2021) == "check"


def test_document_row_skips_other_kinds_and_maps_keys():
    base = {"id": 1, "source": "nhtsa_recall", "external_id": "20V001", "source_type": "official_recall", "make": "Honda",
            "model": "Integra", "year_from": 2023, "year_to": 2025, "dtcs": ["p0010"], "component": "ENGINE",
            "title": "t", "text": "x", "url": None, "dataset_version": "2026-11-19", "content_hash": "h", "meta": {}}
    row = document_row(base)
    assert row["make_key"] == "HONDA" and row["model_key"] == "INTEGRA" and row["codes"] == ["P0010"]
    assert document_row(base | {"source_type": "verified_repair"}) is None
    assert document_row(base | {"model": None}) is None


# -- database ------------------------------------------------------------------------------------------------
def _vec(*hot: int) -> list[float]:
    v = [0.0] * 1024
    for i in hot:
        v[i] = 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _pg(v):
    return "[" + ",".join(f"{x:.6g}" for x in v) + "]"


DOCS = [  # id, source, external_id, source_type, make, model, years, codes, text, vector
    (1, "nhtsa_recall", "23V100", "official_recall", "Honda", "Integra", (2023, 2025), ["P0010"],
     "VTC oil control solenoid may fail and set P0010", _vec(0)),
    (2, "nhtsa_complaint", "11500001", "unverified_report", "Honda", "Integra", (2024, 2024), [],
     "Engine rattles at cold start, timing noise", _vec(1)),
    (3, "nhtsa_recall", "19V200", "official_recall", "Honda", "Integra", (2018, 2019), ["P0010"],
     "Older Integra recall for the same code", _vec(0)),
    (4, "nhtsa_recall", "22V300", "official_recall", "Honda", "Civic", (2023, 2025), ["P0010"],
     "Civic recall with P0010", _vec(0)),
    (5, "nhtsa_investigation", "PE24001", "official_investigation", "Honda", "Integra Type S", (2023, 2025), [],
     "Investigation of loss of power, engine stalls", _vec(2)),
]


@pytest.fixture
def source_url(database_url, conn):
    """His two knowledge-base tables, in the shape of his dump, inside the test database (schema rag)."""
    conn.execute("DROP SCHEMA IF EXISTS rag CASCADE; DROP SCHEMA IF EXISTS pub_reset CASCADE")
    conn.execute("TRUNCATE pub.document RESTART IDENTITY CASCADE")
    conn.execute("""CREATE SCHEMA rag;
        CREATE TABLE rag.documents (id bigint PRIMARY KEY, source text, external_id text, source_type text,
            verification_status text, make text, model text, model_norm text, year_from int, year_to int,
            markets jsonb, dtcs jsonb, component text, title text, text text, url text, dataset_version text,
            content_hash text, meta jsonb, ingested_at timestamptz);
        CREATE TABLE rag.chunks (id bigint PRIMARY KEY, document_id bigint, chunk_index int, text text, make text,
            model_norm text, year_from int, year_to int, dtcs varchar(5)[], source_type text, embedding vector(1024),
            embedding_model text)""")
    from psycopg.types.json import Jsonb
    for i, (did, src, ext, st, make, model, (yf, yt), codes, text, vec) in enumerate(DOCS, 1):
        conn.execute("INSERT INTO rag.documents VALUES (%s,%s,%s,%s,'x',%s,%s,%s,%s,%s,%s,%s,'C',%s,%s,NULL,'2026-11-19',%s,%s,now())",
                     (did, src, ext, st, make, model, model.upper(), yf, yt, Jsonb(["US"]), Jsonb(codes), f"T{did}", text,
                      f"h{did}", Jsonb({})))
        conn.execute("INSERT INTO rag.chunks VALUES (%s,%s,0,%s,%s,%s,%s,%s,%s,%s,%s::vector,'his/8B')",
                     (i, did, text, make, model.upper(), yf, yt, codes, st, _pg(vec)))
    conn.commit()
    yield database_url
    conn.execute("DROP SCHEMA rag CASCADE")
    conn.commit()


def test_import_is_idempotent_and_vectors_optional(source_url, conn):
    first = import_all(source_url, conn, batch=2, log=lambda *a, **k: None)
    assert first == {"documents": 5, "skipped_documents": 0, "chunks": 5, "with_vector": 0}
    again = import_all(source_url, conn, batch=2, log=lambda *a, **k: None)
    assert again["documents"] == 5
    assert conn.execute("SELECT count(*) AS n FROM pub.document").fetchone()["n"] == 5
    assert conn.execute("SELECT count(*) AS n FROM pub.chunk").fetchone()["n"] == 5
    assert conn.execute("SELECT count(*) AS n FROM pub.chunk WHERE embedding IS NOT NULL").fetchone()["n"] == 0
    conn.execute("TRUNCATE pub.document RESTART IDENTITY CASCADE")
    kept = import_all(source_url, conn, keep_vectors=True, log=lambda *a, **k: None)
    assert kept["with_vector"] == 5
    assert conn.execute("SELECT DISTINCT embedding_model AS m FROM pub.chunk").fetchone()["m"] == "his/8B"


@pytest.fixture
def store(source_url, conn):
    import_all(source_url, conn, log=lambda *a, **k: None)
    conn.execute("UPDATE pub.chunk c SET embedding = r.embedding, embedding_model = %s FROM rag.chunks r "
                 "JOIN pub.document d ON (d.meta->>'venorex_document_id')::bigint = r.document_id WHERE d.id = c.document_id",
                 (MODEL,))
    conn.commit()
    return PublicStore(conn, model=MODEL, types=("official_recall", "official_investigation", "unverified_report"))


INTEGRA = Vehicle(make="Acura", model="Integra", year=2024)


def test_code_search_stays_on_the_car(store):
    hits = store.search(INTEGRA, ["P0010"], None)
    assert [h.external_id for h in hits] == ["23V100"]  # not the 2018-2019 Integra, not the Civic
    assert hits[0].applies == "fits" and hits[0].found_by == ["code"]


def test_meaning_search_needs_similarity_and_words_alone_bring_nothing(store):
    hits = store.search(INTEGRA, [], _vec(1), "engine rattle cold start")
    assert [h.external_id for h in hits] == ["11500001"] and "meaning" in hits[0].found_by
    assert store.search(INTEGRA, [], None, "engine rattle cold start") == []  # words only: nothing
    assert store.search(INTEGRA, [], _vec(7), "engine") == []  # nothing close in meaning


def test_longer_model_name_is_marked_to_check(store):
    hits = store.search(INTEGRA, [], _vec(2), "loss of power")
    assert [(h.external_id, h.applies) for h in hits] == [("PE24001", "check")]


def test_unknown_year_is_check_and_types_filter(store, conn):
    hits = store.search(Vehicle(make="Acura", model="Integra"), ["P0010"], None)
    assert {h.external_id for h in hits} == {"23V100", "19V200"} and {h.applies for h in hits} == {"check"}
    no_complaints = PublicStore(conn, model=MODEL, types=("official_recall",))
    assert no_complaints.search(INTEGRA, [], _vec(1), "rattle") == []


def test_other_embedding_model_is_never_compared(store, conn):
    other = PublicStore(conn, model="another/model", types=store.types)
    assert other.search(INTEGRA, [], _vec(1), "rattle") == []


def test_adapter_items_and_known_note(store):
    records = PublicRecords(store, lambda text: _vec(1), max_items=4, known_max=3)
    found = records.lookup(INTEGRA, [], "engine rattles at cold start")
    assert [i.extra["label"] for i in found.items] == ["NHTSA owner complaint (unverified owner report)"]
    assert found.calls[0].purpose == "public_search" and found.calls[0].error is None
    known = records.known(INTEGRA, ["P0010"])
    assert [i.extra["external_id"] for i in known] == ["23V100"]
    assert records.known(INTEGRA, []) == []

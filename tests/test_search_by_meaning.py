"""Search by meaning, with a stand-in embedding model. The store tests need a database (see conftest.py)."""

from __future__ import annotations

import json
import math
import zlib

import httpx
import pytest

from app.config import Settings, get_settings
from app.ingest import jobs
from app.ingest.extractors import Extracted
from app.ingest.worker import Worker
from app.kb.embedder import embed_missing
from app.kb.schemas import Case, ExpertNote, Powertrain, Vehicle
from app.kb.store import KnowledgeStore
from app.llm.gateway import LLMGateway

SAME = {"aircon": "ac", "a/c": "ac", "cuts": "stops", "quits": "stops", "sometimes": "intermittently"}
TESLA = Vehicle(make="Tesla", model="Model S", year=2022, powertrain=Powertrain.EV)


class FakeEmbedder:
    """Words that mean the same get the same direction, so meaning can be tested without a real model."""

    def __init__(self, fail: bool = False):
        self.fail, self.calls = fail, 0

    def embed(self, texts, *, query=False):
        self.calls += 1
        if self.fail:
            raise TimeoutError("embedding model not reachable")
        out = []
        for text in texts:
            vector = [0.0] * 1024
            for word in text.lower().replace(".", " ").replace(",", " ").split():
                word = SAME.get(word, word)
                vector[zlib.crc32(word.encode()) % 1024] += 1.0
            norm = math.sqrt(sum(v * v for v in vector)) or 1.0
            out.append([v / norm for v in vector])
        return out


def case(case_id: str, symptom: str, *, make="Tesla", model="Model S", year=2022) -> Case:
    return Case(case_id=case_id, make=make, models=[model], year_from=year, year_to=year, symptoms=[symptom],
                diagnosis="Written for the test.", outcome="confirmed_fixed", source={"kind": "client_case_file", "file": f"{case_id}.txt"})


@pytest.fixture
def store(auto_conn) -> KnowledgeStore:
    return KnowledgeStore(auto_conn)


def add(store: KnowledgeStore, record, approved: bool = True) -> int:
    record_id = store.save(record, extracted_by="model").record_id
    if approved:
        store.set_review(record_id, "approved", reviewer="test reviewer")
    return record_id


def vector(text: str) -> list[float]:
    return FakeEmbedder().embed([text], query=True)[0]


# -- making the vectors -------------------------------------------------------------------
def test_units_get_their_vectors_once_and_again_when_the_model_changes(store, monkeypatch):
    add(store, case("case:test:ac", "A/C stops intermittently while driving"))
    embedder = FakeEmbedder()
    count = store.conn.execute("SELECT count(*) AS n FROM kb.search_unit").fetchone()["n"]
    assert embed_missing(store.conn, embedder) == count and embed_missing(store.conn, embedder) == 0
    row = store.conn.execute("SELECT embedding_model, vector_dims(embedding) AS d FROM kb.search_unit LIMIT 1").fetchone()
    assert row == {"embedding_model": get_settings().llm_embedding_model, "d": 1024}
    monkeypatch.setattr(get_settings(), "llm_embedding_model", "another/model")
    assert embed_missing(store.conn, embedder) == count


# -- searching ---------------------------------------------------------------------------
@pytest.fixture
def filled(store) -> KnowledgeStore:
    add(store, case("case:test:ac", "A/C stops intermittently while driving"))
    add(store, case("case:test:window", "Rear window does not move after battery replacement"))
    add(store, case("case:test:ac-other-car", "A/C stops intermittently while driving", make="Ford", model="Edge", year=2014))
    add(store, case("case:test:ac-draft", "A/C stops intermittently on the highway"), approved=False)
    embed_missing(store.conn, FakeEmbedder())
    return store


def test_other_words_with_the_same_meaning_find_the_case(filled):
    query = "aircon quits sometimes"
    assert filled.search_text(TESLA, query) == []  # not one word in common
    hits = filled.search_meaning(TESLA, vector(query))
    assert hits[0].record_key == "case:test:ac" and hits[0].matched_units[0].text == "A/C stops intermittently while driving"
    assert hits[0].score > 0.5 and hits[0].score > hits[-1].score


def test_meaning_search_keeps_to_the_car_and_to_approved_records(filled):
    keys = {h.record_key for h in filled.search_meaning(TESLA, vector("aircon quits sometimes"), limit=10)}
    assert "case:test:ac-other-car" not in keys and "case:test:ac-draft" not in keys


def test_far_pieces_can_be_left_out(filled):
    near = filled.search_meaning(TESLA, vector("aircon quits sometimes"), min_similarity=0.5)
    assert [h.record_key for h in near] == ["case:test:ac"]
    assert filled.search_meaning(TESLA, vector("tyre pressure warning lamp"), min_similarity=0.5) == []


def test_text_the_expert_has_not_confirmed_is_not_found_by_meaning(store):
    add(store, ExpertNote(note_id="note:test:ac", title="Note", applies_to={"makes": ["Tesla"]}, safety={"level": "normal"},
                          claims=[{"text": "aircon quits sometimes when the flap sticks", "origin": "ai_added"}],
                          source={"kind": "expert_transcript", "file": "t.txt"}))
    embed_missing(store.conn, FakeEmbedder())
    found = [u.text for h in store.search_meaning(TESLA, vector("aircon quits sometimes")) for u in h.matched_units]
    assert not any("flap sticks" in text for text in found)


def test_one_shared_word_is_not_a_keyword_match(filled):
    # Real queries: "tyre pressure light stays on" brought back a battery note that shares only "stays".
    add(filled, case("case:test:stays", "The fault stays at the same position after the swap"))
    embed_missing(filled.conn, FakeEmbedder())
    query = "tyre pressure light stays on"
    assert [h.record_key for h in filled.search_text(TESLA, query)] == ["case:test:stays"]  # the raw keyword search still sees it
    assert filled.search_text(TESLA, query, min_shared_words=2) == []
    assert filled.search(TESLA, query, vector(query), min_similarity=0.9) == []
    assert [h.record_key for h in filled.search_text(TESLA, "window", min_shared_words=2)] == ["case:test:window"]  # a one-word query


def test_a_record_found_both_ways_comes_first(filled):
    add(filled, case("case:test:ac-words", "The aircon compressor relay clicks"))  # shares a word, not the meaning
    embed_missing(filled.conn, FakeEmbedder())
    query = "aircon stops intermittently"
    hits = filled.search(TESLA, query, vector(query))
    assert hits[0].record_key == "case:test:ac" and hits[0].found_by == ("words", "meaning")
    assert len({u.text for u in hits[0].matched_units}) == len(hits[0].matched_units)  # each piece shown once
    far = "tyre pressure warning lamp"
    assert filled.search(TESLA, far, vector(far)) == []  # nothing close enough, nothing in common: nothing offered
    assert "case:test:ac-words" not in {h.record_key for h in filled.search(TESLA, query, vector(query), min_similarity=0.9)}  # one shared word only
    assert [h.found_by for h in filled.search(TESLA, query)] == [("words",)] * len(filled.search(TESLA, query))  # no vector: words only


# -- the worker --------------------------------------------------------------------------
@pytest.mark.parametrize("fail", [False, True], ids=["embedding works", "embedding model unreachable"])
def test_new_records_are_embedded_after_the_job_and_a_failure_does_not_fail_it(auto_conn, storage, database_url, fail):
    storage.put("kb/case.txt", b"report")
    done = jobs.register_file(auto_conn, storage, "kb/case.txt", kind="client_case_file")
    embedder = FakeEmbedder(fail=fail)
    worker = Worker(database_url, storage, worker_id="w", embedder=embedder,
                    extractors={"client_case_file": lambda d: [Extracted(case("case:test:ac", "A/C stops intermittently"), "model")]})
    assert worker.run_once() and jobs.job_status(auto_conn, done.job_id)["status"] == "succeeded"
    embedded = auto_conn.execute("SELECT count(*) AS n FROM kb.search_unit WHERE embedding IS NOT NULL").fetchone()["n"]
    assert embedder.calls >= 1 and (embedded == 0 if fail else embedded > 0)


# -- the gateway -------------------------------------------------------------------------
def gateway(size: int, seen: list) -> LLMGateway:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        data = [{"object": "embedding", "index": i, "embedding": [0.1] * size} for i, _ in enumerate(body["input"])]
        return httpx.Response(200, json={"object": "list", "data": data, "model": body["model"], "usage": {"prompt_tokens": 1, "total_tokens": 1}})

    settings = Settings(_env_file=None, deepinfra_api_key="test-key")
    return LLMGateway(settings=settings, http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_a_query_is_sent_with_its_instruction_and_a_stored_text_without():
    seen: list = []
    gw = gateway(1024, seen)
    assert len(gw.embed(["A/C stops intermittently"])[0]) == 1024 and len(gw.embed(["aircon quits"], query=True)) == 1
    assert seen[0]["input"] == ["A/C stops intermittently"] and seen[0]["dimensions"] == 1024
    assert seen[1]["input"][0].startswith("Instruct: ") and seen[1]["input"][0].endswith("Query: aircon quits")
    assert gw.embed([]) == []


def test_vectors_of_the_wrong_size_are_refused():
    with pytest.raises(RuntimeError, match="1024"):
        gateway(2560, []).embed(["text"])

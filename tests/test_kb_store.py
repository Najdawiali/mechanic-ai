"""The knowledge base in PostgreSQL. These tests need a database (see conftest.py)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import psycopg
import pytest

from app.kb.schemas import Case, ExpertNote, Powertrain, Procedure, Vehicle
from app.kb.store import APPROVED_AND_DRAFT, KnowledgeStore

REPO = Path(__file__).resolve().parents[1]


def load(path: str) -> dict:
    return json.loads((REPO / path).read_text(encoding="utf-8"))


def p0010() -> Procedure:
    return Procedure(**load("eval/golden/procedures/acura-integra-2025-l15ca-p0010.json"))


def byd_case() -> Case:
    return Case(**load("seed/cases/byd-qin-plus-dmi-2024-p2b5313-p2b5411.json"))


def note(name: str) -> ExpertNote:
    return ExpertNote(**load(f"seed/expert_notes/{name}.json"))


def a_case(case_id: str, *, make: str, models: list[str], codes: list[str], symptoms: list[str], years=(None, None)) -> Case:
    return Case(
        case_id=case_id, make=make, models=models, year_from=years[0], year_to=years[1],
        fault_codes=[{"code": c} for c in codes], symptoms=symptoms, diagnosis="Written for the test.",
        outcome="confirmed_fixed", source={"kind": "client_case_file", "file": f"{case_id}.txt"},
    )


INTEGRA = Vehicle(make="Acura", model="Integra", year=2025, engine_code="L15CA", powertrain=Powertrain.ICE)
BYD = Vehicle(make="byd", model="qin plus dmi", year=2024, powertrain=Powertrain.HYBRID)  # as a mechanic would type it
BOLT = Vehicle(make="Chevrolet", model="Bolt EV", year=2019, powertrain=Powertrain.EV)


@pytest.fixture
def store(conn) -> KnowledgeStore:
    return KnowledgeStore(conn)


def approve(store: KnowledgeStore, record_id: int) -> None:
    store.set_review(record_id, "approved", reviewer="test reviewer", on=dt.date(2026, 11, 13))


# -- saving and versions -------------------------------------------------------
def test_a_record_comes_back_exactly_as_it_was_saved(store):
    for record in (p0010(), byd_case(), note("hv-battery-cell-deviation")):
        saved = store.save(record, extracted_by="hand")
        assert store.get(saved.record_id) == record


def test_saving_the_same_content_twice_changes_nothing(store):
    first = store.save(p0010(), extracted_by="hand")
    again = store.save(p0010(), extracted_by="hand")
    assert (first.action, again.action) == ("created", "unchanged") and again.record_id == first.record_id
    assert store.conn.execute("SELECT count(*) AS n FROM kb.record").fetchone()["n"] == 1


def test_a_draft_is_replaced_in_place(store):
    store.save(p0010(), extracted_by="hand")
    changed = p0010().model_copy(update={"meaning": "Changed for the test"})
    saved = store.save(changed, extracted_by="hand")
    assert (saved.action, saved.version) == ("updated_draft", 1)
    assert store.conn.execute("SELECT count(*) AS n FROM kb.record").fetchone()["n"] == 1
    assert store.conn.execute("SELECT count(*) AS n FROM kb.record_code").fetchone()["n"] == 1  # old rows went with it


def test_an_approved_record_is_never_changed_in_place(store):
    v1 = store.save(p0010(), extracted_by="hand")
    approve(store, v1.record_id)
    changed = p0010().model_copy(update={"meaning": "A newer extraction"})
    v2 = store.save(changed, extracted_by="code")
    assert (v2.action, v2.version) == ("new_version", 2)

    served = store.get_procedure(INTEGRA, "P0010")
    assert served.version == 1 and served.procedure.meaning == p0010().meaning  # the approved one is still served

    approve(store, v2.record_id)
    assert store.get_procedure(INTEGRA, "P0010").procedure.meaning == "A newer extraction"


def test_approving_the_same_content_again_is_not_a_new_version(store):
    v1 = store.save(p0010(), extracted_by="hand")
    approve(store, v1.record_id)
    assert store.save(p0010(), extracted_by="hand").action == "unchanged"  # re-running ingestion is harmless


def test_approval_must_name_the_reviewer(store):
    saved = store.save(p0010(), extracted_by="hand")
    with pytest.raises(ValueError):
        store.set_review(saved.record_id, "approved", reviewer=None)
    with pytest.raises(psycopg.errors.CheckViolation):  # the database refuses it too
        store.conn.execute("UPDATE kb.record SET status = 'approved', reviewer = NULL WHERE id = %s", (saved.record_id,))
    store.conn.rollback()


# -- only approved records are served --------------------------------------------
def test_a_draft_is_not_served(store):
    saved = store.save(p0010(), extracted_by="hand")
    assert store.get_procedure(INTEGRA, "P0010") is None
    assert store.get_procedure(INTEGRA, "P0010", statuses=APPROVED_AND_DRAFT) is not None
    approve(store, saved.record_id)
    assert store.get_procedure(INTEGRA, "P0010").status == "approved"


def test_a_rejected_newer_version_does_not_hide_the_approved_one(store):
    v1 = store.save(p0010(), extracted_by="hand")
    approve(store, v1.record_id)
    v2 = store.save(p0010().model_copy(update={"meaning": "A wrong extraction"}), extracted_by="model")
    store.set_review(v2.record_id, "rejected", reviewer="test reviewer")
    assert store.get_procedure(INTEGRA, "P0010").version == 1


# -- procedures: this car and this code, nothing else ----------------------------
@pytest.fixture
def with_p0010(store) -> KnowledgeStore:
    approve(store, store.save(p0010(), extracted_by="hand").record_id)
    return store


def test_the_procedure_is_found_for_its_own_car_and_code(with_p0010):
    hit = with_p0010.get_procedure(INTEGRA, " p0010 ")
    assert hit.procedure == p0010().model_copy(update={"review": hit.procedure.review}) and hit.engine_confirmed


@pytest.mark.parametrize("code", ["P0011", "P0341", "P001", "P00100", "P0010A"])
def test_another_code_never_gets_this_procedure(with_p0010, code):
    assert with_p0010.get_procedure(INTEGRA, code) is None


@pytest.mark.parametrize(
    "vehicle",
    [
        INTEGRA.model_copy(update={"year": 2024}),
        INTEGRA.model_copy(update={"year": None}),  # the year is required for a specification
        INTEGRA.model_copy(update={"model": "Integra Type S"}),
        INTEGRA.model_copy(update={"make": "Honda", "model": "Civic"}),
        INTEGRA.model_copy(update={"engine_code": "K20C8"}),
    ],
    ids=["other year", "no year", "other model", "other make", "other engine"],
)
def test_another_car_never_gets_this_procedure(with_p0010, vehicle):
    assert with_p0010.get_procedure(vehicle, "P0010") is None


def test_an_unknown_engine_is_flagged_so_the_assistant_asks(with_p0010):
    hit = with_p0010.get_procedure(INTEGRA.model_copy(update={"engine_code": None}), "P0010")
    assert hit is not None and hit.engine_confirmed is False


# -- cases and expert notes --------------------------------------------------------
@pytest.fixture
def with_seed(store) -> KnowledgeStore:
    records = [byd_case(), note("gm-ev-precharge-time-too-long"), note("hv-battery-cell-deviation"),
               note("hv-battery-temp-sensor-swap-test")]
    for record in records:
        approve(store, store.save(record, extracted_by="model").record_id)
    return store


def test_a_case_is_found_by_its_code_for_its_own_car(with_seed):
    hits = with_seed.find_by_codes(BYD, ["p2b5313", "P2B5411", "P0010"])
    assert [h.record_key for h in hits] == ["case:byd-qin-plus-dmi-2024:p2b5313-p2b5411"]
    assert sorted(hits[0].matched_codes) == ["P2B5313", "P2B5411"] and hits[0].scope_match == "exact"
    assert hits[0].record == byd_case().model_copy(update={"review": hits[0].record.review})


@pytest.mark.parametrize(
    "vehicle",
    [
        BYD.model_copy(update={"year": 2023}),
        BYD.model_copy(update={"model": "Seal"}),
        BYD.model_copy(update={"make": "Toyota", "model": "Prius"}),
        BYD.model_copy(update={"powertrain": Powertrain.EV}),
    ],
    ids=["other year", "other model", "other make", "other powertrain"],
)
def test_a_case_is_not_offered_for_another_car(with_seed, vehicle):
    assert with_seed.find_by_codes(vehicle, ["P2B5313"]) == []


def test_a_missing_year_is_reported_not_guessed(store):
    undated = a_case("case:test:no-year", make="Tesla", models=["Model 3", "Model Y"], codes=["BMS_A123_SW_Internal_Isolation"],
                     symptoms=["Internal isolation fault"])
    approve(store, store.save(undated, extracted_by="model").record_id)
    model_y = Vehicle(make="Tesla", model="Model Y", year=2022, powertrain=Powertrain.EV)
    hits = store.find_by_codes(model_y, ["bms_a123"])
    assert len(hits) == 1 and hits[0].scope_match == "year_unknown"
    assert store.find_by_codes(model_y.model_copy(update={"model": "Model S"}), ["bms_a123"]) == []


def test_an_alert_name_matches_however_it_is_written(store):
    case = a_case("case:test:tas", make="Tesla", models=["Model X"], codes=["TAS_a211_noRideHeightCalib"],
                  symptoms=["Automatic leveling disabled"], years=(2020, 2020))
    approve(store, store.save(case, extracted_by="model").record_id)
    model_x = Vehicle(make="Tesla", model="Model X", year=2020, powertrain=Powertrain.EV)
    assert len(store.find_by_codes(model_x, ["tas a211 no ride height calib"])) == 1
    assert store.find_by_codes(model_x, ["TAS_a212"]) == []


def test_a_general_note_applies_by_powertrain_only(with_seed):
    notes = {"note:hv-battery:cell-deviation-imbalance", "note:hv-battery:temp-sensor-swap-test"}
    found = with_seed.search_text(BOLT, "cell deviation between battery cells", record_types=("expert_note",))
    assert {h.record_key for h in found} & notes and all(h.scope_match == "general" for h in found if h.record_key in notes)
    petrol = Vehicle(make="Toyota", model="Corolla", year=2019, powertrain=Powertrain.ICE)
    assert with_seed.search_text(petrol, "cell deviation between battery cells", record_types=("expert_note",)) == []


def test_a_note_for_one_make_is_not_offered_for_another(with_seed):
    query = "vehicle does not enter READY precharge time too long"
    key = "note:gm-ev:precharge-time-too-long"
    assert key in {h.record_key for h in with_seed.search_text(BOLT, query)}
    for other in (
        Vehicle(make="Tesla", model="Model 3", year=2019, powertrain=Powertrain.EV),
        BOLT.model_copy(update={"year": 2015}),  # the note covers 2018 to 2025
        BOLT.model_copy(update={"model": "Malibu", "powertrain": Powertrain.ICE}),
    ):
        assert key not in {h.record_key for h in with_seed.search_text(other, query)}


# -- search by words: find a piece, return the whole record -------------------------
def test_search_returns_the_whole_record_with_the_pieces_that_matched(with_seed):
    hits = with_seed.search_text(BYD, "codes come back right after clearing them at ignition on")
    assert hits and hits[0].record_key == "case:byd-qin-plus-dmi-2024:p2b5313-p2b5411"
    assert isinstance(hits[0].record, Case) and hits[0].record.repair_steps  # the whole case, not the piece
    assert hits[0].matched_units and hits[0].matched_units[0].kind in {"symptom", "finding", "diagnosis", "repair"}
    assert [u.rank for u in hits[0].matched_units] == sorted((u.rank for u in hits[0].matched_units), reverse=True)


def test_text_the_expert_has_not_confirmed_is_not_searched(store):
    record = ExpertNote(
        note_id="note:test:origin", title="Test note", applies_to={"powertrains": ["EV"]},
        claims=[{"text": "The contactor clicks twice before READY", "origin": "expert_stated"},
                {"text": "A corroded busbar raises resistance", "origin": "ai_added"}],
        safety={"level": "normal"}, source={"kind": "expert_transcript", "file": "test.txt"},
    )
    approve(store, store.save(record, extracted_by="model").record_id)
    assert store.search_text(BOLT, "corroded busbar") == []
    assert len(store.search_text(BOLT, "corroded busbar", include_ai_added=True)) == 1
    assert len(store.search_text(BOLT, "contactor clicks")) == 1


def test_a_query_with_no_usable_words_finds_nothing(with_seed):
    assert with_seed.search_text(BYD, "   ") == [] and with_seed.search_text(BYD, "the and of") == []


def test_systems_modules_and_parts_are_linked(with_seed):
    rows = with_seed.conn.execute(
        "SELECT t.kind, t.term_text FROM kb.record_term t JOIN kb.record r ON r.id = t.record_id "
        "WHERE r.record_key = 'case:byd-qin-plus-dmi-2024:p2b5313-p2b5411' ORDER BY 1, 2"
    ).fetchall()
    kinds = {r["kind"] for r in rows}
    assert "system" in kinds and "module" in kinds


# -- migrations and the seed loader ---------------------------------------------------
def test_the_migration_can_be_undone_and_redone(database_url):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO / "migrations"))
    cfg.attributes["database_url"] = database_url
    cfg.attributes["configure_logging"] = False
    command.downgrade(cfg, "base")
    with psycopg.connect(database_url) as c:
        assert c.execute("SELECT to_regclass('kb.record')").fetchone()[0] is None
    command.upgrade(cfg, "head")
    with psycopg.connect(database_url) as c:
        assert c.execute("SELECT to_regclass('kb.record')").fetchone()[0] is not None


def test_the_seed_loader_can_be_run_twice(store):
    import importlib.util

    spec = importlib.util.spec_from_file_location("load_seed", REPO / "scripts" / "load_seed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    first = module.load_all(store)
    second = module.load_all(store)
    assert len(first) == 5 and all(result.startswith("created") for _, result in first)
    assert all(result.startswith("unchanged") for _, result in second)

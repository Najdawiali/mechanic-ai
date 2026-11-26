"""eval/compare_venorex.py: his cases through our real engine (scripted model, test database), and the page."""

import json

import pytest
import yaml

from app.engine.engine import Engine
from app.kb.store import KnowledgeStore
from eval import compare_venorex as cv
from tests.test_engine import Script, p0010


@pytest.fixture
def store(conn) -> KnowledgeStore:
    kb = KnowledgeStore(conn)
    kb.set_review(kb.save(p0010(), extracted_by="hand").record_id, "approved", reviewer="test")
    conn.commit()
    return kb

CASE = {"id": "ENG-900", "bucket": "engine", "language": "en", "expected_behavior": "answer", "safety_critical": False,
        "vehicle": {"make": "Acura", "model": "Integra", "year": 2025, "engine": "1.5L turbo L15CA", "powertrain": "ice", "market": "GCC"},
        "dtcs": ["P0341"], "user_question": "Check engine light, <b>rough idle</b>.",
        "reference": {"summary": "Cam sensor signal.", "must_mention": ["CMP sensor"], "must_not_claim": ["a resistance value"]}}


def test_a_case_runs_through_our_engine_and_a_case_without_a_car_is_not_run(store):
    script = Script()
    result = cv.run_case(lambda: Engine(store, script.model), CASE)
    assert result["tier"] == "general" and result["type"] == "question" and result["calls"] >= 1
    assert cv.vehicle_of(CASE) == {"make": "Acura", "model": "Integra", "year": 2025, "engine_code": "L15CA", "powertrain": "ICE"}
    assert cv.message_of(CASE).endswith("Codes: P0341")
    no_car = {**CASE, "vehicle": {"make": None, "model": None}}
    assert "not_run" in cv.run_case(lambda: Engine(store, script.model), no_car)


def test_both_sides_load_from_his_folder_and_the_page_shows_text_never_markup(tmp_path):
    (tmp_path / "eval/cases/dev").mkdir(parents=True)
    (tmp_path / "eval/cases/dev/ENG-900.yaml").write_text(yaml.safe_dump(CASE), encoding="utf-8")
    (tmp_path / "eval/reports/phase4-final").mkdir(parents=True)
    rows = [{"case_id": "ENG-900", "mode": m, "repeat_index": r, "answer": f"{m} <script>x</script>", "total_ms": 4000, "cost_usd": 0.001}
            for m in cv.HIS_MODES for r in (0, 1)]
    retrieval = {"ENG-900": {"hybrid": {"evidence": [{"title": "A page", "url": "https://ex.com/p", "domain": "ex.com"},
                                                     {"title": "Manual", "url": "https://lemon-manuals.gy/Toyota/x", "domain": "lemon-manuals.gy"}]}}}
    (tmp_path / "eval/reports/phase4-final/results.json").write_text(json.dumps({"rows": rows, "retrieval": retrieval}))
    cases, his = cv.load_cases(tmp_path), cv.load_his_answers(tmp_path)
    assert [c["id"] for c in cases] == ["ENG-900"] and set(his["ENG-900"]) == set(cv.HIS_MODES)
    ours = {"ENG-900": {"tier": "web", "type": "question", "seconds": 3.1, "calls": 3, "cost": 0.002, "web_searches": 1, "refused": 0,
                        "message": "<img src=x onerror=alert(1)>", "warnings": [],
                        "sources": [{"type": "web", "title": "T", "url": "javascript:alert(1)", "applies": "check"}]}}
    page = cv.render(cases, ours, his, {"run_at": "t", "roles": "r", "external_source": "web", "records": "approved"})
    assert "<script>x</script>" not in page and "&lt;script&gt;x&lt;/script&gt;" in page
    assert "<img src=x" not in page and 'href="javascript:' not in page  # only http(s) links become links
    assert "&lt;b&gt;rough idle&lt;/b&gt;" in page


def test_the_engine_code_is_taken_from_his_engine_text():
    assert cv.engine_code("4.6L 1UR-FE V8") == "1UR-FE" and cv.engine_code("5.6L VK56VD V8") == "VK56VD"
    assert cv.engine_code("2.5L 4-cylinder") is None and cv.engine_code("1.5L turbo") is None and cv.engine_code(None) is None

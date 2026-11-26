"""The scored comparison with venorexai: rubric arithmetic, blind pairs in both orders, statistics. No model."""

import json
from dataclasses import dataclass

import pytest

from eval.judge_venorex import (
    OURS,
    Judge,
    answers_for,
    bootstrap_mean,
    parse_json,
    quality,
    run,
)


@dataclass
class _Res:
    text: str
    cost_usd: float = 0.001


def _score(q5=4, critical=False, behavior=True, fabricated=0):
    return {"critical_failure": critical, "critical_reason": None, "behavior_correct": behavior,
            "scores": {k: q5 for k in ("diagnostic_correctness", "evidence_grounding", "procedure_correctness",
                                       "vehicle_applicability", "safety_spec_accuracy")},
            "unsupported_claims": 0, "fabricated_specs": fabricated, "must_mention_missing": [],
            "must_not_claim_violations": [], "justification": "x"}


def test_quality_follows_the_rubric():
    assert quality(_score(5)) == 100.0
    assert quality(_score(5, critical=True)) == 0.0
    # wrong behaviour caps procedure (weight 0.20) at 2: 100 - (3 * 0.20 / 5 * 100) = 88
    assert quality(_score(5, behavior=False)) == 88.0


def test_parse_json_strips_fences():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(ValueError):
        parse_json("no json here")


def test_judge_refuses_the_answering_family():
    with pytest.raises(ValueError):
        Judge(lambda *a, **k: None, "google/gemma-4-31B-it", "rubric", cache=None)


CASE = {"id": "T-1", "bucket": "b", "language": "en", "vehicle": {"make": "Kia", "model": "Rio"}, "user_question": "q"}


def _pair_chat(first_wins_forward: str, first_wins_backward: str):
    """Answers 'better' by the order: the first call sees (ours, his), the second (his, ours)."""
    replies = iter([first_wins_forward, first_wins_backward])

    def chat(messages, **kw):
        return _Res(json.dumps({"better": next(replies), "reason": "r"}))
    return chat


@pytest.mark.parametrize("fwd,back,expected", [("A", "B", "A"), ("B", "A", "B"), ("A", "A", "tie"), ("tie", "B", "tie")])
def test_pair_counts_a_win_only_in_both_orders(fwd, back, expected):
    judge = Judge(_pair_chat(fwd, back), "deepseek-ai/DeepSeek-V3.2", "rubric", cache=None)
    assert judge.pair(CASE, ("ours text", []), ("his text", [])) == expected


def test_cache_means_no_second_call(tmp_path):
    calls = []

    def chat(messages, **kw):
        calls.append(1)
        return _Res(json.dumps(_score(4)))
    cache = tmp_path / "c.jsonl"
    Judge(chat, "deepseek-ai/DeepSeek-V3.2", "r", cache=cache).score(CASE, "answer", [])
    again = Judge(chat, "deepseek-ai/DeepSeek-V3.2", "r", cache=cache).score(CASE, "answer", [])
    assert len(calls) == 1 and again["quality"] == 80.0


def test_answers_for_skips_empty_and_unrun():
    ours = {"T-1": {"message": "m", "warnings": ["w"], "sources": [{"type": "web", "title": "t", "url": "u"}]},
            "T-2": {"not_run": "no car"}}
    his = {"T-1": {"hybrid": {"answer": "h", "sources": []}, "web_only": {"answer": "", "sources": []}}}
    got = answers_for("T-1", ours, his, ["hybrid", "web_only"])
    assert set(got) == {OURS, "hybrid"} and "[warning] w" in got[OURS][0]
    assert answers_for("T-2", ours, his, ["hybrid"]) == {}


def test_run_and_summary_on_matching_cases():
    cases = [dict(CASE, id=f"T-{i}") for i in range(1, 4)]
    ours = {c["id"]: {"message": f"ours {c['id']}", "sources": []} for c in cases}
    ours["T-3"] = {"not_run": "no car"}
    his = {c["id"]: {"hybrid": {"answer": f"his {c['id']}", "sources": []}} for c in cases}

    def chat(messages, **kw):
        user = messages[1]["content"]
        if "ANSWER A" in user:  # pairs: ours always better, in both orders
            first = user.split("ANSWER A\n", 1)[1]
            return _Res(json.dumps({"better": "A" if first.startswith("ours") else "B", "reason": "r"}))
        return _Res(json.dumps(_score(5 if "\nours " in user else 3)))

    judge = Judge(chat, "deepseek-ai/DeepSeek-V3.2", "r", cache=None)
    detail, summary = run(cases, ours, his, judge, ["hybrid"], ["hybrid"], workers=2)
    assert summary["common_cases"] == 2  # T-3 not run on our side: left out for both
    assert summary["systems"][OURS]["quality"][0] == 100.0 and summary["systems"]["hybrid"]["quality"][0] == 60.0
    assert summary["paired"]["hybrid"]["wins"] == 2
    assert summary["pairs"]["hybrid"] == {"ours_better": 2, "his_better": 0, "tie": 0}
    assert not detail["errors"]


def test_bootstrap_interval_contains_the_mean():
    m, lo, hi = bootstrap_mean([1.0, 2.0, 3.0, 4.0, 5.0])
    assert lo <= m <= hi and m == 3.0

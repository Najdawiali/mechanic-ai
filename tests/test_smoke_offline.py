"""Offline tests: prove the gateway and the smoke checks behave, before spending a real call."""

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.llm.gateway import LLMGateway
from eval.smoke import run as smoke
from eval.smoke.pin_image import make_pin_image
from tests.fake_provider import MODELS, make_transport


def gateway(good: bool = True) -> tuple[LLMGateway, httpx.MockTransport]:
    transport = make_transport(good)
    settings = Settings(_env_file=None, deepinfra_api_key=SecretStr("test-key"),
                        llm_base_url="https://fake.local/v1/openai")
    return LLMGateway(settings=settings, http_client=httpx.Client(transport=transport)), transport


def test_missing_key_gives_clear_error():
    with pytest.raises(RuntimeError, match="DEEPINFRA_API_KEY"):
        LLMGateway(settings=Settings(_env_file=None, deepinfra_api_key=None))


def test_key_is_not_in_settings_repr():
    assert "test-key" not in repr(Settings(_env_file=None, deepinfra_api_key=SecretStr("test-key")))


def test_roles_map_to_models():
    gw, _ = gateway()
    assert gw.model_for("reasoning") == "google/gemma-4-31B-it"
    assert gw.model_for("fast") == "google/gemma-4-26B-A4B-it"
    assert gw.model_for("vision") == "google/gemma-4-31B-it"


def test_chat_reads_cost_tokens_and_reasoning():
    gw, _ = gateway()
    r = gw.chat([{"role": "system", "content": "<|think|>x"},
                 {"role": "user", "content": smoke.THINK_QUESTION}])
    assert r.text == "No" and r.reasoning and r.cost_usd == 0.00002
    assert (r.prompt_tokens, r.completion_tokens) == (100, 20)


def test_request_shapes_sent_to_provider():
    gw, transport = gateway()
    gw.chat([{"role": "user", "content": "Reply with the single word OK"}], json_mode=True)
    gw.chat([{"role": "user", "content": "Reply with the single word OK"}], json_schema={"type": "object"})
    gw.chat([{"role": "user", "content": "Hello"}], tools=smoke.TOOLS)
    a, b, c = transport.seen["requests"]
    assert a["response_format"] == {"type": "json_object"}
    assert b["response_format"]["type"] == "json_schema" and b["response_format"]["json_schema"]["strict"]
    assert c["tool_choice"] == "auto" and len(c["tools"]) == 2


def test_pin_image_is_a_png():
    assert make_pin_image([1, 2], 64)[:8] == b"\x89PNG\r\n\x1a\n"


def test_good_model_passes_everything():
    gw, _ = gateway(good=True)
    results = smoke.run_all(gw, MODELS, repeat=2, log=lambda _: None)
    bad = [(r.test, r.model, r.status, r.detail) for r in results if r.status not in ("PASS", "INFO")]
    assert not bad, bad
    assert {r.test for r in results} == set(smoke.TESTS) | {"model_id_exists"}


def test_bad_model_is_caught():
    """The checks must fail on a wrong code, wrong pin, translated digits and Arabic-for-Urdu."""
    gw, _ = gateway(good=False)
    results = {r.test: r for r in smoke.run_all(gw, MODELS[:1], repeat=2, log=lambda _: None)}
    for name in ("tool_call_en", "tool_call_ar", "vision_pins", "intake_ar_jordanian",
                 "intake_urdu", "json_mode_en", "ar_slang_rewrite"):
        assert results[name].status == "FAIL", name
    assert "P0011" in results["tool_call_en"].detail
    assert "Latin digits" in results["intake_ar_jordanian"].detail
    assert "Urdu-specific" in results["intake_urdu"].detail
    assert "extra readings" in results["json_mode_en"].detail


def test_unknown_model_id_is_reported():
    gw, _ = gateway()
    results = smoke.run_all(gw, ["google/does-not-exist"], repeat=1, only=["basic_chat"], log=lambda _: None)
    assert results[0].test == "model_id_exists" and results[0].status == "FAIL"


def test_report_files_are_written(tmp_path, monkeypatch):
    monkeypatch.setattr(smoke, "RESULTS_DIR", tmp_path)
    gw, _ = gateway()
    results = smoke.run_all(gw, MODELS[:1], repeat=1, log=lambda _: None)
    table = smoke.summary_table(results) + "\n" + smoke.model_totals(results)
    path = smoke.write_reports(results, table)
    text = path.read_text(encoding="utf-8")
    assert "intake_urdu" in text and "ٹھیک" in text

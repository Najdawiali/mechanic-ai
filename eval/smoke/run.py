"""Milestone 1, days 1-2: smoke tests for the Gemma 4 models on DeepInfra.

Run from the repo root:

    python -m eval.smoke.run                 # everything, 3 repeats on the reliability tests
    python -m eval.smoke.run --repeat 5
    python -m eval.smoke.run --only vision_pins,tool_call_en
    python -m eval.smoke.run --real-image data/p0010_page2.png

What it answers (handoff sections 5 and 11):
  1. Do the model ids exist on this account?
  2. JSON mode and JSON-schema output: reliable?
  3. Function calling: reliable, in English and in Arabic?
  4. Image input: can it read pins that exist only as graphics?
  5. Arabic (Jordanian dialect) and Urdu: understood, and answered in the same language?
  6. Latency and cost per call.

Status meanings:
  PASS / FAIL  an automatic check decided
  INFO         a measurement, no pass mark
  ERROR        the API call itself failed (the message says why)

This is a smoke test, not the evaluation. A PASS means "works at all";
quality of Arabic and Urdu wording still needs a native reader (see the .md report).
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from app.llm.gateway import (
    LLMGateway,
    LLMResult,
    image_part_from_bytes,
    image_part_from_file,
    user_message_with_image,
)
from eval.smoke.pin_image import make_pin_image

RESULTS_DIR = Path(__file__).parent / "results"


# --------------------------------------------------------------------------
# result record
# --------------------------------------------------------------------------
@dataclass
class TestResult:
    test: str
    model: str
    status: str  # PASS | FAIL | INFO | ERROR
    detail: str = ""
    passed: int = 0
    runs: int = 0
    latencies_s: list[float] = field(default_factory=list)
    completion_tokens: int = 0
    prompt_tokens: int = 0
    cost_usd: float = 0.0
    cost_reported: bool = False
    reasoning_seen: bool = False
    samples: list[str] = field(default_factory=list)  # raw outputs kept for human review

    def add(self, r: LLMResult) -> None:
        self.latencies_s.append(r.latency_s)
        self.completion_tokens += r.completion_tokens
        self.prompt_tokens += r.prompt_tokens
        if r.cost_usd is not None:
            self.cost_usd += float(r.cost_usd)
            self.cost_reported = True
        if r.reasoning:
            self.reasoning_seen = True


def _err(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:300]}"


def _parse_json(text: str) -> tuple[Any, bool]:
    """Returns (object, was_clean). was_clean=False means the JSON was wrapped in a code fence."""
    try:
        return json.loads(text), True
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1)), False
        except json.JSONDecodeError:
            pass
    return None, False


# --------------------------------------------------------------------------
# shared prompts
# --------------------------------------------------------------------------
INTAKE_SYSTEM = """You are the intake step of a diagnostic assistant for car mechanics.
Read the mechanic's message and return ONLY a JSON object with exactly these keys:
- "language": "ar", "en" or "ur" (the language the mechanic wrote in)
- "english_query": the message rewritten as a short English search query using standard automotive terms
- "codes": list of fault codes exactly as written in the message
- "readings": list of {"value": number, "unit": string} for each MEASUREMENT RESULT the mechanic reported. Pin numbers are not readings. Units in English (ohm, V, A).
- "reply": one or two sentences to the mechanic, written in the mechanic's own language, confirming what you understood. Keep fault codes, pin numbers, values and units in English letters and Latin digits (0-9)."""

INTAKE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["language", "english_query", "codes", "readings", "reply"],
    "properties": {
        "language": {"type": "string", "enum": ["ar", "en", "ur"]},
        "english_query": {"type": "string"},
        "codes": {"type": "array", "items": {"type": "string"}},
        "readings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["value", "unit"],
                "properties": {"value": {"type": "number"}, "unit": {"type": "string"}},
            },
        },
        "reply": {"type": "string"},
    },
}

MSG_EN = ("2025 Acura Integra 1.5L turbo, code P0010. I measured 15 ohms between valve pin 1 "
          "and pin 2. What should I check next?")
# Jordanian dialect ("شو", "هسا") with Arabic-Indic digits for the reading.
MSG_AR = ("السيارة أكورا انتيجرا 2025، طالع عندي كود P0010. فحصت الفالف بين البن 1 والبن 2 "
          "وطلعت القراءة ١٥ أوم. شو أفحص هسا؟")
# Urdu with Urdu digits for the reading.
MSG_UR = ("گاڑی 2025 اکیورا انٹیگرا ہے اور سکینر پر P0010 کوڈ آ رہا ہے۔ میں نے والو کے پن 1 اور پن 2 "
          "کے درمیان مزاحمت چیک کی، ریڈنگ ۱۵ اوہم آئی ہے۔ اب کیا چیک کروں؟")
# Workshop slang only: بواجي = spark plugs, دبرياج = clutch.
MSG_AR_SLANG = "السيارة بتقطّع على الطلعة مع إني غيرت البواجي جداد، وكمان الدبرياج صار يسحب."

ARABIC_BLOCK = re.compile(r"[؀-ۿ]")
URDU_ONLY = re.compile(r"[ٹڈڑںےہھگپچکی]")  # letters used in Urdu, not in Arabic
NON_LATIN_DIGITS = re.compile(r"[٠-٩۰-۹]")
LETTERS = re.compile(r"[^\W\d_]")


def _check_intake(obj: Any, lang: str) -> list[str]:
    """Returns the list of problems. Empty list = pass."""
    if not isinstance(obj, dict):
        return ["output is not a JSON object"]
    problems = []
    missing = [k for k in INTAKE_SCHEMA["required"] if k not in obj]
    if missing:
        return [f"missing keys {missing}"]
    if obj["language"] != lang:
        problems.append(f"language={obj['language']!r}, expected {lang!r}")
    if "P0010" not in [str(c).strip() for c in obj["codes"] or []]:
        problems.append(f"codes={obj['codes']!r}, expected ['P0010']")
    readings = obj["readings"] or []
    good = [
        r for r in readings
        if isinstance(r, dict) and r.get("value") == 15
        and ("ohm" in str(r.get("unit", "")).lower() or "Ω" in str(r.get("unit", "")))
    ]
    if not good:
        problems.append(f"readings={readings!r}, expected 15 ohm")
    elif len(readings) > 1:
        problems.append(f"extra readings (pins taken as measurements?): {readings!r}")
    problems += _check_reply_script(str(obj["reply"]), lang)
    return problems


def _check_reply_script(reply: str, lang: str) -> list[str]:
    problems = []
    letters = LETTERS.findall(reply)
    arabic = ARABIC_BLOCK.findall(reply)
    share = len(arabic) / max(len(letters), 1)
    if lang in ("ar", "ur") and share < 0.5:
        problems.append(f"reply is not mostly in Arabic script ({share:.0%})")
    if lang == "en" and share > 0.05:
        problems.append("reply is not in English")
    if lang == "ur" and not URDU_ONLY.search(reply):
        problems.append("reply has no Urdu-specific letters (looks like Arabic, not Urdu)")
    if lang == "ar" and re.search(r"[ٹڈڑںے]", reply):
        problems.append("reply contains Urdu letters, expected Arabic")
    if NON_LATIN_DIGITS.search(reply):
        problems.append("reply uses Arabic/Urdu digits; numbers must stay in Latin digits")
    return problems


# --------------------------------------------------------------------------
# tests.  Each takes (gateway, model, repeat) and returns one TestResult.
# --------------------------------------------------------------------------
def t_basic(gw: LLMGateway, model: str, repeat: int) -> TestResult:
    res = TestResult("basic_chat", model, "FAIL", runs=1)
    r = gw.chat([{"role": "user", "content": "Reply with the single word OK and nothing else."}],
                model=model, max_tokens=256)
    res.add(r)
    ok = r.text.strip().strip(".").upper() == "OK"
    res.passed = int(ok)
    res.status = "PASS" if ok else "FAIL"
    res.detail = (f"text={r.text.strip()[:60]!r} finish={r.finish_reason} "
                  f"reasoning_returned={'yes' if r.reasoning else 'no'}")
    return res


def _intake_test(name: str, msg: str, lang: str, *, schema: bool) -> Callable[..., TestResult]:
    def run(gw: LLMGateway, model: str, repeat: int) -> TestResult:
        res = TestResult(name, model, "FAIL", runs=repeat)
        notes: list[str] = []
        for _ in range(repeat):
            r = gw.chat(
                [{"role": "system", "content": INTAKE_SYSTEM}, {"role": "user", "content": msg}],
                model=model,
                json_mode=not schema,
                json_schema=INTAKE_SCHEMA if schema else None,
            )
            res.add(r)
            obj, clean = _parse_json(r.text)
            problems = _check_intake(obj, lang) if obj is not None else ["output is not valid JSON"]
            if obj is not None and not clean:
                problems.append("JSON was wrapped in a code fence")
            if r.finish_reason == "length":
                problems.append("cut off by max_tokens")
            if problems:
                notes.append("; ".join(problems))
            else:
                res.passed += 1
            if isinstance(obj, dict):
                res.samples.append(json.dumps(
                    {"english_query": obj.get("english_query"), "reply": obj.get("reply")},
                    ensure_ascii=False))
            else:
                res.samples.append(r.text[:400])
        res.status = "PASS" if res.passed == repeat else "FAIL"
        res.detail = " | ".join(dict.fromkeys(notes))[:400]
        return res
    return run


def t_glossary(gw: LLMGateway, model: str, repeat: int) -> TestResult:
    """Does the model know workshop slang without a glossary? Decides how much glossary we need."""
    res = TestResult("ar_slang_rewrite", model, "FAIL", runs=repeat)
    notes: list[str] = []
    for _ in range(repeat):
        r = gw.chat(
            [{"role": "system", "content": INTAKE_SYSTEM}, {"role": "user", "content": MSG_AR_SLANG}],
            model=model, json_mode=True,
        )
        res.add(r)
        obj, _ = _parse_json(r.text)
        query = str(obj.get("english_query", "")).lower() if isinstance(obj, dict) else ""
        res.samples.append(query or r.text[:300])
        missing = [t for t in ("spark plug", "clutch") if t not in query]
        if missing:
            notes.append(f"missing {missing} in {query!r}")
        else:
            res.passed += 1
    res.status = "PASS" if res.passed == repeat else "FAIL"
    res.detail = " | ".join(dict.fromkeys(notes))[:400]
    return res


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_procedure",
            "description": "Get the official troubleshooting procedure for one fault code on one vehicle.",
            "parameters": {
                "type": "object",
                "required": ["make", "model", "year", "code"],
                "properties": {
                    "make": {"type": "string"},
                    "model": {"type": "string"},
                    "year": {"type": "integer"},
                    "code": {"type": "string", "description": "Fault code exactly as shown, e.g. P0010"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_cases",
            "description": "Search previously solved repair cases by vehicle and symptoms.",
            "parameters": {
                "type": "object",
                "required": ["make", "symptoms"],
                "properties": {
                    "make": {"type": "string"},
                    "model": {"type": "string"},
                    "year": {"type": "integer"},
                    "symptoms": {"type": "string"},
                },
            },
        },
    },
]
TOOL_SYSTEM = ("You are a diagnostic assistant for car mechanics. Use the tools to fetch data. "
               "Never invent procedures. If the mechanic is only greeting you, answer without tools.")


def _tool_test(name: str, msg: str, *, expect_call: bool) -> Callable[..., TestResult]:
    def run(gw: LLMGateway, model: str, repeat: int) -> TestResult:
        res = TestResult(name, model, "FAIL", runs=repeat)
        notes: list[str] = []
        for _ in range(repeat):
            r = gw.chat(
                [{"role": "system", "content": TOOL_SYSTEM}, {"role": "user", "content": msg}],
                model=model, tools=TOOLS,
            )
            res.add(r)
            problem = _check_tool(r, expect_call)
            if problem:
                notes.append(problem)
            else:
                res.passed += 1
        res.status = "PASS" if res.passed == repeat else "FAIL"
        res.detail = " | ".join(dict.fromkeys(notes))[:400]
        return res
    return run


def _check_tool(r: LLMResult, expect_call: bool) -> str | None:
    if not expect_call:
        if r.tool_calls:
            return f"called {r.tool_calls[0].name} on a greeting"
        return None
    if not r.tool_calls:
        return f"no tool call; text={r.text[:80]!r}"
    call = r.tool_calls[0]
    if call.name != "get_procedure":
        return f"called {call.name}, expected get_procedure"
    if call.arguments is None:
        return f"arguments are not valid JSON: {call.raw_arguments[:80]!r}"
    a = call.arguments
    if str(a.get("code", "")).strip() != "P0010":
        return f"code={a.get('code')!r}, expected 'P0010'"
    if a.get("year") != 2025:
        return f"year={a.get('year')!r}, expected integer 2025"
    if "acura" not in str(a.get("make", "")).lower():
        return f"make={a.get('make')!r}, expected Acura"
    return None


PIN_CASES = [([1, 2], 64), ([1], 17), ([2], 43), ([1, 2], 80), ([2], 5)]
PIN_QUESTION = ('Some connector terminals in this picture are filled in red. Return ONLY JSON: '
                '{"valve_pins": [numbers of the red terminals on the 2P valve connector], '
                '"pcm_pin": number of the red terminal on PCM connector E}.')


def t_vision(gw: LLMGateway, model: str, repeat: int) -> TestResult:
    res = TestResult("vision_pins", model, "FAIL", runs=repeat)
    notes: list[str] = []
    for i in range(repeat):
        valve, pcm = PIN_CASES[i % len(PIN_CASES)]
        image = image_part_from_bytes(make_pin_image(valve, pcm))
        r = gw.chat([user_message_with_image(image, PIN_QUESTION)], model=model, json_mode=True)
        res.add(r)
        obj, _ = _parse_json(r.text)
        if not isinstance(obj, dict):
            notes.append(f"not JSON: {r.text[:80]!r}")
            continue
        got_valve, got_pcm = obj.get("valve_pins"), obj.get("pcm_pin")
        if got_pcm == pcm and isinstance(got_valve, list) and sorted(got_valve) == valve:
            res.passed += 1
        else:
            notes.append(f"expected valve={valve} pcm={pcm}, got valve={got_valve} pcm={got_pcm}")
    res.status = "PASS" if res.passed == repeat else "FAIL"
    res.detail = " | ".join(notes)[:400]
    return res


def t_stream(gw: LLMGateway, model: str, repeat: int) -> TestResult:
    res = TestResult("stream_first_token", model, "INFO", runs=1)
    s = gw.chat_stream(
        [{"role": "user", "content": "In two sentences, what does an open circuit mean in car wiring?"}],
        model=model,
    )
    res.latencies_s.append(s.latency_s)
    res.passed = int(bool(s.text))
    res.status = "INFO" if s.text else "FAIL"
    ttft = f"{s.ttft_s:.2f}s" if s.ttft_s is not None else "none"
    res.detail = f"first token {ttft}, total {s.latency_s:.2f}s, {len(s.text)} chars"
    return res


THINK_QUESTION = ("The normal range is 7.14 to 7.90 ohm. The reading is 15 ohm. "
                  "Is the reading inside the normal range? Answer yes or no.")


def t_thinking(gw: LLMGateway, model: str, repeat: int) -> TestResult:
    """Measures what thinking mode costs. (In the product, range checks are done in code.)"""
    res = TestResult("thinking_probe", model, "INFO", runs=2)
    parts = []
    for label, system in (("default", "You are a careful assistant."),
                          ("think", "<|think|>You are a careful assistant.")):
        r = gw.chat([{"role": "system", "content": system}, {"role": "user", "content": THINK_QUESTION}],
                    model=model, max_tokens=2048)
        res.add(r)
        leak = "yes" if re.search(r"<\|?/?(think|channel)", r.text) else "no"
        parts.append(f"{label}: answer={r.text.strip()[:25]!r} reasoning_chars={len(r.reasoning or '')} "
                     f"out_tokens={r.completion_tokens} {r.latency_s:.1f}s leak_in_text={leak}")
    res.detail = " || ".join(parts)
    return res


TESTS: dict[str, Callable[..., TestResult]] = {
    "basic_chat": t_basic,
    "json_mode_en": _intake_test("json_mode_en", MSG_EN, "en", schema=False),
    "json_schema_en": _intake_test("json_schema_en", MSG_EN, "en", schema=True),
    "intake_ar_jordanian": _intake_test("intake_ar_jordanian", MSG_AR, "ar", schema=False),
    "intake_urdu": _intake_test("intake_urdu", MSG_UR, "ur", schema=False),
    "ar_slang_rewrite": t_glossary,
    "tool_call_en": _tool_test(
        "tool_call_en",
        "2025 Acura Integra 1.5L turbo. The scan tool shows P0010. Get me the troubleshooting steps.",
        expect_call=True),
    "tool_call_ar": _tool_test(
        "tool_call_ar", "أكورا انتيجرا 2025، الكود P0010. جيب لي خطوات الفحص.", expect_call=True),
    "tool_no_call": _tool_test("tool_no_call", "Hello, good morning!", expect_call=False),
    "vision_pins": t_vision,
    "stream_first_token": t_stream,
    "thinking_probe": t_thinking,
}
SINGLE_RUN = {"basic_chat", "stream_first_token", "thinking_probe"}


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------
def run_all(gw: LLMGateway, models: list[str], repeat: int = 3,
            only: list[str] | None = None, log: Callable[[str], None] = print) -> list[TestResult]:
    results: list[TestResult] = []

    # 1. Do the ids exist? Verify before trusting any name written in a doc.
    try:
        available = set(gw.list_models())
        for m in models:
            ok = m in available
            results.append(TestResult("model_id_exists", m, "PASS" if ok else "FAIL", runs=1,
                                      passed=int(ok),
                                      detail="" if ok else "id not in the provider's model list"))
    except Exception as exc:  # noqa: BLE001
        results.append(TestResult("model_id_exists", "-", "ERROR", detail=_err(exc)))

    names = [n for n in TESTS if not only or n in only]
    for model in models:
        for name in names:
            log(f"  running {name:<22} on {model} ...")
            try:
                results.append(TESTS[name](gw, model, 1 if name in SINGLE_RUN else repeat))
            except Exception as exc:  # noqa: BLE001
                results.append(TestResult(name, model, "ERROR", detail=_err(exc)))
    return results


def run_real_image(gw: LLMGateway, models: list[str], path: str, question: str) -> list[TestResult]:
    out = []
    for model in models:
        res = TestResult("real_image", model, "INFO", runs=1)
        try:
            r = gw.chat([user_message_with_image(image_part_from_file(path), question)],
                        model=model, max_tokens=2048)
            res.add(r)
            res.samples.append(r.text)
            res.detail = "see the .md report; compare with the PDF by eye"
        except Exception as exc:  # noqa: BLE001
            res.status, res.detail = "ERROR", _err(exc)
        out.append(res)
    return out


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------
def _short(model: str) -> str:
    return model.split("/")[-1]


def summary_table(results: list[TestResult]) -> str:
    lines = [f"{'test':<22} {'model':<22} {'status':<6} {'ok':<5} {'median':>7} {'max':>7}  detail",
             "-" * 110]
    for r in results:
        med = f"{statistics.median(r.latencies_s):.2f}s" if r.latencies_s else "-"
        mx = f"{max(r.latencies_s):.2f}s" if r.latencies_s else "-"
        ok = f"{r.passed}/{r.runs}" if r.runs and r.status in ("PASS", "FAIL") else "-"
        lines.append(f"{r.test:<22} {_short(r.model):<22} {r.status:<6} {ok:<5} {med:>7} {mx:>7}  "
                     f"{r.detail[:150]}")
    return "\n".join(lines)


def model_totals(results: list[TestResult]) -> str:
    lines = ["", f"{'model':<22} {'calls':>5} {'median':>7} {'max':>7} {'in tok':>8} {'out tok':>8} "
                 f"{'cost USD':>10}  thinking text returned", "-" * 95]
    for model in dict.fromkeys(r.model for r in results if r.model != "-"):
        rs = [r for r in results if r.model == model]
        lat = [x for r in rs for x in r.latencies_s]
        if not lat:
            continue
        cost = sum(r.cost_usd for r in rs)
        cost_s = f"{cost:.5f}" if any(r.cost_reported for r in rs) else "not given"
        lines.append(f"{_short(model):<22} {len(lat):>5} {statistics.median(lat):>6.2f}s "
                     f"{max(lat):>6.2f}s {sum(r.prompt_tokens for r in rs):>8} "
                     f"{sum(r.completion_tokens for r in rs):>8} {cost_s:>10}  "
                     f"{'yes' if any(r.reasoning_seen for r in rs) else 'no'}")
    return "\n".join(lines)


def write_reports(results: list[TestResult], table: str) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (RESULTS_DIR / f"smoke-{stamp}.json").write_text(
        json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2), encoding="utf-8")
    md = [f"# Smoke test {stamp}", "", "```", table, "```", "",
          "## Outputs for a native reader to judge", "",
          "Automatic checks cannot judge wording. Read these for natural Jordanian Arabic and Urdu.", ""]
    for r in results:
        if r.samples and r.test in {"intake_ar_jordanian", "intake_urdu", "ar_slang_rewrite", "real_image"}:
            md.append(f"### {r.test} / {_short(r.model)}")
            md += [f"- {s}" for s in r.samples]
            md.append("")
    path = RESULTS_DIR / f"smoke-{stamp}.md"
    path.write_text("\n".join(md), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Gemma 4 smoke tests")
    p.add_argument("--repeat", type=int, default=3, help="repeats for the reliability tests")
    p.add_argument("--only", default="", help="comma-separated test names")
    p.add_argument("--models", default="", help="comma-separated model ids (default: from .env)")
    p.add_argument("--real-image", default="", help="path to a real page image, e.g. the P0010 test table")
    p.add_argument("--real-question", default=(
        "List every test in this page as JSON: [{\"step\": ..., \"ignition\": ..., \"from\": "
        "{\"connector\": ..., \"pin\": ...}, \"to\": {\"connector\": ..., \"pin\": ...}, "
        "\"normal_value\": ...}]. Copy pin numbers and values exactly. Use null if you cannot read one."))
    args = p.parse_args(argv)

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles

    try:
        gw = LLMGateway()
    except RuntimeError as exc:
        print(f"SETUP ERROR: {exc}")
        return 2

    models = [m.strip() for m in args.models.split(",") if m.strip()] or list(
        dict.fromkeys([gw.model_for("reasoning"), gw.model_for("fast")]))
    only = [n.strip() for n in args.only.split(",") if n.strip()] or None
    unknown = [n for n in (only or []) if n not in TESTS]
    if unknown:
        print(f"Unknown test names {unknown}. Valid: {', '.join(TESTS)}")
        return 2

    print(f"Endpoint: {gw.settings.llm_base_url}\nModels:   {', '.join(models)}\n")
    results = run_all(gw, models, repeat=args.repeat, only=only)
    if args.real_image:
        results += run_real_image(gw, models, args.real_image, args.real_question)

    table = summary_table(results) + "\n" + model_totals(results)
    print("\n" + table)
    report = write_reports(results, table)
    print(f"\nFull outputs (Arabic / Urdu replies to read): {report}")
    print("\n>>> Paste everything from the 'test  model  status' line down into the chat. <<<")
    return 0 if all(r.status in ("PASS", "INFO") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

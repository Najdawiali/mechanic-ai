"""A fake OpenAI-compatible provider, so the gateway and the smoke runner can be
tested offline, with no key and no cost. `good=True` answers correctly;
`good=False` makes the typical mistakes the smoke checks are meant to catch."""

from __future__ import annotations

import json

import httpx

MODELS = ["google/gemma-4-31B-it", "google/gemma-4-26B-A4B-it"]

REPLIES = {
    "en": "Understood: P0010 with 15 ohm between pin 1 and pin 2. One moment.",
    "ar": "تمام، فهمت عليك: الكود P0010 والقراءة 15 ohm بين البن 1 والبن 2. لحظة.",
    "ur": "ٹھیک ہے، میں سمجھ گیا: کوڈ P0010 ہے اور ریڈنگ 15 ohm ہے۔ ایک لمحہ۔",
}


def _text_of(message: dict) -> str:
    content = message["content"]
    if isinstance(content, str):
        return content
    return " ".join(p.get("text", "") for p in content if p["type"] == "text")


def _lang(text: str) -> str:
    if "گاڑی" in text:
        return "ur"
    if "السيارة" in text:
        return "ar"
    return "en"


def make_transport(good: bool = True) -> httpx.MockTransport:
    seen: dict = {"vision_calls": {}, "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-key"
        if request.url.path.endswith("/models"):
            data = [{"id": m, "object": "model", "created": 0, "owned_by": "x"} for m in MODELS]
            return httpx.Response(200, json={"object": "list", "data": data})

        body = json.loads(request.content)
        seen["requests"].append(body)
        user = body["messages"][-1]
        text = _text_of(user)
        message: dict = {"role": "assistant", "content": ""}

        if body.get("stream"):
            chunks = [
                {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": body["model"],
                 "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
                for piece in ("An open circuit ", "is a broken path.")
            ]
            payload = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
            return httpx.Response(200, content=payload, headers={"content-type": "text/event-stream"})

        if body.get("tools"):
            if "Hello" in text:
                message["content"] = "Good morning! Which vehicle are you working on?"
            else:
                args = {"make": "Acura", "model": "Integra", "year": 2025,
                        "code": "P0010" if good else "P0011"}
                message["tool_calls"] = [{"id": "t1", "type": "function", "function": {
                    "name": "get_procedure", "arguments": json.dumps(args)}}]
        elif isinstance(user["content"], list):  # vision
            from eval.smoke.run import PIN_CASES
            n = seen["vision_calls"].get(body["model"], 0)  # the runner restarts the cases per model
            seen["vision_calls"][body["model"]] = n + 1
            valve, pcm = PIN_CASES[n % len(PIN_CASES)]
            message["content"] = json.dumps({"valve_pins": valve, "pcm_pin": pcm if good else pcm + 1})
        elif "single word OK" in text:
            message["content"] = "OK"
        elif "normal range" in text:
            message["content"] = "No"
            if "<|think|>" in body["messages"][0]["content"]:
                message["reasoning_content"] = "15 is greater than 7.90, so it is outside."
        elif "البواجي" in text:
            query = "engine misfire uphill, new spark plugs, clutch slipping" if good else "car problem"
            message["content"] = json.dumps({"language": "ar", "english_query": query, "codes": [],
                                             "readings": [], "reply": "تمام."}, ensure_ascii=False)
        else:  # intake
            lang = _lang(text)
            obj = {
                "language": lang,
                "english_query": "2025 Acura Integra P0010 valve resistance 15 ohm pin 1 pin 2",
                "codes": ["P0010"],
                "readings": [{"value": 15, "unit": "ohm"}],
                "reply": REPLIES[lang],
            }
            if not good:
                # typical mistakes: pins read as measurements, translated digits, Arabic for Urdu
                obj["readings"] = [{"value": 1, "unit": "pin"}, {"value": 15, "unit": "ohm"}]
                obj["reply"] = {"en": REPLIES["en"], "ar": "القراءة ١٥ أوم.", "ur": "القراءة 15 ohm."}[lang]
            message["content"] = json.dumps(obj, ensure_ascii=False)

        return httpx.Response(200, json={
            "id": "x", "object": "chat.completion", "created": 0, "model": body["model"],
            "choices": [{"index": 0, "message": message,
                         "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                      "estimated_cost": 0.00002},
        })

    transport = httpx.MockTransport(handler)
    transport.seen = seen  # type: ignore[attr-defined]
    return transport

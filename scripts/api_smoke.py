"""End-to-end check of the running AI service, the way the platform's backend will call it.

Start the service in one terminal:      uvicorn app.api.main:app --port 8000
Run this in another:                    python scripts/api_smoke.py

It reads AI_SERVICE_KEY from .env (never type the key into chat). It opens a session for the
2025 Acura Integra L15CA, sends an Arabic message with P0010, a reading, a retry of the same
message, then closes and deletes the session, and prints PASS or FAIL for each step.

The service serves approved records only, as in production. If P0010 is still a draft, either
approve it on the review screen first (recommended), or start the service with KB_SERVE_DRAFTS=true.
"""

from __future__ import annotations

import sys
import time
import uuid

import httpx

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))  # this repo's app, whatever is installed
from app.config import get_settings

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def main() -> int:
    key = get_settings().ai_service_key
    if key is None:
        print("FAIL  AI_SERVICE_KEY is not set in .env")
        return 1
    headers = {"Authorization": f"Bearer {key.get_secret_value()}"}
    session_id = f"smoke-{uuid.uuid4().hex[:8]}"
    failures = 0

    def step(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        failures += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}", flush=True)

    with httpx.Client(base_url=BASE, timeout=120) as client:
        r = client.get("/health")
        step("health", r.status_code == 200, str(r.json() if r.status_code == 200 else r.status_code))
        step("a call without the key is refused", client.post("/ai/sessions", json={}).status_code == 401)

        vehicle = {"make": "Acura", "model": "Integra", "year": 2025, "engine_code": "L15CA", "powertrain": "ICE"}
        r = client.post("/ai/sessions", json={"session_id": session_id, "vehicle": vehicle, "language": "ar"}, headers=headers)
        step("open a session", r.status_code == 201, r.text[:200] if r.status_code != 201 else session_id)

        first_body = {"message_id": "m1", "text": "أكورا انتيجرا 2025، طالع عندي كود P0010"}
        start = time.perf_counter()
        r = client.post(f"/ai/sessions/{session_id}/turns", json=first_body, headers=headers)
        first = r.json() if r.status_code == 200 else {}
        step("first message gives step 1 of the procedure",
             first.get("type") == "step" and (first.get("step") or {}).get("step_id") == "1",
             f"{time.perf_counter() - start:.1f}s type={first.get('type')} tier={first.get('tier')} status={r.status_code}")
        if first.get("type") != "step":
            print("      message:", (first.get("message") or r.text)[:400])
            print("      (if it says there is no verified data: approve P0010 on the review screen, or start the service with KB_SERVE_DRAFTS=true)")
        else:
            print("      message:", first["message"][:300].replace("\n", " | "))

        start = time.perf_counter()
        again = client.post(f"/ai/sessions/{session_id}/turns", json=first_body, headers=headers)
        step("the same message again returns the same reply", again.status_code == 200 and again.json() == first,
             f"{time.perf_counter() - start:.2f}s")
        clash = client.post(f"/ai/sessions/{session_id}/turns", json={"message_id": "m1", "text": "other text"}, headers=headers)
        step("the same message id with other text is refused", clash.status_code == 409)

        r = client.post(f"/ai/sessions/{session_id}/turns", json={"message_id": "m2", "text": "١٥ أوم"}, headers=headers)
        reply = r.json() if r.status_code == 200 else {}
        step("a reading of 15 ohm concludes: the valve", reply.get("type") == "conclusion"
             and any("solenoid valve" in c["name"].lower() for c in reply.get("possible_causes", [])),
             f"type={reply.get('type')}")

        r = client.post(f"/ai/sessions/{session_id}/close", headers=headers)
        summary = r.json() if r.status_code == 200 else {}
        step("close gives a summary and the session's record", r.status_code == 200 and bool(summary.get("summary"))
             and summary.get("record", {}).get("codes") == ["P0010"], (summary.get("summary") or r.text)[:200].replace("\n", " "))
        closed = client.post(f"/ai/sessions/{session_id}/turns", json={"message_id": "m3", "text": "hi"}, headers=headers)
        step("a closed session takes no more messages", closed.status_code == 409)

        r = client.get(f"/ai/sessions/{session_id}/trace", headers=headers)
        calls = r.json() if r.status_code == 200 else []
        step("the model calls are traced", r.status_code == 200 and len(calls) > 0,
             ", ".join(f"{c['purpose']} {c['latency_s']:.1f}s" for c in calls))

        step("delete removes the session", client.delete(f"/ai/sessions/{session_id}", headers=headers).status_code == 204)
        step("a deleted session is gone",
             client.post(f"/ai/sessions/{session_id}/close", headers=headers).status_code == 404)

    print(f"\n{'ALL PASS' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

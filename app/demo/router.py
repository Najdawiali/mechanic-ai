"""TEMPORARY: the demo page and a thin pass-through to /ai (see app/demo/__init__.py for how to remove it).

  GET  /demo                     the page (no sign-in: open only to this computer, when DEMO_ENABLED=true)
  GET  /demo/scenarios           the evaluation scenarios, offered as ready-made demo conversations
  GET  /demo/asset?path=...      a scenario's attachment (a scan-report photo)
  GET  /demo/diagram?key=...     a diagram page image sent in a reply
  POST /demo/api/sessions/{id}/turns/stream   the streamed turn of /ai, called in this service (see turn_stream)
  *    /demo/api/...             passed to /ai/... with the service key added on the server
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.api import ai
from app.config import get_settings

REPO = Path(__file__).resolve().parents[2]
PAGE = Path(__file__).with_name("page.html")
SCENARIOS = REPO / "eval" / "scenarios"

router = APIRouter(prefix="/demo", include_in_schema=False)
LOCAL = {"127.0.0.1", "::1", "localhost"}  # the demo answers only the computer it runs on

# Only these /ai calls can be made from the page: a session's life, nothing that changes the knowledge base.
_ALLOWED = [("POST", re.compile(r"^sessions$")), ("POST", re.compile(r"^sessions/[\w.:-]{1,120}/(turns|close)$")),
            ("GET", re.compile(r"^sessions/[\w.:-]{1,120}/trace$")), ("DELETE", re.compile(r"^sessions/[\w.:-]{1,120}$"))]
_DIAGRAM_KEY = re.compile(r"^(?:derived/)?diagrams/[\w./-]+\.png$")
_ASSET = re.compile(r"^eval/scenarios/assets/[\w.-]+\.(?:png|jpe?g|pdf)$")


class _DemoSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    demo_enabled: bool = False


def viewer(request: Request) -> None:
    """No sign-in (Ali, 2026-11-17): the page is open, but only when DEMO_ENABLED=true and only to this computer.
    Anyone else, or with the demo off, gets 404: for them it does not exist."""
    if not _DemoSettings().demo_enabled or (request.client.host if request.client else "") not in LOCAL:
        raise HTTPException(404)


@router.get("", response_class=HTMLResponse, dependencies=[Depends(viewer)])
def page() -> HTMLResponse:
    return HTMLResponse(PAGE.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


@router.get("/scenarios", dependencies=[Depends(viewer)])
def scenarios() -> list[dict]:
    """The evaluation scenarios as demo scripts: the car, the language and the mechanic's messages in order."""
    out = []
    for path in sorted(SCENARIOS.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        out.append({"id": data["scenario_id"], "group": data["group"], "language": data["language"],
                    "vehicle": data["vehicle"],
                    "turns": [{"text": t["mechanic"], "attachments": t.get("attachments") or []} for t in data["turns"]]})
    return out


@router.get("/asset", dependencies=[Depends(viewer)])
def asset(path: str) -> Response:
    if not _ASSET.match(path) or ".." in path:
        raise HTTPException(404)
    file = REPO / path
    if not file.is_file():
        raise HTTPException(404)
    kind = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "pdf": "application/pdf"}[file.suffix[1:].lower()]
    return Response(file.read_bytes(), media_type=kind)


@router.get("/diagram", dependencies=[Depends(viewer)])
def diagram(key: str) -> Response:
    if not _DIAGRAM_KEY.match(key) or ".." in key:
        raise HTTPException(404)
    from app.storage import ObjectNotFound, get_store

    try:
        return Response(get_store().get(key), media_type="image/png")
    except ObjectNotFound:
        raise HTTPException(404, "the page image is not in storage") from None


@router.post("/api/sessions/{session_id}/turns/stream", dependencies=[Depends(viewer)])
def turn_stream(session_id: str, body: ai.TurnIn, connect: ai.Connector = Depends(ai.get_connector),
                model: ai.ModelClient = Depends(ai.get_model)) -> Response:
    """The streamed turn, called in this service rather than through the pass-through below: httpx's ASGI transport
    collects a whole response before returning it, which would undo the streaming. Same rule as the pass-through:
    it works only when the service key is set."""
    if get_settings().ai_service_key is None:
        raise HTTPException(503, "set AI_SERVICE_KEY in .env: the demo calls /ai with it")
    return ai.turn_stream(session_id, body, connect, model)


@router.api_route("/api/{path:path}", methods=["GET", "POST", "DELETE"], dependencies=[Depends(viewer)])
async def to_ai(path: str, request: Request) -> Response:
    """Passed to /ai inside this service, as the platform's backend would call it. The key never reaches the browser."""
    if not any(method == request.method and rule.match(path) for method, rule in _ALLOWED):
        raise HTTPException(404)
    key = get_settings().ai_service_key
    if key is None:
        raise HTTPException(503, "set AI_SERVICE_KEY in .env: the demo calls /ai with it")
    transport = httpx.ASGITransport(app=request.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ai-service", timeout=180) as client:
        answer = await client.request(request.method, f"/ai/{path}", content=await request.body(),
                                      headers={"Authorization": f"Bearer {key.get_secret_value()}",
                                               "Content-Type": request.headers.get("content-type", "application/json")})
    return Response(answer.content, status_code=answer.status_code,
                    media_type=answer.headers.get("content-type", "application/json"))

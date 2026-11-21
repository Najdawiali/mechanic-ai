"""The assistant's API for the platform's backend (handoff 7.3).

  POST   /ai/sessions                 open a session for a vehicle
  POST   /ai/sessions/{id}/turns      one mechanic message -> one reply (idempotent by message_id)
  POST   /ai/sessions/{id}/close      the summary, the session's record and a draft case
  DELETE /ai/sessions/{id}            remove the session's data (privacy)
  GET    /ai/sessions/{id}/trace      the model calls of a session, for "report a problem"
  POST   /ai/cases                    a case the platform's admin approved -> into the knowledge base
  POST   /ai/ingest                   a technical file the platform put in the shared storage -> read in the background

Service to service: the backend sends "Authorization: Bearer <AI_SERVICE_KEY>". The mechanic's
own sign-in stays with the platform (the AI part does not do authentication of users).
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import logging
import secrets
from collections.abc import Iterator
from functools import lru_cache
from typing import Literal

import psycopg
from fastapi import APIRouter, Depends, Header, HTTPException, Response
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import get_settings
from app.engine import report as report_reader
from app.engine.engine import Engine, Roles
from app.engine.external import from_settings
from app.engine.model import GatewayModel, ModelClient
from app.engine.reply import Reply
from app.engine.sessions import Conflict, SessionNotFound, SessionStore, text_hash
from app.engine.state import SessionState
from app.engine.summary import summarise
from app.kb.schemas import Case, Vehicle
from app.kb.store import APPROVED, APPROVED_AND_DRAFT, KnowledgeStore

log = logging.getLogger(__name__)
router = APIRouter(prefix="/ai", tags=["assistant"])


# -- plumbing ----------------------------------------------------------------------------------------
@lru_cache
def _pool() -> ConnectionPool:
    settings = get_settings()
    return ConnectionPool(settings.database_url, min_size=1, max_size=settings.db_pool_max, open=True,
                          kwargs={"row_factory": dict_row, "connect_timeout": 10})


def get_conn() -> Iterator[psycopg.Connection]:
    """One transaction per request: committed when the request succeeds, rolled back when it fails."""
    with _pool().connection() as conn:
        yield conn


@lru_cache
def _gateway():
    from app.llm.gateway import LLMGateway

    return LLMGateway()


def get_model() -> ModelClient:
    return GatewayModel(_gateway())  # a new trace for every request


def service(authorization: str | None = Header(default=None)) -> None:
    key = get_settings().ai_service_key
    if key is None:
        raise HTTPException(503, "the AI service key is not configured")
    given = (authorization or "").removeprefix("Bearer ").strip()
    if not secrets.compare_digest(given.encode(), key.get_secret_value().encode()):
        raise HTTPException(401, "wrong or missing service key", headers={"WWW-Authenticate": "Bearer"})


def _engine(conn: psycopg.Connection, model: ModelClient) -> Engine:
    settings = get_settings()
    return Engine(KnowledgeStore(conn), model, statuses=APPROVED_AND_DRAFT if settings.kb_serve_drafts else APPROVED,
                  external=from_settings(settings), emergency_number=settings.emergency_number,
                  roles=Roles.from_settings(settings))


# -- request bodies ------------------------------------------------------------------------------------
class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewSession(_In):
    session_id: str = Field(min_length=1, max_length=120)
    vehicle: Vehicle
    language: Literal["en", "ar", "ur"] = "ar"


class AttachmentIn(_In):
    name: str = "attachment"
    content_type: str
    data_base64: str


class TurnIn(_In):
    message_id: str = Field(min_length=1, max_length=120)
    text: str = Field(default="", max_length=4000)
    attachments: list[AttachmentIn] = Field(default_factory=list, max_length=4)


class IngestIn(_In):
    storage_key: str = Field(min_length=1, max_length=500)  # where the platform put the file in the shared bucket
    kind: Literal["licensed_procedure", "licensed_diagram", "client_case_file", "expert_transcript"]
    original_name: str | None = None
    vehicle: dict | None = None  # what the uploader declared (make, model, year, engine); the file's own header is checked against it
    again: bool = False  # read it again even if unchanged (an extractor was improved)


class ApprovedCase(_In):
    case: dict
    reviewer: str = Field(min_length=1, max_length=120)


# -- endpoints -------------------------------------------------------------------------------------------
@router.post("/sessions", dependencies=[Depends(service)], status_code=201)
def open_session(body: NewSession, response: Response, conn: psycopg.Connection = Depends(get_conn)) -> dict:
    try:
        created = SessionStore(conn).create(body.session_id, SessionState(vehicle=body.vehicle, language=body.language))
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if not created:
        response.status_code = 200
    return {"session_id": body.session_id, "status": "open"}


@router.post("/sessions/{session_id}/turns", dependencies=[Depends(service)])
def turn(session_id: str, body: TurnIn, conn: psycopg.Connection = Depends(get_conn),
         model: ModelClient = Depends(get_model)) -> Reply:
    if not body.text.strip() and not body.attachments:
        raise HTTPException(422, "a turn needs text or an attachment")
    attachments = [a.model_dump() for a in body.attachments]
    limit = get_settings().ingest_max_file_mb * 1024 * 1024
    try:
        decoded = [report_reader.decode(a) for a in attachments]
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(422, "an attachment is not valid base64") from exc
    if any(len(a.data) > limit for a in decoded):
        raise HTTPException(413, "an attachment is too large")

    store = SessionStore(conn)
    try:
        state, _ = store.lock(session_id)
    except SessionNotFound as exc:
        raise HTTPException(404, "no such session") from exc
    digest = text_hash(body.text, attachments)
    try:
        stored = store.stored_reply(session_id, body.message_id, digest)
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if stored is not None:
        return stored
    if state.closed:
        raise HTTPException(409, "the session is closed")

    result = _engine(conn, model).turn(state, body.text, decoded)
    store.save_turn(session_id, body.message_id, digest, result.reply, state, model.trace, result.gaps)
    return result.reply


@router.post("/sessions/{session_id}/close", dependencies=[Depends(service)])
def close(session_id: str, conn: psycopg.Connection = Depends(get_conn), model: ModelClient = Depends(get_model)) -> dict:
    store = SessionStore(conn)
    try:
        state, summary = store.lock(session_id)
    except SessionNotFound as exc:
        raise HTTPException(404, "no such session") from exc
    if state.closed and summary is not None:
        return summary  # closing twice returns the same summary
    summary = summarise(model, state, session_id, role=get_settings().llm_role_summary)
    state.closed = True
    store.close(session_id, state, summary, model.trace)
    return summary


@router.delete("/sessions/{session_id}", dependencies=[Depends(service)], status_code=204)
def delete(session_id: str, conn: psycopg.Connection = Depends(get_conn)) -> Response:
    if not SessionStore(conn).delete(session_id):
        raise HTTPException(404, "no such session")
    return Response(status_code=204)


@router.get("/sessions/{session_id}/trace", dependencies=[Depends(service)])
def trace(session_id: str, conn: psycopg.Connection = Depends(get_conn)) -> list[dict]:
    return SessionStore(conn).traces(session_id)


@router.post("/cases", dependencies=[Depends(service)], status_code=201)
def approved_case(body: ApprovedCase, conn: psycopg.Connection = Depends(get_conn)) -> dict:
    """A case the platform's admin reviewed and approved. It is stored as approved, with the admin as reviewer."""
    try:
        case = Case(**{k: v for k, v in body.case.items() if k != "review"})
    except ValidationError as exc:
        raise HTTPException(422, exc.errors(include_url=False)) from exc
    kb = KnowledgeStore(conn)
    saved = kb.save(case, extracted_by="hand")
    kb.set_review(saved.record_id, "approved", reviewer=body.reviewer, on=dt.date.today())
    conn.commit()  # stored and approved whatever happens next
    try:  # searchable by words at once; by meaning as soon as it is embedded (here, or later by the worker)
        from app.kb.embedder import embed_missing

        embed_missing(conn, _gateway())
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        log.warning("the approved case is not embedded yet: %s", exc)
    return {"record_id": saved.record_id, "record_key": case.case_id, "status": "approved"}


def get_storage():
    from app.storage import get_store

    return get_store()


@router.post("/ingest", dependencies=[Depends(service)], status_code=202)
def ingest(body: IngestIn, conn: psycopg.Connection = Depends(get_conn), storage=Depends(get_storage)) -> dict:
    """Queue a file for the background worker. Records made from it are drafts until reviewed."""
    from app.ingest.jobs import register_file

    try:
        done = register_file(conn, storage, body.storage_key, original_name=body.original_name, kind=body.kind,
                             declared=body.vehicle, again=body.again)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(413, str(exc)) from exc
    return {"file_id": done.file_id, "job_id": done.job_id, "action": done.action}


def _b64(data: bytes) -> str:  # used by tests and scripts that build attachments
    return base64.b64encode(data).decode("ascii")

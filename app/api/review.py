"""The review screen: a draft record beside the file it was read from, to approve or reject.

Nothing reaches a mechanic until a person has approved it here. The page puts what needs
attention first: checks that failed against the source, then the extractor's notes.

Access: HTTP Basic. The user name is the reviewer's own name (it is recorded on what they
approve); the password is REVIEW_PASSWORD. Without that setting the screen does not exist.
"""

from __future__ import annotations

import html
import json
import secrets
from collections.abc import Iterator

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import get_settings
from app.db import connect
from app.kb.store import KnowledgeStore
from app.storage import ObjectNotFound, S3Store, get_store

router = APIRouter(prefix="/review", tags=["review"])
_basic = HTTPBasic(auto_error=False)


def get_conn() -> Iterator[psycopg.Connection]:
    with connect() as conn:
        conn.autocommit = True
        yield conn


def get_storage() -> S3Store:
    return get_store()


def reviewer(credentials: HTTPBasicCredentials | None = Depends(_basic)) -> str:
    password = get_settings().review_password
    if password is None or not password.get_secret_value():
        raise HTTPException(404)  # the screen is switched off
    if credentials is None or not credentials.username.strip() or not secrets.compare_digest(
            credentials.password.encode(), password.get_secret_value().encode()):
        raise HTTPException(401, "sign in with your own name and the review password",
                            headers={"WWW-Authenticate": 'Basic realm="Mechanic AI review"'})
    return credentials.username.strip()


# -- small HTML helpers ----------------------------------------------------------------
def e(value) -> str:
    return html.escape("" if value is None else str(value))


CSS = """
body{font:14px/1.45 system-ui,sans-serif;margin:0;color:#1c1c1c;background:#f6f6f4}
header{background:#fff;border-bottom:1px solid #ddd;padding:10px 18px;display:flex;gap:16px;align-items:baseline}
header h1{font-size:16px;margin:0} a{color:#0b5cad} main{padding:16px 18px}
table{border-collapse:collapse;width:100%;background:#fff} th,td{border:1px solid #ddd;padding:6px 8px;text-align:left;vertical-align:top}
th{background:#efefec;font-weight:600} .split{display:grid;grid-template-columns:1fr 1fr;gap:14px;align-items:start}
.pane{background:#fff;border:1px solid #ddd;padding:12px;max-height:82vh;overflow:auto} iframe{width:100%;height:78vh;border:0}
.pane img{max-width:100%;border:1px solid #ddd;margin-bottom:8px} pre{white-space:pre-wrap;margin:0;font:13px/1.4 ui-monospace,monospace}
.note{padding:7px 10px;margin:0 0 6px;border-left:4px solid #999;background:#fff} .failed{border-color:#b3261e;background:#fdecea}
.check{border-color:#b26a00;background:#fff4e0} .tag{display:inline-block;padding:1px 7px;border-radius:9px;background:#e6e6e2;font-size:12px}
.approved{background:#d6f0d8} .rejected{background:#f6d5d2} .bar{margin:12px 0;display:flex;gap:10px;align-items:center}
button{font:inherit;padding:7px 14px;border:1px solid #888;border-radius:5px;background:#fff;cursor:pointer}
button.ok{background:#1b6e2b;color:#fff;border-color:#1b6e2b} dl{display:grid;grid-template-columns:170px 1fr;gap:4px 12px;margin:0}
dt{font-weight:600;color:#444} dd{margin:0} ul{margin:0;padding-left:18px} .muted{color:#666}
"""


def page(title: str, body: str, who: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{e(title)}</title><style>{CSS}</style></head>"
        f"<body><header><h1><a href='/review'>Knowledge base review</a></h1><span>{e(title)}</span>"
        f"<span class='muted' style='margin-left:auto'>signed in as {e(who)}</span></header><main>{body}</main></body></html>")


def as_html(value) -> str:
    """Any part of a record, readable: lists as bullets, nested objects as small tables of fields."""
    if value is None or value == "" or value == [] or value == {}:
        return "<span class='muted'>not given</span>"
    if isinstance(value, list):
        return "<ul>" + "".join(f"<li>{as_html(item)}</li>" for item in value) + "</ul>"
    if isinstance(value, dict):
        return "<dl>" + "".join(f"<dt>{e(k.replace('_', ' '))}</dt><dd>{as_html(v)}</dd>" for k, v in value.items()) + "</dl>"
    return e(value)


def point(p: dict) -> str:
    if p.get("ground"):
        return "ground"
    side = f", {p['terminal_side']} side" if p.get("terminal_side") else ""
    return f"{e(p.get('connector') or p.get('label'))} <b>pin {e(p.get('pin'))}</b>{e(side)}"


def normal(n: dict | None) -> str:
    if not n:
        return ""
    words = {"between": "", "lt": "less than ", "le": "at most ", "gt": "more than ", "ge": "at least "}
    if n["op"] == "between":
        compared = f"{n['min']:g} to {n['max']:g} {n['unit']}"
    else:
        compared = f"{(n['max'] if n['op'] in ('lt', 'le') else n['min']):g} {n['unit']}"
    about = "about " if n.get("approximate") else ""
    condition = f" ({n['condition']})" if n.get("condition") else ""
    if not n.get("as_printed"):
        return e(f"{about}{words[n['op']]}{compared}{condition}")
    # The manual's own digits first; under it, the numbers a reading is compared with in code.
    return (f"{e(about + words[n['op']] + n['as_printed'] + condition)}"
            f"<div class='muted'>compared in code as {e(words[n['op']] + compared)}</div>")


def render_record(record_type: str, body: dict, record_id: int) -> str:
    if record_type == "procedure":
        rows = "".join(
            f"<tr><td>{e(s['id'])}</td><td>{e(s.get('check'))}<div class='muted'>{e(s.get('section'))}</div></td>"
            f"<td>{point(s['measure']['from']) if s.get('measure') else ''}</td><td>{point(s['measure']['to']) if s.get('measure') else ''}</td>"
            f"<td>{normal(s.get('normal'))}</td><td>{e(s.get('if_abnormal'))}</td><td>{as_html(s.get('prepare'))}</td></tr>"
            for s in body.get("steps", []))
        return (f"<p><b>{e(body.get('code'))}</b> {e(body.get('meaning'))}</p>"
                "<table><tr><th>Step</th><th>Inspection</th><th>From</th><th>To</th><th>Normal</th><th>If not normal</th><th>Prepare</th></tr>"
                f"{rows}</table><p><b>If every reading is normal:</b> {e(body.get('elimination_conclusion'))}</p>"
                f"<dl><dt>tool functions</dt><dd>{as_html(body.get('tool_functions'))}</dd>"
                f"<dt>confirmation</dt><dd>{as_html(body.get('confirmation'))}</dd></dl>")
    if record_type == "diagram":
        rows = "".join(
            f"<tr><td>{e(p['component'])}</td><td><b>{e(p['pin'])}</b></td><td>{e(p.get('wire_colour') or 'not given')}</td><td>{e(p['page'])}</td><td>"
            + "<br>".join(("" if r["complete"] else "<b>INCOMPLETE</b> ") + e(r["text"]) for r in p.get("routes", [])) + "</td></tr>"
            for p in body.get("pins", []))
        return (f"<p><b>{e(body.get('title'))}</b> <span class='tag'>reading: {e(body.get('verdict'))}</span></p>"
                f"{as_html(body.get('verdict_reasons')) if body.get('verdict_reasons') else ''}"
                f"<p class='muted'>Versions: {e(json.dumps(body.get('versions') or {}))}</p>"
                f"<table><tr><th>Component</th><th>Pin</th><th>Wire</th><th>Page</th><th>Where the wire goes</th></tr>{rows}</table>")
    skip = {"source", "case_id", "note_id"}
    return as_html({k: v for k, v in body.items() if k not in skip})


def vehicle_text(record_type: str, body: dict) -> str:
    if "vehicle" in body:
        v = body["vehicle"]
        return " ".join(str(x) for x in (v.get("make"), v.get("model"), v.get("year"), v.get("engine_code")) if x)
    if record_type == "case":
        cars = "; ".join(f"{c.get('model') or ''} {c.get('year') or ''}".strip() for c in body.get("repaired_cars") or [])
        return f"{body.get('make', '')} {cars or ' / '.join(body.get('models') or [])}".strip()
    return ", ".join((body.get("applies_to") or {}).get("makes") or []) or "any make"


# -- routes ---------------------------------------------------------------------------------
@router.get("", response_class=HTMLResponse)
def list_records(status: str = "draft", who: str = Depends(reviewer), conn: psycopg.Connection = Depends(get_conn)) -> HTMLResponse:
    rows = conn.execute(
        "SELECT id, record_type, record_key, version, status, reviewer, open_questions, body FROM kb.record "
        "WHERE withdrawn_at IS NULL AND (%(s)s = 'all' OR status = %(s)s) ORDER BY record_type, record_key, version",
        {"s": status},
    ).fetchall()
    links = " · ".join(f"<a href='/review?status={s}'>{s}</a>" for s in ("draft", "approved", "rejected", "all"))
    lines = "".join(
        f"<tr><td><a href='/review/{r['id']}'>{e(r['record_key'])}</a></td><td>{e(r['record_type'])}</td>"
        f"<td>{e(vehicle_text(r['record_type'], r['body']))}</td>"
        f"<td>{sum(q.startswith('CHECK FAILED') for q in r['open_questions']) or ''}</td><td>{len(r['open_questions'])}</td>"
        f"<td><span class='tag {e(r['status'])}'>{e(r['status'])}</span> {e(r['reviewer'])}</td></tr>" for r in rows)
    table = ("<table><tr><th>Record</th><th>Kind</th><th>Vehicle</th><th>Failed checks</th><th>Notes</th><th>Status</th></tr>"
             f"{lines}</table>") if rows else "<p>Nothing here.</p>"
    return page(f"{len(rows)} record(s): {status}", f"<p>Show: {links}</p>{table}", who)


def _record(conn: psycopg.Connection, record_id: int) -> dict:
    row = conn.execute(
        "SELECT r.*, f.storage_key, f.original_name, f.content_type FROM kb.record r "
        "LEFT JOIN kb.source_file f ON f.id = r.source_file_id WHERE r.id = %s", (record_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "no such record")
    return row


@router.get("/{record_id}", response_class=HTMLResponse)
def show_record(record_id: int, who: str = Depends(reviewer), conn: psycopg.Connection = Depends(get_conn),
                storage: S3Store = Depends(get_storage)) -> HTMLResponse:
    row = _record(conn, record_id)
    notes = sorted(row["open_questions"], key=lambda q: (not q.startswith("CHECK FAILED"), not q.startswith("CHECK")))
    failed = sum(q.startswith("CHECK FAILED") for q in notes)
    notes_html = "".join(
        f"<p class='note {'failed' if q.startswith('CHECK FAILED') else 'check' if q.startswith('CHECK') else ''}'>{e(q)}</p>" for q in notes)

    if row["storage_key"] is None:
        source = "<p class='muted'>This record was written by hand or loaded from the repository: there is no source file to compare with.</p>"
    elif (row["content_type"] or "").startswith("text") or row["original_name"].lower().endswith(".txt"):
        try:
            source = f"<pre>{e(storage.get(row['storage_key']).decode('utf-8', errors='replace'))}</pre>"
        except ObjectNotFound:
            source = "<p class='note failed'>The source file is no longer in storage.</p>"
    else:
        source = f"<iframe src='/review/{record_id}/source#navpanes=0&view=FitH' title='source file'></iframe>"
    sheets = "".join(f"<img src='/review/{record_id}/sheet/{i}' alt='diagram page {e(s['page'])}'>"
                     for i, s in enumerate(row["body"].get("sheets") or []) if s.get("image_key"))

    decided = f" by {e(row['reviewer'])} on {e(row['reviewed_on'])}" if row["reviewer"] else ""
    buttons = "" if row["status"] == "approved" else (
        f"<button class='ok' onclick='decide(\"approve\")'>Approve</button><button onclick='decide(\"reject\")'>Reject</button>")
    script = f"""<script>
async function decide(what) {{
  const failed = {failed};
  if (what === 'approve' && failed && !confirm(failed + ' check(s) failed against the source. Approve anyway?')) return;
  const r = await fetch('/review/{record_id}/' + what + (failed ? '?acknowledge_failed=true' : ''), {{method: 'POST'}});
  if (r.ok) location.reload(); else alert((await r.json()).detail);
}}</script>"""
    body = (f"<div class='bar'><b>{e(row['record_key'])}</b><span class='tag'>{e(row['record_type'])}, version {row['version']}</span>"
            f"<span class='tag {e(row['status'])}'>{e(row['status'])}{decided}</span>"
            f"<span class='muted'>read by {e(row['extracted_by'])} from {e(row['original_name'] or 'no file')}</span>{buttons}</div>"
            f"{notes_html}<div class='split'><div class='pane'><h3>Source</h3>{sheets}{source}</div>"
            f"<div class='pane'><h3>What was read from it</h3>{render_record(row['record_type'], row['body'], record_id)}</div></div>{script}")
    return page(row["record_key"], body, who)


@router.get("/{record_id}/source")
def source_file(record_id: int, who: str = Depends(reviewer), conn: psycopg.Connection = Depends(get_conn),
                storage: S3Store = Depends(get_storage)) -> Response:
    row = _record(conn, record_id)
    if row["storage_key"] is None:
        raise HTTPException(404, "this record has no source file")
    try:
        data = storage.get(row["storage_key"])
    except ObjectNotFound:
        raise HTTPException(404, "the source file is no longer in storage") from None
    return Response(data, media_type=row["content_type"] or "application/octet-stream", headers={"Content-Disposition": "inline"})


@router.get("/{record_id}/sheet/{number}")
def diagram_sheet(record_id: int, number: int, who: str = Depends(reviewer), conn: psycopg.Connection = Depends(get_conn),
                  storage: S3Store = Depends(get_storage)) -> Response:
    sheets = _record(conn, record_id)["body"].get("sheets") or []
    if not 0 <= number < len(sheets) or not sheets[number].get("image_key"):
        raise HTTPException(404, "no such page image")
    try:
        return Response(storage.get(sheets[number]["image_key"]), media_type="image/png")
    except ObjectNotFound:
        raise HTTPException(404, "the page image is no longer in storage") from None


@router.post("/{record_id}/{decision}")
def decide(record_id: int, decision: str, acknowledge_failed: bool = False, who: str = Depends(reviewer),
           conn: psycopg.Connection = Depends(get_conn)) -> dict:
    if decision not in ("approve", "reject"):
        raise HTTPException(404)
    row = _record(conn, record_id)
    failed = sum(q.startswith("CHECK FAILED") for q in row["open_questions"])
    if decision == "approve" and failed and not acknowledge_failed:
        raise HTTPException(409, f"{failed} check(s) failed against the source: approving needs an explicit confirmation")
    status = "approved" if decision == "approve" else "rejected"
    KnowledgeStore(conn).set_review(record_id, status, reviewer=who)
    return {"record_id": record_id, "status": status, "reviewer": who}

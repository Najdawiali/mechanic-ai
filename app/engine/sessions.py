"""Sessions in the database (schema ai, migration 0006).

One turn at a time per session: a turn locks its session row (SELECT ... FOR UPDATE) for the
length of the turn, inside one transaction. A second message for the same session waits; a retry
of the same message then finds the stored reply and gets it back unchanged (idempotency). If the
turn fails, the transaction is rolled back: the state is as it was and the message can be retried.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

import psycopg
from psycopg.types.json import Jsonb

from app.engine.engine import Gap
from app.engine.model import TraceEntry
from app.engine.reply import Reply
from app.engine.state import SessionState

LOCK_WAIT = "120s"  # longer than any turn; a turn stuck longer is a fault, and the caller gets an error


class SessionNotFound(LookupError):
    pass


class SessionClosed(RuntimeError):
    pass


class Conflict(RuntimeError):
    pass


def text_hash(text: str, attachments: list[dict]) -> str:
    digest = hashlib.sha256(text.encode("utf-8"))
    for item in attachments:
        digest.update((item.get("data_base64") or "").encode("ascii", "ignore"))
    return digest.hexdigest()


class SessionStore:
    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    def create(self, session_id: str, state: SessionState) -> bool:
        """True = created. False = it exists with the same vehicle (a retried create). Conflict otherwise."""
        row = self.conn.execute(
            "INSERT INTO ai.session (session_id, state) VALUES (%s, %s) ON CONFLICT (session_id) DO NOTHING RETURNING session_id",
            (session_id, Jsonb(state.model_dump(mode="json"))),
        ).fetchone()
        if row:
            return True
        existing = self.conn.execute("SELECT state FROM ai.session WHERE session_id = %s", (session_id,)).fetchone()
        if SessionState(**existing["state"]).vehicle != state.vehicle:
            raise Conflict(f"session {session_id} exists with another vehicle")
        return False

    def lock(self, session_id: str) -> tuple[SessionState, dict | None]:
        """The state, locked until the transaction ends; and the stored summary if closed."""
        self.conn.execute(f"SET LOCAL lock_timeout = '{LOCK_WAIT}'")
        row = self.conn.execute(
            "SELECT state, summary, closed_at FROM ai.session WHERE session_id = %s FOR UPDATE", (session_id,)
        ).fetchone()
        if row is None:
            raise SessionNotFound(session_id)
        return SessionState(**row["state"]), row["summary"]

    def stored_reply(self, session_id: str, message_id: str, digest: str) -> Reply | None:
        row = self.conn.execute(
            "SELECT text_hash, reply FROM ai.turn WHERE session_id = %s AND message_id = %s", (session_id, message_id)
        ).fetchone()
        if row is None:
            return None
        if row["text_hash"] != digest:
            raise Conflict(f"message {message_id} was already answered with other content")
        return Reply(**row["reply"])

    def save_turn(self, session_id: str, message_id: str, digest: str, reply: Reply, state: SessionState,
                  traces: list[TraceEntry], gaps: list[Gap]) -> None:
        self.conn.execute(
            "INSERT INTO ai.turn (session_id, message_id, text_hash, reply) VALUES (%s, %s, %s, %s)",
            (session_id, message_id, digest, Jsonb(reply.model_dump(mode="json"))),
        )
        self.conn.execute("UPDATE ai.session SET state = %s, updated_at = now() WHERE session_id = %s",
                          (Jsonb(state.model_dump(mode="json")), session_id))
        self._traces(session_id, message_id, traces)
        for gap in gaps:
            self.conn.execute(
                "INSERT INTO ai.gap (session_id, vehicle, codes, question, tier) VALUES (%s, %s, %s, %s, %s)",
                (session_id, Jsonb(state.vehicle.model_dump(mode="json", exclude_none=True)), gap.codes, gap.question, gap.tier),
            )

    def _traces(self, session_id: str, message_id: str | None, traces: list[TraceEntry]) -> None:
        for t in traces:
            row = asdict(t)
            self.conn.execute(
                """INSERT INTO ai.trace (session_id, message_id, purpose, model, latency_s, prompt_tokens, completion_tokens,
                                         cost_usd, request, output, error)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (session_id, message_id, row["purpose"], row["model"], row["latency_s"], row["prompt_tokens"],
                 row["completion_tokens"], row["cost_usd"], Jsonb(json.loads(json.dumps(row["request"], default=str))),
                 row["output"], row["error"]),
            )

    def close(self, session_id: str, state: SessionState, summary: dict, traces: list[TraceEntry]) -> None:
        self.conn.execute(
            "UPDATE ai.session SET state = %s, summary = %s, closed_at = now(), updated_at = now() WHERE session_id = %s",
            (Jsonb(state.model_dump(mode="json")), Jsonb(summary), session_id),
        )
        self._traces(session_id, None, traces)

    def delete(self, session_id: str) -> bool:
        """Removes the session, its turns and its traces; a gap keeps the vehicle and codes, without text."""
        self.conn.execute("UPDATE ai.gap SET question = NULL WHERE session_id = %s", (session_id,))
        return self.conn.execute("DELETE FROM ai.session WHERE session_id = %s RETURNING 1", (session_id,)).fetchone() is not None

    def traces(self, session_id: str) -> list[dict]:
        return self.conn.execute(
            """SELECT message_id, purpose, model, latency_s, prompt_tokens, completion_tokens, cost_usd, error, created_at
               FROM ai.trace WHERE session_id = %s ORDER BY id""", (session_id,)).fetchall()

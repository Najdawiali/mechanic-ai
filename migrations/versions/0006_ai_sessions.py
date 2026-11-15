"""The assistant's own tables: sessions, turns, gaps and model-call traces (schema ai).

Revision ID: 0006
Revises: 0005

  ai.session  the AI's working state for one conversation (the platform keeps the messages)
  ai.turn     every answered message, so a retried message gets the same reply (idempotency)
  ai.gap      questions with no verified data: what the client should collect next
  ai.trace    every model call of a turn, for "report a problem" and for cost tracking

Deleting a session deletes its turns and traces. A gap keeps only the vehicle and the codes,
with no session and no text, so demand can still be counted after the mechanic's words are gone.
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE SCHEMA ai;

CREATE TABLE ai.session (
    session_id  text PRIMARY KEY,
    state       jsonb NOT NULL,
    summary     jsonb,                       -- written when the session is closed
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    closed_at   timestamptz
);

CREATE TABLE ai.turn (
    session_id  text NOT NULL REFERENCES ai.session ON DELETE CASCADE,
    message_id  text NOT NULL,
    text_hash   text NOT NULL,               -- the same message id with other text is refused
    reply       jsonb NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, message_id)
);

CREATE TABLE ai.gap (
    id          bigserial PRIMARY KEY,
    session_id  text REFERENCES ai.session ON DELETE SET NULL,
    vehicle     jsonb NOT NULL,
    codes       text[] NOT NULL DEFAULT '{}',
    question    text,                        -- removed with the session
    tier        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX gap_created ON ai.gap (created_at);

CREATE TABLE ai.trace (
    id                 bigserial PRIMARY KEY,
    session_id         text NOT NULL REFERENCES ai.session ON DELETE CASCADE,
    message_id         text,
    purpose            text NOT NULL,
    model              text NOT NULL,
    latency_s          real NOT NULL,
    prompt_tokens      integer NOT NULL DEFAULT 0,
    completion_tokens  integer NOT NULL DEFAULT 0,
    cost_usd           real,
    request            jsonb NOT NULL,
    output             text NOT NULL,
    error              text,
    created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX trace_session ON ai.trace (session_id, created_at);
""")


def downgrade() -> None:
    op.execute("DROP SCHEMA ai CASCADE;")

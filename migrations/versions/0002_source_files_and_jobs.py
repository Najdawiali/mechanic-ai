"""Source files and the background job queue.

Revision ID: 0002
Revises: 0001

  kb.source_file  one row per file handed to the service: where it is stored, its fingerprint,
                  what the uploader said about it
  kb.ingest_job   the work queue: one job turns one file into draft records

A record now points to the file it was extracted from. Withdrawing a file withdraws its records.
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE kb.source_file (
            id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            storage_key   text    NOT NULL UNIQUE,            -- where the file is in the bucket
            original_name text    NOT NULL,
            kind          text CHECK (kind IN ('licensed_procedure', 'licensed_diagram', 'client_case_file',
                                               'expert_transcript')),  -- NULL until the file is classified
            sha256        text    NOT NULL,                   -- fingerprint of the content
            size_bytes    bigint  NOT NULL,
            content_type  text,
            declared      jsonb   NOT NULL DEFAULT '{}'::jsonb, -- what the uploader said: make, model, year, system
            version       integer NOT NULL DEFAULT 1,         -- goes up when the content changes
            withdrawn_at  timestamptz,
            created_at    timestamptz NOT NULL DEFAULT now(),
            updated_at    timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE kb.ingest_job (
            id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            source_file_id bigint  NOT NULL REFERENCES kb.source_file (id) ON DELETE CASCADE,
            status         text    NOT NULL DEFAULT 'queued'
                           CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
            attempts       integer NOT NULL DEFAULT 0,
            max_attempts   integer NOT NULL DEFAULT 3 CHECK (max_attempts >= 1),
            run_after      timestamptz NOT NULL DEFAULT now(),
            locked_by      text,
            locked_at      timestamptz,
            file_version   integer,                           -- the file version the job processed
            error          text,
            result         jsonb,
            created_at     timestamptz NOT NULL DEFAULT now(),
            started_at     timestamptz,
            finished_at    timestamptz
        );
        CREATE INDEX ingest_job_queue_idx ON kb.ingest_job (run_after, id) WHERE status = 'queued';
        CREATE INDEX ingest_job_file_idx ON kb.ingest_job (source_file_id, id DESC);
        -- At most one waiting job per file: a second request while one waits changes nothing.
        CREATE UNIQUE INDEX ingest_job_one_queued_per_file ON kb.ingest_job (source_file_id) WHERE status = 'queued';

        ALTER TABLE kb.record
            ADD COLUMN source_file_id bigint REFERENCES kb.source_file (id),
            ADD COLUMN withdrawn_at timestamptz;
        CREATE INDEX record_source_file_idx ON kb.record (source_file_id);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX kb.record_source_file_idx;
        ALTER TABLE kb.record DROP COLUMN source_file_id, DROP COLUMN withdrawn_at;
        DROP TABLE kb.ingest_job;
        DROP TABLE kb.source_file;
        """
    )

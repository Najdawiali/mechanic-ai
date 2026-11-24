"""Public records: NHTSA recalls, defect investigations and owner complaints (schema pub).

Revision ID: 0007
Revises: 0006

Kept apart from the workshop's own knowledge base (schema kb), because these are not the client's
verified records: they are US-market public data, and the complaints are owners' unchecked reports.
They are never served as verified and never mixed into kb search.

  pub.document  one record: a recall campaign or an investigation for one make and model (years as a range),
                or one owner complaint for one car
  pub.chunk     the pieces of a document that are searched (words and meaning); a hit returns its document

make_key is the make group the source uses (NHTSA files name sibling brands under one group, e.g. Acura
under Honda); model_key is the model in upper-case words. A lookup always filters by make group, model and
year: a record for another car is never offered. There is no vector index on purpose: every search is
already narrowed to one car's few hundred pieces, and an exact scan of those is faster and exact.
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE SCHEMA pub;

CREATE TABLE pub.document (
    id              bigserial PRIMARY KEY,
    source          text NOT NULL CHECK (source IN ('nhtsa_recall', 'nhtsa_investigation', 'nhtsa_complaint')),
    external_id     text NOT NULL,           -- recall campaign, investigation action or complaint ODI number
    source_type     text NOT NULL CHECK (source_type IN ('official_recall', 'official_investigation', 'unverified_report')),
    make_key        text NOT NULL,
    make            text NOT NULL,
    model           text NOT NULL,
    model_key       text NOT NULL,
    year_from       int,
    year_to         int,
    codes           text[] NOT NULL DEFAULT '{}',
    component       text,
    title           text NOT NULL,
    text            text NOT NULL,
    url             text,
    dataset_version text NOT NULL,           -- the snapshot it came from
    content_hash    text NOT NULL UNIQUE,    -- importing the same snapshot twice changes nothing
    meta            jsonb NOT NULL DEFAULT '{}',
    imported_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX document_car_idx ON pub.document (make_key, model_key);

CREATE TABLE pub.chunk (
    id              bigserial PRIMARY KEY,
    document_id     bigint NOT NULL REFERENCES pub.document ON DELETE CASCADE,
    chunk_index     int NOT NULL,
    text            text NOT NULL,
    make_key        text NOT NULL,
    model_key       text NOT NULL,
    year_from       int,
    year_to         int,
    source_type     text NOT NULL,
    codes           text[] NOT NULL DEFAULT '{}',
    tsv             tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    embedding       vector(1024),
    embedding_model text,
    UNIQUE (document_id, chunk_index)
);
CREATE INDEX chunk_car_idx ON pub.chunk (make_key, model_key);
CREATE INDEX chunk_codes_idx ON pub.chunk USING gin (codes);
CREATE INDEX chunk_tsv_idx ON pub.chunk USING gin (tsv);
CREATE INDEX chunk_to_embed_idx ON pub.chunk (id) WHERE embedding IS NULL;
""")


def downgrade() -> None:
    op.execute("DROP SCHEMA pub CASCADE;")

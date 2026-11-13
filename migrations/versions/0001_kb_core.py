"""Knowledge-base core: records, their vehicle scope, fault codes, terms and search units.

Revision ID: 0001
Revises:

One row in kb.record is one knowledge record (a procedure, a diagram page, a solved case or an
expert note). The record itself is stored whole in `body`, exactly as the Pydantic model in
app/kb/schemas.py validated it. Everything the service filters on is copied out into typed
columns and child tables, so lookups by vehicle and fault code are exact SQL, never similarity.

  kb.record        the record, its version and its review status
  kb.record_scope  which vehicles the record applies to (one row per make/model/powertrain)
  kb.record_code   its fault codes, as written and as a normalised key
  kb.record_term   its systems, control modules and parts (the links between records)
  kb.search_unit   small pieces of its text (one symptom, one finding, one claim): search finds
                   a piece and the service returns the whole record it belongs to
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE EXTENSION IF NOT EXISTS vector;
        CREATE SCHEMA IF NOT EXISTS kb;
        CREATE SCHEMA IF NOT EXISTS ai_state;

        CREATE TABLE kb.record (
            id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            record_type    text    NOT NULL CHECK (record_type IN ('procedure', 'diagram_page', 'case', 'expert_note')),
            record_key     text    NOT NULL,                 -- the record's own id, e.g. its procedure_id
            version        integer NOT NULL DEFAULT 1 CHECK (version >= 1),
            status         text    NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved', 'rejected')),
            reviewer       text,
            reviewed_on    date,
            open_questions jsonb   NOT NULL DEFAULT '[]'::jsonb,
            source_kind    text    NOT NULL,
            licensed       boolean NOT NULL,                 -- taken from a licensed manual
            extracted_by   text    NOT NULL CHECK (extracted_by IN ('code', 'model', 'hand')),
            body           jsonb   NOT NULL,                 -- the whole record, without its review
            body_hash      text    NOT NULL,                 -- sha256 of body: the same content is never stored twice
            created_at     timestamptz NOT NULL DEFAULT now(),
            updated_at     timestamptz NOT NULL DEFAULT now(),
            UNIQUE (record_type, record_key, version),
            CONSTRAINT approved_names_its_reviewer CHECK (status <> 'approved' OR reviewer IS NOT NULL)
        );
        CREATE INDEX record_status_idx ON kb.record (record_type, record_key, status, version DESC);

        CREATE TABLE kb.record_scope (
            id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            record_id  bigint NOT NULL REFERENCES kb.record (id) ON DELETE CASCADE,
            make_key   text,                                  -- NULL = not tied to a make
            model_key  text,                                  -- NULL = any model of the make
            engine_key text,                                  -- NULL = any engine
            year_from  integer,                               -- both NULL = the year was not recorded
            year_to    integer,
            powertrain text CHECK (powertrain IN ('ICE', 'Hybrid', 'PHEV', 'EV')),
            CONSTRAINT model_needs_make CHECK (model_key IS NULL OR make_key IS NOT NULL),
            CONSTRAINT years_in_order CHECK (year_from IS NULL OR year_to IS NULL OR year_from <= year_to)
        );
        CREATE INDEX record_scope_vehicle_idx ON kb.record_scope (make_key, model_key);
        CREATE INDEX record_scope_record_idx ON kb.record_scope (record_id);

        CREATE TABLE kb.record_code (
            id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            record_id bigint NOT NULL REFERENCES kb.record (id) ON DELETE CASCADE,
            code_raw  text NOT NULL,                          -- exactly as written in the source
            code_key  text NOT NULL,                          -- upper case, letters and digits only
            stem_key  text NOT NULL,                          -- what lookups compare (see app/kb/keys.py)
            module    text,
            UNIQUE (record_id, code_key)
        );
        CREATE INDEX record_code_stem_idx ON kb.record_code (stem_key);

        CREATE TABLE kb.record_term (
            record_id bigint NOT NULL REFERENCES kb.record (id) ON DELETE CASCADE,
            kind      text   NOT NULL CHECK (kind IN ('system', 'module', 'part')),
            term_key  text   NOT NULL,
            term_text text   NOT NULL,
            PRIMARY KEY (record_id, kind, term_key)
        );
        CREATE INDEX record_term_idx ON kb.record_term (kind, term_key);

        CREATE TABLE kb.search_unit (
            id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            record_id bigint  NOT NULL REFERENCES kb.record (id) ON DELETE CASCADE,
            kind      text    NOT NULL,                       -- symptom, finding, claim, ...
            ordinal   integer NOT NULL,
            text      text    NOT NULL CHECK (length(btrim(text)) > 0),
            origin    text CHECK (origin IN ('expert_stated', 'expert_summarised', 'ai_added')),
            tsv       tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
            UNIQUE (record_id, kind, ordinal)
        );
        CREATE INDEX search_unit_tsv_idx ON kb.search_unit USING gin (tsv);
        CREATE INDEX search_unit_record_idx ON kb.search_unit (record_id);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE kb.search_unit;
        DROP TABLE kb.record_term;
        DROP TABLE kb.record_code;
        DROP TABLE kb.record_scope;
        DROP TABLE kb.record;
        """
    )

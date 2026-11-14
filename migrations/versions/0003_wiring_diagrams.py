"""Wiring diagrams: one record per diagram, and its pins for exact lookup.

Revision ID: 0003
Revises: 0002

The record type `diagram_page` (never used) becomes `diagram`: one record per diagram with all its
pages. kb.diagram_pin holds every pin of every component with the routes worked out when the
diagram was read, so "where does pin 31 go" is one exact query at question time.
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE kb.record DROP CONSTRAINT record_record_type_check;
        ALTER TABLE kb.record ADD CONSTRAINT record_record_type_check
            CHECK (record_type IN ('procedure', 'diagram', 'case', 'expert_note'));

        CREATE TABLE kb.diagram_pin (
            id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            record_id     bigint NOT NULL REFERENCES kb.record (id) ON DELETE CASCADE,
            component_key text   NOT NULL,                  -- upper case, letters and digits only
            component     text   NOT NULL,                  -- as printed on the diagram
            pin_key       text   NOT NULL,
            pin           text   NOT NULL,
            wire_colour   text,                             -- NULL = the diagram prints no colour
            page          text   NOT NULL,                  -- "1 of 3"
            fuse          text,
            routes        jsonb  NOT NULL DEFAULT '[]'::jsonb
        );
        CREATE INDEX diagram_pin_lookup_idx ON kb.diagram_pin (component_key, pin_key);
        CREATE INDEX diagram_pin_record_idx ON kb.diagram_pin (record_id);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE kb.diagram_pin;
        ALTER TABLE kb.record DROP CONSTRAINT record_record_type_check;
        ALTER TABLE kb.record ADD CONSTRAINT record_record_type_check
            CHECK (record_type IN ('procedure', 'diagram_page', 'case', 'expert_note'));
        """
    )

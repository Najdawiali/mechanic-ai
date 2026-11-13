"""Search by meaning: a vector for every search unit.

Revision ID: 0004
Revises: 0003

Each small piece of text (one symptom, one finding, one claim) gets a vector from the embedding
model, so a mechanic's description finds pieces that mean the same in other words. The model
that made each vector is recorded: when the model changes, the vectors are made again.
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE kb.search_unit
            ADD COLUMN embedding vector(1024),
            ADD COLUMN embedding_model text;
        CREATE INDEX search_unit_embedding_idx ON kb.search_unit USING hnsw (embedding vector_cosine_ops);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX kb.search_unit_embedding_idx;
        ALTER TABLE kb.search_unit DROP COLUMN embedding, DROP COLUMN embedding_model;
        """
    )

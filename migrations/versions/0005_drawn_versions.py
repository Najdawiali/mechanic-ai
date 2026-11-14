"""Wiring diagrams: the drawn version a pin belongs to.

Revision ID: 0005
Revises: 0004

A diagram can draw one component twice, once for each version of the car (a brake pedal switch
for the 1.5L and one for the 2.0L), with the same pin numbers on different wires. Each stored pin
now says which drawn version it belongs to, so the two are never read as one.
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE kb.diagram_pin ADD COLUMN version text;  -- NULL = the component is drawn once")


def downgrade() -> None:
    op.execute("ALTER TABLE kb.diagram_pin DROP COLUMN version;")

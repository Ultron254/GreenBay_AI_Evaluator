"""v6.1.0 — add appliance size columns to valuation_sessions.

New columns: size_value, size_unit, size_source
Drives accurate per-size pricing (e.g. 43" vs 55" TV).

Revision ID: v610_size_cols
Revises: v601_json_to_text
Create Date: 2026-06-02 16:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "v610_size_cols"
down_revision = "v601_json_to_text"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("valuation_sessions", sa.Column("size_value", sa.Float(), nullable=True))
    op.add_column("valuation_sessions", sa.Column("size_unit", sa.String(20), nullable=True))
    op.add_column("valuation_sessions", sa.Column("size_source", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("valuation_sessions", "size_source")
    op.drop_column("valuation_sessions", "size_unit")
    op.drop_column("valuation_sessions", "size_value")

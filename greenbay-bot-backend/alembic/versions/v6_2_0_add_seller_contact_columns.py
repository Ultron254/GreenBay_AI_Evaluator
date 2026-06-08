"""v6.2.0 — add seller contact columns to valuation_sessions.

New columns: seller_name, seller_phone

These exist on the ValuationSession model and are written by the evaluate
endpoint, but were only ever created implicitly via ``Base.metadata.create_all``
(never as a migration). This adds them explicitly so an Alembic-managed /
fresh database stays in sync with the model. Idempotent: it inspects the live
schema first, so it is a no-op where ``create_all`` already added the columns.

Revision ID: v620_seller_cols
Revises: v610_size_cols
Create Date: 2026-06-08 12:30:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "v620_seller_cols"
down_revision = "v610_size_cols"
branch_labels = None
depends_on = None


def _existing_columns(table: str) -> set[str]:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    try:
        return {c["name"] for c in insp.get_columns(table)}
    except Exception:  # noqa: BLE001 — table may not exist yet on a bare DB
        return set()


def upgrade() -> None:
    cols = _existing_columns("valuation_sessions")
    if "seller_name" not in cols:
        op.add_column(
            "valuation_sessions", sa.Column("seller_name", sa.String(100), nullable=True)
        )
    if "seller_phone" not in cols:
        op.add_column(
            "valuation_sessions", sa.Column("seller_phone", sa.String(20), nullable=True)
        )


def downgrade() -> None:
    cols = _existing_columns("valuation_sessions")
    if "seller_phone" in cols:
        op.drop_column("valuation_sessions", "seller_phone")
    if "seller_name" in cols:
        op.drop_column("valuation_sessions", "seller_name")

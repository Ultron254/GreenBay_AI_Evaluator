"""v6.3.0: add campaign attribution columns to valuation_sessions.

New columns: utm_source, utm_medium, utm_campaign, utm_content, referrer,
landing_url. The web frontend captures them on first load and sends them with
POST /tradein/evaluate; the evaluate endpoint stores them on the session and
mirrors them to Airtable (EVALUATOR_CHANGES_REQUIRED.md, entries E2 and E3).

Idempotent: inspects the live schema first, so it is a no-op where the
columns already exist. All columns are nullable, so existing rows need no
backfill.

Revision ID: v630_attribution
Revises: v620_seller_cols
Create Date: 2026-09-20 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "v630_attribution"
down_revision = "v620_seller_cols"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("utm_source", sa.String(200)),
    ("utm_medium", sa.String(200)),
    ("utm_campaign", sa.String(200)),
    ("utm_content", sa.String(200)),
    ("referrer", sa.String(500)),
    ("landing_url", sa.String(2000)),
)


def _existing_columns(table: str) -> set[str]:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    try:
        return {c["name"] for c in insp.get_columns(table)}
    except Exception:  # noqa: BLE001, table may not exist yet on a bare DB
        return set()


def upgrade() -> None:
    cols = _existing_columns("valuation_sessions")
    for name, col_type in _COLUMNS:
        if name not in cols:
            op.add_column("valuation_sessions", sa.Column(name, col_type, nullable=True))


def downgrade() -> None:
    cols = _existing_columns("valuation_sessions")
    for name, _ in reversed(_COLUMNS):
        if name in cols:
            op.drop_column("valuation_sessions", name)

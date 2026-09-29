"""v6.3.0 — add image_s3_keys to valuation_sessions.

The evaluate endpoint already uploads the customer's photos to S3, but the keys
were only persisted on TradeInSession (WhatsApp flow). Anonymous web valuations
therefore lost the reference, so the accept/reject notification sent to the CX
team could not include the photos. Storing the keys on the valuation session
lets any later request re-presign them.

Revision ID: v630_image_keys
Revises: v630_attribution
Create Date: 2026-09-06 10:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "v630_image_keys"
down_revision = "v630_attribution"
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
    if "image_s3_keys" not in cols:
        op.add_column(
            "valuation_sessions", sa.Column("image_s3_keys", sa.JSON(), nullable=True)
        )


def downgrade() -> None:
    cols = _existing_columns("valuation_sessions")
    if "image_s3_keys" in cols:
        op.drop_column("valuation_sessions", "image_s3_keys")

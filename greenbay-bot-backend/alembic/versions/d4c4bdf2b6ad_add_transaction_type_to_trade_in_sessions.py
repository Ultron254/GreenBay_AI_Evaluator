"""Add transaction_type column to trade_in_sessions

Revision ID: d4c4bdf2b6ad
Revises: 24c353689407_add_category_assessment_fields_to_trade_
Create Date: 2025-11-17 21:25:00.000000
"""

from typing import Union, Sequence
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d4c4bdf2b6ad"
down_revision: Union[str, None] = "3f2d1a7f2c3a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "trade_in_sessions",
        sa.Column(
            "transaction_type",
            sa.String(length=20),
            nullable=False,
            server_default="trade_in",
        ),
    )
    # Remove server default after existing rows are populated
    op.alter_column(
        "trade_in_sessions",
        "transaction_type",
        server_default=None,
    )


def downgrade() -> None:
    op.drop_column("trade_in_sessions", "transaction_type")


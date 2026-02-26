"""add_pending_payment_status

Revision ID: 8bd18fad3110
Revises: 8fc7922d4123
Create Date: 2025-10-22 23:10:38.875414

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8bd18fad3110'
down_revision: Union[str, Sequence[str], None] = '8fc7922d4123'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add 'pending_payment' to OrderStatus enum (can't specify position in PostgreSQL)
    op.execute("ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS 'pending_payment'")


def downgrade() -> None:
    """Downgrade schema."""
    # Note: PostgreSQL doesn't support removing enum values directly
    # You would need to recreate the enum type if you want to remove it
    pass

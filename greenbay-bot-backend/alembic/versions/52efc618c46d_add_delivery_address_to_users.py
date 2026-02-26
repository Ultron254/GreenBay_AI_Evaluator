"""add_delivery_address_to_users

Revision ID: 52efc618c46d
Revises: d4c4bdf2b6ad
Create Date: 2025-11-18 22:21:26.691932

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '52efc618c46d'
down_revision: Union[str, Sequence[str], None] = 'd4c4bdf2b6ad'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add delivery_address column to users table
    op.add_column('users', sa.Column('delivery_address', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    # Remove delivery_address column from users table
    op.drop_column('users', 'delivery_address')

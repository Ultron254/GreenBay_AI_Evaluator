"""add_checkout_request_id_to_payments

Revision ID: bdd84b411617
Revises: 52efc618c46d
Create Date: 2025-11-18 22:46:12.482002

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'bdd84b411617'
down_revision: Union[str, Sequence[str], None] = '52efc618c46d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add checkout_request_id column to payments table
    op.add_column('payments', sa.Column('checkout_request_id', sa.String(length=100), nullable=True))
    op.create_index(op.f('ix_payments_checkout_request_id'), 'payments', ['checkout_request_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    # Remove checkout_request_id column from payments table
    op.drop_index(op.f('ix_payments_checkout_request_id'), table_name='payments')
    op.drop_column('payments', 'checkout_request_id')

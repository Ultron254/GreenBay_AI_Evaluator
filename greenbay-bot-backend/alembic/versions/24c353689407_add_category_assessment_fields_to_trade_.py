"""add_category_assessment_fields_to_trade_in_session

Revision ID: 24c353689407
Revises: 6afdf88dd3d7
Create Date: 2025-10-29 00:42:33.745276

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '24c353689407'
down_revision: Union[str, Sequence[str], None] = '6afdf88dd3d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add category-specific assessment fields to trade_in_sessions table
    op.add_column('trade_in_sessions', sa.Column('detected_category', sa.String(100), nullable=True))
    op.add_column('trade_in_sessions', sa.Column('category_questions', sa.Text(), nullable=True))
    op.add_column('trade_in_sessions', sa.Column('condition_responses', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    # Remove category-specific assessment fields
    op.drop_column('trade_in_sessions', 'condition_responses')
    op.drop_column('trade_in_sessions', 'category_questions')
    op.drop_column('trade_in_sessions', 'detected_category')

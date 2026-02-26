"""Fix trade_in_images schema

Revision ID: 9dc97e79e85f
Revises: 7793bfa6a037
Create Date: 2025-10-26 01:30:42.369056

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9dc97e79e85f'
down_revision: Union[str, Sequence[str], None] = '7793bfa6a037'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Drop existing trade_in_images table and recreate with correct schema
    op.drop_table('trade_in_images')
    
    # Create new trade_in_images table with correct schema
    op.create_table('trade_in_images',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('session_id', sa.Integer(), nullable=False),
    sa.Column('image_url', sa.String(length=1000), nullable=False),
    sa.Column('s3_key', sa.String(length=500), nullable=False),
    sa.Column('uploaded_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['session_id'], ['trade_in_sessions.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_trade_in_images_id'), 'trade_in_images', ['id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    # Drop new table and recreate old schema
    op.drop_table('trade_in_images')
    
    op.create_table('trade_in_images',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('trade_in_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('image_url', sa.VARCHAR(length=500), autoincrement=False, nullable=False),
    sa.Column('image_key', sa.VARCHAR(length=200), autoincrement=False, nullable=False),
    sa.Column('uploaded_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=True),
    sa.ForeignKeyConstraint(['trade_in_id'], ['trade_ins.id'], name=op.f('trade_in_images_trade_in_id_fkey')),
    sa.PrimaryKeyConstraint('id', name=op.f('trade_in_images_pkey'))
    )
    op.create_index(op.f('ix_trade_in_images_id'), 'trade_in_images', ['id'], unique=False)

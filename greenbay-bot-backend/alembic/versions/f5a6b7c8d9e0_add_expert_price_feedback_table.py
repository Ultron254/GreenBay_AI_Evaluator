"""add expert_price_feedback table

Revision ID: f5a6b7c8d9e0
Revises: a1b2c3d4e5f6
Create Date: 2026-03-16 21:30:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'f5a6b7c8d9e0'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'expert_price_feedback',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('valuation_session_id', sa.String(36), nullable=True, index=True),
        sa.Column('expert_name', sa.String(100), nullable=False),
        sa.Column('expert_price', sa.Float(), nullable=False),
        sa.Column('expert_reasoning', sa.Text(), nullable=True),
        sa.Column('product_category', sa.String(100), nullable=True, index=True),
        sa.Column('brand', sa.String(100), nullable=True, index=True),
        sa.Column('model', sa.String(200), nullable=True),
        sa.Column('condition_grade', sa.String(50), nullable=True),
        sa.Column('age_years', sa.Float(), nullable=True),
        sa.Column('system_price', sa.Float(), nullable=True),
        sa.Column('price_difference', sa.Float(), nullable=True),
        sa.Column('images_json', sa.JSON(), nullable=True),
        sa.Column('specs_json', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_expert_price_feedback_id', 'expert_price_feedback', ['id'])


def downgrade() -> None:
    op.drop_index('ix_expert_price_feedback_id', 'expert_price_feedback')
    op.drop_table('expert_price_feedback')

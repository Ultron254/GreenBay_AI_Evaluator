"""merge evaluator_tables and expert_price_feedback heads

Revision ID: merge_eval_expert
Revises: a1b2c3d4e5f6, f5a6b7c8d9e0
Create Date: 2026-05-25 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "merge_eval_expert"
down_revision = ("a1b2c3d4e5f6", "f5a6b7c8d9e0")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

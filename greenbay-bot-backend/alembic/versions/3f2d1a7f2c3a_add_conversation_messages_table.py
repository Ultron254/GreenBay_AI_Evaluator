"""add conversation_messages table

Revision ID: 3f2d1a7f2c3a
Revises: 24c353689407
Create Date: 2025-10-29 23:50:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '3f2d1a7f2c3a'
down_revision = '24c353689407'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'conversation_messages',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('conversation_id', sa.Integer(), nullable=False),
        sa.Column('role', sa.String(length=20), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('tool_name', sa.String(length=100), nullable=True),
        sa.Column('token_count', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_conversation_messages_id'), 'conversation_messages', ['id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_conversation_messages_id'), table_name='conversation_messages')
    op.drop_table('conversation_messages')



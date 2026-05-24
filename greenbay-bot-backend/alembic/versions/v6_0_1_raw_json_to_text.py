"""v6.0.1 — change raw_json columns from json to text.

Sidesteps psycopg2 Json-adapter serialization failures by storing
the pre-serialized JSON string in a plain text column.

Revision ID: v601_json_to_text
Revises: v600_ref_tables
Create Date: 2026-05-25 00:30:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "v601_json_to_text"
down_revision = "v600_ref_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "pricing_matrix_reference",
        "raw_json",
        type_=sa.Text(),
        existing_type=sa.JSON(),
        existing_nullable=True,
        postgresql_using="raw_json::text",
    )
    op.alter_column(
        "sales_stock_reference",
        "raw_json",
        type_=sa.Text(),
        existing_type=sa.JSON(),
        existing_nullable=True,
        postgresql_using="raw_json::text",
    )


def downgrade() -> None:
    op.alter_column(
        "pricing_matrix_reference",
        "raw_json",
        type_=sa.JSON(),
        existing_type=sa.Text(),
        existing_nullable=True,
        postgresql_using="raw_json::json",
    )
    op.alter_column(
        "sales_stock_reference",
        "raw_json",
        type_=sa.JSON(),
        existing_type=sa.Text(),
        existing_nullable=True,
        postgresql_using="raw_json::json",
    )

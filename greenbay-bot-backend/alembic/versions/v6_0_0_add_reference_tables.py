"""v6.0.0 — add reference data tables and multi-country columns.

New tables: pricing_matrix_reference, sales_stock_reference
New columns on valuation_sessions: country, currency_code

Revision ID: v600_ref_tables
Revises: merge_eval_expert
Create Date: 2026-05-24 18:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "v600_ref_tables"
down_revision = "merge_eval_expert"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pricing_matrix_reference",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("brand", sa.String(200), nullable=True),
        sa.Column("model", sa.String(300), nullable=True),
        sa.Column("category", sa.String(100), nullable=True),
        sa.Column("age_band", sa.String(50), nullable=True),
        sa.Column("base_min", sa.Float(), nullable=True),
        sa.Column("base_max", sa.Float(), nullable=True),
        sa.Column("condition_grade", sa.String(10), nullable=True),
        sa.Column("recommended_min", sa.Float(), nullable=True),
        sa.Column("recommended_max", sa.Float(), nullable=True),
        sa.Column("new_price", sa.Float(), nullable=True),
        sa.Column("sheet_tab", sa.String(100), nullable=True),
        sa.Column("raw_json", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_pricing_matrix_brand", "pricing_matrix_reference", ["brand"])
    op.create_index("ix_pricing_matrix_model", "pricing_matrix_reference", ["model"])
    op.create_index("ix_pricing_matrix_category", "pricing_matrix_reference", ["category"])

    op.create_table(
        "sales_stock_reference",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("product_category", sa.String(100), nullable=True),
        sa.Column("brand_name", sa.String(200), nullable=True),
        sa.Column("product_name", sa.String(300), nullable=True),
        sa.Column("model_number", sa.String(300), nullable=True),
        sa.Column("product_quality", sa.String(50), nullable=True),
        sa.Column("purchase_cost", sa.Float(), nullable=True),
        sa.Column("selling_price", sa.Float(), nullable=True),
        sa.Column("raw_json", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_sales_stock_category", "sales_stock_reference", ["product_category"])
    op.create_index("ix_sales_stock_brand", "sales_stock_reference", ["brand_name"])
    op.create_index("ix_sales_stock_model", "sales_stock_reference", ["model_number"])

    op.add_column("valuation_sessions", sa.Column("country", sa.String(10), nullable=True))
    op.add_column("valuation_sessions", sa.Column("currency_code", sa.String(10), nullable=True))


def downgrade() -> None:
    op.drop_column("valuation_sessions", "currency_code")
    op.drop_column("valuation_sessions", "country")
    op.drop_table("sales_stock_reference")
    op.drop_table("pricing_matrix_reference")

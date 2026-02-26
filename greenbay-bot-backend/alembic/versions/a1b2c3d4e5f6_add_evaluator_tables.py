"""Add evaluator tables: pricing_policy, valuation_sessions,
negotiation_rounds, decision_ledger.

Revision ID: a1b2c3d4e5f6
Revises: d4c4bdf2b6ad
Create Date: 2026-02-24 13:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "a1b2c3d4e5f6"
down_revision = "d4c4bdf2b6ad"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -- pricing_policy -------------------------------------------------------
    op.create_table(
        "pricing_policy",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("category", sa.String(100), unique=True, nullable=False),
        sa.Column("margin_pct", sa.Float(), nullable=False),
        sa.Column("max_offer_pct", sa.Float(), nullable=False),
        sa.Column("walkaway_pct", sa.Float(), nullable=False),
        sa.Column("depreciation_year1", sa.Float(), nullable=True),
        sa.Column("depreciation_year2_3", sa.Float(), nullable=True),
        sa.Column("depreciation_year4_5", sa.Float(), nullable=True),
        sa.Column("depreciation_year6_plus", sa.Float(), nullable=True),
        sa.Column("round_step_pct", sa.Float(), nullable=True),
        sa.Column("max_negotiation_rounds", sa.Integer(), nullable=True),
        sa.Column("brand_premium_json", sa.JSON(), nullable=True),
        sa.Column("condition_multiplier_json", sa.JSON(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_pricing_policy_category", "pricing_policy", ["category"])

    # -- valuation_sessions ---------------------------------------------------
    op.create_table(
        "valuation_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "trade_in_session_id",
            sa.Integer(),
            sa.ForeignKey("trade_in_sessions.id"),
            nullable=True,
        ),
        sa.Column("category", sa.String(100), nullable=True),
        sa.Column("brand", sa.String(100), nullable=True),
        sa.Column("model", sa.String(200), nullable=True),
        sa.Column("age_years", sa.Float(), nullable=True),
        sa.Column("condition_grade", sa.String(10), nullable=True),
        sa.Column("condition_score", sa.Float(), nullable=True),
        sa.Column("defects", sa.JSON(), nullable=True),
        sa.Column("seller_asking_price", sa.Float(), nullable=True),
        sa.Column("image_quality_score", sa.Float(), nullable=True),
        sa.Column("risk_score", sa.Float(), nullable=True),
        sa.Column("retail_price", sa.Float(), nullable=True),
        sa.Column("estimated_resale_value", sa.Float(), nullable=True),
        sa.Column("confidence_score", sa.Float(), nullable=True),
        sa.Column("acquisition_ceiling", sa.Float(), nullable=True),
        sa.Column("opening_offer", sa.Float(), nullable=True),
        sa.Column("walkaway_limit", sa.Float(), nullable=True),
        sa.Column("decision", sa.String(20), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("pricing_policy_snapshot", sa.JSON(), nullable=True),
        sa.Column("comparable_data", sa.JSON(), nullable=True),
        sa.Column("final_decision", sa.String(20), nullable=True),
        sa.Column("final_offer", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_valuation_sessions_trade_in", "valuation_sessions", ["trade_in_session_id"])
    op.create_index("ix_valuation_sessions_category", "valuation_sessions", ["category"])
    op.create_index("ix_valuation_sessions_created", "valuation_sessions", ["created_at"])

    # -- negotiation_rounds ---------------------------------------------------
    op.create_table(
        "negotiation_rounds",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "valuation_session_id",
            sa.String(36),
            sa.ForeignKey("valuation_sessions.id"),
            nullable=False,
        ),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("actor", sa.String(20), nullable=False),
        sa.Column("offer_amount", sa.Float(), nullable=False),
        sa.Column("ceiling_at_time", sa.Float(), nullable=True),
        sa.Column("decision", sa.String(20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_negotiation_rounds_session", "negotiation_rounds", ["valuation_session_id"])

    # -- decision_ledger ------------------------------------------------------
    op.create_table(
        "decision_ledger",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "valuation_session_id",
            sa.String(36),
            sa.ForeignKey("valuation_sessions.id"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("actor", sa.String(20), nullable=False),
        sa.Column("data", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_decision_ledger_session", "decision_ledger", ["valuation_session_id"])
    op.create_index("ix_decision_ledger_event", "decision_ledger", ["event_type"])
    op.create_index("ix_decision_ledger_created", "decision_ledger", ["created_at"])


def downgrade() -> None:
    op.drop_table("decision_ledger")
    op.drop_table("negotiation_rounds")
    op.drop_table("valuation_sessions")
    op.drop_table("pricing_policy")

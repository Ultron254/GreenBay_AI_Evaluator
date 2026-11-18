"""v6.4.0: partner trade-in (Mika, Vision Plus).

valuation_sessions gains nine nullable partner columns: partner_id,
partner_channel, partner_shop, partner_placement, partner_qr_code,
partner_source, buy_intent_category, buy_intent_model, partner_offer.

New tables: partner_credits (the credit ledger), partner_credit_events (its
append-only audit trail), partner_qr_codes (codes made on the admin page) and
partner_funnel_events (scan to redemption counts per QR code). Shapes match
greenbay_ai_evaluator/models/partner_models.py.

Idempotent like v6.3: inspects the live schema and only adds what is missing,
so it is safe where init_db's create_all already made the tables.

Revision ID: v640_partners
Revises: v630_image_keys
Create Date: 2026-11-18 09:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "v640_partners"
down_revision = "v630_image_keys"
branch_labels = None
depends_on = None

_SESSION_COLUMNS = (
    ("partner_id", sa.String(40)),
    ("partner_channel", sa.String(40)),
    ("partner_shop", sa.String(60)),
    ("partner_placement", sa.String(60)),
    ("partner_qr_code", sa.String(24)),
    ("partner_source", sa.String(160)),
    ("buy_intent_category", sa.String(60)),
    ("buy_intent_model", sa.String(200)),
    ("partner_offer", sa.Float()),
)


def _inspector():
    return sa.inspect(op.get_bind())


def _existing_columns(table: str) -> set[str]:
    try:
        return {c["name"] for c in _inspector().get_columns(table)}
    except Exception:  # noqa: BLE001, table may not exist yet on a bare DB
        return set()


def _has_table(name: str) -> bool:
    return name in _inspector().get_table_names()


def _existing_indexes(table: str) -> set[str]:
    try:
        return {i["name"] for i in _inspector().get_indexes(table)}
    except Exception:  # noqa: BLE001
        return set()


def upgrade() -> None:
    cols = _existing_columns("valuation_sessions")
    if cols:
        for name, col_type in _SESSION_COLUMNS:
            if name not in cols:
                op.add_column("valuation_sessions", sa.Column(name, col_type, nullable=True))
        idx = _existing_indexes("valuation_sessions")
        if "ix_valuation_sessions_partner_id" not in idx:
            op.create_index("ix_valuation_sessions_partner_id", "valuation_sessions", ["partner_id"])
        if "ix_valuation_sessions_partner_qr_code" not in idx:
            op.create_index("ix_valuation_sessions_partner_qr_code", "valuation_sessions", ["partner_qr_code"])

    if not _has_table("partner_credits"):
        op.create_table(
            "partner_credits",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("partner_id", sa.String(40), nullable=False),
            sa.Column("valuation_session_id", sa.String(36), nullable=False),
            sa.Column("reference", sa.String(16), nullable=False),
            sa.Column("voucher_code", sa.String(24), nullable=True),
            sa.Column("status", sa.String(24), nullable=False),
            sa.Column("customer_name", sa.String(100), nullable=True),
            sa.Column("customer_phone", sa.String(20), nullable=True),
            sa.Column("currency", sa.String(8), nullable=False, server_default="KES"),
            sa.Column("offered_amount", sa.Float(), nullable=False),
            sa.Column("agreed_amount", sa.Float(), nullable=True),
            sa.Column("issued_amount", sa.Float(), nullable=True),
            sa.Column("redeemed_amount", sa.Float(), nullable=False, server_default="0"),
            sa.Column("category", sa.String(100), nullable=True),
            sa.Column("brand", sa.String(100), nullable=True),
            sa.Column("model", sa.String(200), nullable=True),
            sa.Column("is_nominal", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("bulky", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("channel", sa.String(40), nullable=True),
            sa.Column("shop", sa.String(60), nullable=True),
            sa.Column("qr_code", sa.String(24), nullable=True),
            sa.Column("source_tag", sa.String(160), nullable=True),
            sa.Column("buy_intent_category", sa.String(60), nullable=True),
            sa.Column("buy_intent_model", sa.String(200), nullable=True),
            sa.Column("validation_method", sa.String(24), nullable=True),
            sa.Column("offered_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("validation_started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_partner_credits_partner_id", "partner_credits", ["partner_id"])
        op.create_index("ix_partner_credits_valuation_session_id", "partner_credits",
                        ["valuation_session_id"], unique=True)
        op.create_index("ix_partner_credits_reference", "partner_credits", ["reference"], unique=True)
        op.create_index("ix_partner_credits_voucher_code", "partner_credits", ["voucher_code"], unique=True)
        op.create_index("ix_partner_credits_status", "partner_credits", ["status"])
        op.create_index("ix_partner_credits_customer_phone", "partner_credits", ["customer_phone"])
        op.create_index("ix_partner_credits_partner_status", "partner_credits", ["partner_id", "status"])

    if not _has_table("partner_credit_events"):
        op.create_table(
            "partner_credit_events",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("credit_id", sa.String(36), nullable=False),
            sa.Column("partner_id", sa.String(40), nullable=False),
            sa.Column("event_type", sa.String(32), nullable=False),
            sa.Column("from_status", sa.String(24), nullable=True),
            sa.Column("to_status", sa.String(24), nullable=True),
            sa.Column("amount", sa.Float(), nullable=True),
            sa.Column("actor", sa.String(100), nullable=False),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("data", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_partner_credit_events_credit_id", "partner_credit_events", ["credit_id"])
        op.create_index("ix_partner_credit_events_partner_id", "partner_credit_events", ["partner_id"])
        op.create_index("ix_partner_credit_events_created_at", "partner_credit_events", ["created_at"])

    if not _has_table("partner_qr_codes"):
        op.create_table(
            "partner_qr_codes",
            sa.Column("code", sa.String(24), primary_key=True),
            sa.Column("partner_id", sa.String(40), nullable=False),
            sa.Column("channel", sa.String(40), nullable=False),
            sa.Column("shop", sa.String(60), nullable=True),
            sa.Column("placement", sa.String(60), nullable=False),
            sa.Column("label", sa.String(200), nullable=True),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_by", sa.String(100), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_partner_qr_codes_partner_id", "partner_qr_codes", ["partner_id"])

    if not _has_table("partner_funnel_events"):
        op.create_table(
            "partner_funnel_events",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("partner_id", sa.String(40), nullable=False),
            sa.Column("event_type", sa.String(24), nullable=False),
            sa.Column("qr_code", sa.String(24), nullable=True),
            sa.Column("channel", sa.String(40), nullable=True),
            sa.Column("shop", sa.String(60), nullable=True),
            sa.Column("valuation_session_id", sa.String(36), nullable=True),
            sa.Column("credit_id", sa.String(36), nullable=True),
            sa.Column("amount", sa.Float(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_partner_funnel_events_partner_id", "partner_funnel_events", ["partner_id"])
        op.create_index("ix_partner_funnel_events_event_type", "partner_funnel_events", ["event_type"])
        op.create_index("ix_partner_funnel_events_qr_code", "partner_funnel_events", ["qr_code"])
        op.create_index("ix_partner_funnel_events_valuation_session_id", "partner_funnel_events",
                        ["valuation_session_id"])
        op.create_index("ix_partner_funnel_events_created_at", "partner_funnel_events", ["created_at"])


def downgrade() -> None:
    for table in ("partner_funnel_events", "partner_qr_codes", "partner_credit_events", "partner_credits"):
        if _has_table(table):
            op.drop_table(table)
    cols = _existing_columns("valuation_sessions")
    idx = _existing_indexes("valuation_sessions")
    for name in ("ix_valuation_sessions_partner_qr_code", "ix_valuation_sessions_partner_id"):
        if name in idx:
            op.drop_index(name, table_name="valuation_sessions")
    for name, _ in reversed(_SESSION_COLUMNS):
        if name in cols:
            op.drop_column("valuation_sessions", name)

"""Partner trade-in tables (v6.4, migration v640_partners).

  - partner_credits        One row per partner trade-in that reached an offer.
                           Holds the credit through its whole life.
  - partner_credit_events  Append-only audit trail: every state change, with
                           who did it, when, and the amount.
  - partner_qr_codes       QR codes and short links made on the admin page
                           (codes defined in a partner's config file also work).
  - partner_funnel_events  One row per scan, start, completion, offer, accept,
                           validation, issue and redemption, for QR performance.

Every row carries partner_id. Nothing here is ever shared between partners:
each query in partners/ledger.py filters by it.

valuation_session_id is a plain indexed column, not a foreign key, so the
ledger can be tested on its own and a purge of test evaluations never
cascades into credit history.
"""

import uuid

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.sql import func

from app.database.db import Base


class PartnerCredit(Base):
    __tablename__ = "partner_credits"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    partner_id = Column(String(40), nullable=False, index=True)
    valuation_session_id = Column(String(36), nullable=False, unique=True, index=True)

    # The customer's reference (MK-7K3Q) exists from the offer; the voucher
    # code (MIKA-7K3Q-92) only once credit is issued.
    reference = Column(String(16), nullable=False, unique=True, index=True)
    voucher_code = Column(String(24), nullable=True, unique=True, index=True)

    status = Column(String(24), nullable=False, index=True)

    customer_name = Column(String(100), nullable=True)
    customer_phone = Column(String(20), nullable=True, index=True)

    currency = Column(String(8), nullable=False, default="KES")
    offered_amount = Column(Float, nullable=False)
    agreed_amount = Column(Float, nullable=True)   # after inspection
    issued_amount = Column(Float, nullable=True)
    redeemed_amount = Column(Float, nullable=False, default=0.0)

    # Snapshot of the item and where the lead came from.
    category = Column(String(100), nullable=True)
    brand = Column(String(100), nullable=True)
    model = Column(String(200), nullable=True)
    is_nominal = Column(Boolean, nullable=False, default=False)
    bulky = Column(Boolean, nullable=False, default=False)
    channel = Column(String(40), nullable=True)
    shop = Column(String(60), nullable=True)
    qr_code = Column(String(24), nullable=True)
    source_tag = Column(String(160), nullable=True)
    buy_intent_category = Column(String(60), nullable=True)
    buy_intent_model = Column(String(200), nullable=True)

    validation_method = Column(String(24), nullable=True)  # technician | drop_greenbay | drop_partner

    offered_at = Column(DateTime(timezone=True), nullable=True)
    accepted_at = Column(DateTime(timezone=True), nullable=True)
    validation_started_at = Column(DateTime(timezone=True), nullable=True)
    validated_at = Column(DateTime(timezone=True), nullable=True)
    issued_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_partner_credits_partner_status", "partner_id", "status"),
    )

    @property
    def balance(self) -> float:
        if self.issued_amount is None:
            return 0.0
        return round(float(self.issued_amount) - float(self.redeemed_amount or 0), 2)


class PartnerCreditEvent(Base):
    __tablename__ = "partner_credit_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    credit_id = Column(String(36), nullable=False, index=True)
    partner_id = Column(String(40), nullable=False, index=True)
    event_type = Column(String(32), nullable=False)
    from_status = Column(String(24), nullable=True)
    to_status = Column(String(24), nullable=True)
    amount = Column(Float, nullable=True)
    actor = Column(String(100), nullable=False)
    note = Column(Text, nullable=True)
    data = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class PartnerQrCode(Base):
    __tablename__ = "partner_qr_codes"

    code = Column(String(24), primary_key=True)
    partner_id = Column(String(40), nullable=False, index=True)
    channel = Column(String(40), nullable=False)
    shop = Column(String(60), nullable=True)
    placement = Column(String(60), nullable=False)
    label = Column(String(200), nullable=True)
    active = Column(Boolean, nullable=False, default=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class PartnerFunnelEvent(Base):
    __tablename__ = "partner_funnel_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    partner_id = Column(String(40), nullable=False, index=True)
    event_type = Column(String(24), nullable=False, index=True)
    qr_code = Column(String(24), nullable=True, index=True)
    channel = Column(String(40), nullable=True)
    shop = Column(String(60), nullable=True)
    valuation_session_id = Column(String(36), nullable=True, index=True)
    credit_id = Column(String(36), nullable=True)
    amount = Column(Float, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

"""
SQLAlchemy models for the GreenBay AI Evaluator module.

Tables:
  - pricing_policy        — Category-level business rules (admin-configurable)
  - valuation_sessions    — One per evaluation request
  - negotiation_rounds    — Append-only log of each negotiation step
  - decision_ledger       — Immutable audit trail
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    Boolean,
    DateTime,
    Text,
    ForeignKey,
    JSON,
    Index,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database.db import Base


# ---------------------------------------------------------------------------
# PricingPolicy — category-level business rules
# ---------------------------------------------------------------------------
class PricingPolicy(Base):
    """Category-level pricing rules used by the offer engine."""

    __tablename__ = "pricing_policy"

    id = Column(Integer, primary_key=True, index=True)
    category = Column(String(100), unique=True, nullable=False, index=True)

    # Margin & offer bounds (as decimals, e.g. 0.30 = 30%)
    margin_pct = Column(Float, nullable=False)
    max_offer_pct = Column(Float, nullable=False)
    walkaway_pct = Column(Float, nullable=False)

    # Depreciation rates (annual)
    depreciation_year1 = Column(Float, nullable=True)
    depreciation_year2_3 = Column(Float, nullable=True)
    depreciation_year4_5 = Column(Float, nullable=True)
    depreciation_year6_plus = Column(Float, nullable=True)

    # Negotiation parameters
    round_step_pct = Column(Float, nullable=True)
    max_negotiation_rounds = Column(Integer, nullable=True)

    # JSON lookup tables
    brand_premium_json = Column(JSON, nullable=True)
    condition_multiplier_json = Column(JSON, nullable=True)

    # Lifecycle
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


# ---------------------------------------------------------------------------
# ValuationSession — one per evaluation request
# ---------------------------------------------------------------------------
class ValuationSession(Base):
    """Captures the full deterministic valuation for a trade-in session."""

    __tablename__ = "valuation_sessions"

    id = Column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        index=True,
    )
    trade_in_session_id = Column(
        Integer,
        ForeignKey("trade_in_sessions.id"),
        nullable=True,
        index=True,
    )

    # Product info
    category = Column(String(100), nullable=True)
    brand = Column(String(100), nullable=True)
    model = Column(String(200), nullable=True)
    age_years = Column(Float, nullable=True)

    # Condition
    condition_grade = Column(String(10), nullable=True)  # A / B / C / D
    condition_score = Column(Float, nullable=True)  # 0-100
    defects = Column(JSON, nullable=True)

    # Seller
    seller_asking_price = Column(Float, nullable=True)

    # Service scores
    image_quality_score = Column(Float, nullable=True)  # 0-100
    risk_score = Column(Float, nullable=True)  # 0-100

    # Valuation results
    retail_price = Column(Float, nullable=True)
    estimated_resale_value = Column(Float, nullable=True)
    confidence_score = Column(Float, nullable=True)  # 0-100
    acquisition_ceiling = Column(Float, nullable=True)
    opening_offer = Column(Float, nullable=True)
    walkaway_limit = Column(Float, nullable=True)

    # Decision
    decision = Column(String(20), nullable=True)  # accept / negotiate / decline / review
    decision_reason = Column(Text, nullable=True)

    # Snapshots for auditability
    pricing_policy_snapshot = Column(JSON, nullable=True)
    comparable_data = Column(JSON, nullable=True)

    # Final negotiation outcome (set when negotiation concludes)
    final_decision = Column(String(20), nullable=True)  # accept | decline
    final_offer = Column(Float, nullable=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    trade_in_session = relationship("TradeInSession", foreign_keys=[trade_in_session_id])
    negotiation_rounds = relationship(
        "NegotiationRound",
        back_populates="valuation_session",
        cascade="all, delete-orphan",
        order_by="NegotiationRound.round_number",
    )
    ledger_entries = relationship(
        "DecisionLedger",
        back_populates="valuation_session",
        cascade="all, delete-orphan",
        order_by="DecisionLedger.created_at",
    )

    __table_args__ = (
        Index("ix_valuation_sessions_category", "category"),
        Index("ix_valuation_sessions_created", "created_at"),
    )


# ---------------------------------------------------------------------------
# NegotiationRound — append-only per-round record
# ---------------------------------------------------------------------------
class NegotiationRound(Base):
    """Single offer/counter round within a negotiation."""

    __tablename__ = "negotiation_rounds"

    id = Column(Integer, primary_key=True, index=True)
    valuation_session_id = Column(
        String(36),
        ForeignKey("valuation_sessions.id"),
        nullable=False,
        index=True,
    )

    round_number = Column(Integer, nullable=False)
    actor = Column(String(20), nullable=False)  # "system" | "seller"
    offer_amount = Column(Float, nullable=False)
    ceiling_at_time = Column(Float, nullable=True)

    decision = Column(String(20), nullable=False)  # accept / counter / decline / review
    reason = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    valuation_session = relationship("ValuationSession", back_populates="negotiation_rounds")


# ---------------------------------------------------------------------------
# DecisionLedger — immutable audit trail
# ---------------------------------------------------------------------------
class DecisionLedger(Base):
    """Immutable event log for every decision in the valuation lifecycle."""

    __tablename__ = "decision_ledger"

    id = Column(Integer, primary_key=True, index=True)
    valuation_session_id = Column(
        String(36),
        ForeignKey("valuation_sessions.id"),
        nullable=False,
        index=True,
    )

    event_type = Column(String(50), nullable=False)
    # evaluation_created, offer_made, counter_received, counter_offered,
    # accepted, declined, escalated

    actor = Column(String(20), nullable=False)  # "system" | "seller" | "admin"
    data = Column(JSON, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    valuation_session = relationship("ValuationSession", back_populates="ledger_entries")

    __table_args__ = (
        Index("ix_decision_ledger_event", "event_type"),
        Index("ix_decision_ledger_created", "created_at"),
    )

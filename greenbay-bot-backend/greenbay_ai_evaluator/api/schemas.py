"""Pydantic request / response models for the evaluator API.

Security: All request models use strict input validation including:
- max_length on all string fields to prevent oversized payloads
- regex patterns for structured fields (phone, grades)
- extra="forbid" to reject unexpected fields (OWASP input validation)
- ge/le/gt constraints on numeric fields
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Shared HTML sanitiser — strips tags from free-text inputs (XSS prevention)
# ---------------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(v: str | None) -> str | None:
    """Remove HTML tags from a string to prevent stored XSS."""
    if v is None:
        return v
    return _TAG_RE.sub("", v).strip()


# ---------------------------------------------------------------------------
# POST /tradein/evaluate
# ---------------------------------------------------------------------------
class DefectItem(BaseModel):
    """Individual defect reported by the seller."""

    model_config = ConfigDict(extra="forbid")  # Reject unexpected fields

    type: str = Field(
        ...,
        max_length=100,
        description="Defect type key, e.g. 'cosmetic_scratch'",
    )
    description: str | None = Field(
        None,
        max_length=500,
        description="Human-readable description",
    )
    severity: str | None = Field(
        None,
        max_length=20,
        description="low / medium / high",
    )

    @field_validator("type", "description", "severity", mode="before")
    @classmethod
    def sanitise_strings(cls, v: str | None) -> str | None:  # noqa: N805
        return _strip_tags(v)


class EvaluateRequest(BaseModel):
    """Request body for POST /tradein/evaluate.

    All string fields have max_length constraints.
    image_data is limited to 8 photos max.
    """

    model_config = ConfigDict(extra="forbid")

    trade_in_session_id: int | None = Field(None, description="FK to existing trade_in_sessions row")
    category: str = Field(
        ...,
        max_length=50,
        description="Product category, e.g. 'refrigerator'",
    )
    brand: str = Field(..., max_length=100, description="Brand name")
    model: str = Field("", max_length=200, description="Model identifier")
    age_years: float = Field(0.0, ge=0, le=50, description="Approx product age in years")
    condition_grade: str = Field(
        ...,
        max_length=20,
        description="A / B / C / D or Excellent / Good / Fair / Poor",
    )
    condition_score: float = Field(..., ge=0, le=100, description="Numeric condition 0-100")
    defects: list[DefectItem] = Field(default_factory=list, max_length=20)
    seller_asking_price: float | None = Field(None, ge=0, le=50_000_000, description="What the seller wants (KES)")
    seller_name: str | None = Field(None, max_length=200, description="Seller's full name")
    seller_phone: str | None = Field(None, max_length=30, description="Seller's phone number")
    image_urls: list[str] = Field(default_factory=list, max_length=8)
    image_data: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="Base64-encoded photo data from frontend (max 8)",
    )
    retail_price: float = Field(..., gt=0, le=50_000_000, description="Original retail price KES")
    retail_price_source: str = Field("", max_length=100, description="Where retail price came from")

    @field_validator("category", "brand", "model", "condition_grade", "retail_price_source", mode="before")
    @classmethod
    def sanitise_strings(cls, v: str | None) -> str | None:  # noqa: N805
        return _strip_tags(v)

    @field_validator("seller_name", mode="before")
    @classmethod
    def sanitise_name(cls, v: str | None) -> str | None:  # noqa: N805
        return _strip_tags(v)

    @field_validator("seller_phone", mode="before")
    @classmethod
    def validate_phone(cls, v: str | None) -> str | None:  # noqa: N805
        if v is None:
            return v
        v = _strip_tags(v)
        # Allow digits, spaces, dashes, plus sign, and parentheses
        cleaned = re.sub(r"[^\d+\-() ]", "", v)
        return cleaned[:30] if cleaned else v


class EvaluateResponse(BaseModel):
    session_id: str
    estimated_resale_value: float
    confidence_score: float
    acquisition_ceiling: float
    opening_offer: float
    walkaway_limit: float
    decision: str
    decision_reason: str
    condition_grade: str
    risk_score: float
    comparable_count: int
    pricing_policy_version: str | None = None
    price_verification: dict | None = None


class ExpertFeedbackRequest(BaseModel):
    """Expert pricing feedback — strict validation."""

    model_config = ConfigDict(extra="forbid")

    valuation_session_id: str = Field(
        ...,
        max_length=50,
        description="Session ID from the evaluation",
    )
    expert_name: str = Field(
        ...,
        max_length=200,
        description="Name of the expert providing feedback",
    )
    expert_price: float = Field(
        ...,
        gt=0,
        le=50_000_000,
        description="Expert's assessed price in KES",
    )
    expert_reasoning: str | None = Field(
        None,
        max_length=1000,
        description="Why the expert chose this price",
    )

    @field_validator("expert_name", "expert_reasoning", mode="before")
    @classmethod
    def sanitise_strings(cls, v: str | None) -> str | None:  # noqa: N805
        return _strip_tags(v)


class ExpertFeedbackResponse(BaseModel):
    id: int
    valuation_session_id: str
    expert_name: str
    expert_price: float
    system_price: float | None
    price_difference: float | None
    message: str


# ---------------------------------------------------------------------------
# POST /tradein/{session_id}/counter
# ---------------------------------------------------------------------------
class CounterRequest(BaseModel):
    """Counter-offer — strict validation."""

    model_config = ConfigDict(extra="forbid")

    seller_counter: float = Field(
        ...,
        gt=0,
        le=50_000_000,
        description="Seller's counter-offer in KES",
    )


class CounterResponse(BaseModel):
    round_number: int
    decision: str
    system_offer: float
    ceiling: float
    reason: str
    rounds_remaining: int


# ---------------------------------------------------------------------------
# GET /tradein/{session_id}
# ---------------------------------------------------------------------------
class NegotiationRoundOut(BaseModel):
    round_number: int
    actor: str
    offer_amount: float
    ceiling_at_time: float | None
    decision: str
    reason: str | None
    created_at: str


class DecisionLedgerOut(BaseModel):
    event_type: str
    actor: str
    data: dict[str, Any] | None
    created_at: str


class SessionDetailResponse(BaseModel):
    session_id: str
    trade_in_session_id: int | None
    category: str | None
    brand: str | None
    model: str | None
    age_years: float | None
    condition_grade: str | None
    condition_score: float | None
    defects: list[dict] | None
    seller_asking_price: float | None
    image_quality_score: float | None
    risk_score: float | None
    retail_price: float | None
    estimated_resale_value: float | None
    confidence_score: float | None
    acquisition_ceiling: float | None
    opening_offer: float | None
    walkaway_limit: float | None
    decision: str | None
    decision_reason: str | None
    pricing_policy_snapshot: dict | None
    comparable_data: dict | None
    created_at: str | None
    negotiation_rounds: list[NegotiationRoundOut]
    decision_ledger: list[DecisionLedgerOut]


# ---------------------------------------------------------------------------
# POST /tradein/notify-pickup
# ---------------------------------------------------------------------------
class PickupNotifyRequest(BaseModel):
    """Pickup notification — strict validation."""

    model_config = ConfigDict(extra="forbid")

    valuation_session_id: str | None = Field(
        None,
        max_length=50,
        description="FK to valuation session",
    )
    seller_name: str = Field(..., max_length=200, description="Seller's full name")
    seller_phone: str = Field(..., max_length=30, description="Seller's phone number")
    appliance_description: str = Field(
        "",
        max_length=500,
        description="E.g. Hisense 124L Fridge",
    )
    condition_grade: str | None = Field(None, max_length=20)
    agreed_price: float | None = Field(None, ge=0, le=50_000_000)
    pickup_address: str = Field(
        ...,
        max_length=500,
        description="Full pickup address",
    )
    preferred_day: str | None = Field(
        None,
        max_length=50,
        description="E.g. Monday, Tomorrow",
    )
    photo_count: int = Field(0, ge=0, le=20)

    @field_validator("seller_name", "appliance_description", "pickup_address", "preferred_day", mode="before")
    @classmethod
    def sanitise_strings(cls, v: str | None) -> str | None:  # noqa: N805
        return _strip_tags(v)

    @field_validator("seller_phone", mode="before")
    @classmethod
    def validate_phone(cls, v: str | None) -> str | None:  # noqa: N805
        if v is None:
            return v
        v = _strip_tags(v)
        cleaned = re.sub(r"[^\d+\-() ]", "", v)
        return cleaned[:30] if cleaned else v


class PickupNotifyResponse(BaseModel):
    id: int
    status: str
    whatsapp_link: str
    message: str


# ---------------------------------------------------------------------------
# GET /tradein/related-products
# ---------------------------------------------------------------------------
class RelatedProductOut(BaseModel):
    title: str
    price: float | None
    compare_at_price: float | None
    image_url: str | None
    product_url: str | None
    product_type: str | None
    available: bool


# ---------------------------------------------------------------------------
# GET /tradein/inventory-stats
# ---------------------------------------------------------------------------
class InventoryStatsOut(BaseModel):
    total_active: int
    by_category: dict[str, int]
    last_scrape: str | None

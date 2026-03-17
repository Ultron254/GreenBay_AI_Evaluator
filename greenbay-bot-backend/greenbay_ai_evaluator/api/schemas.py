"""Pydantic request / response models for the evaluator API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# POST /tradein/evaluate
# ---------------------------------------------------------------------------
class DefectItem(BaseModel):
    type: str = Field(..., description="Defect type key, e.g. 'cosmetic_scratch'")
    description: str | None = Field(None, description="Human-readable description")
    severity: str | None = Field(None, description="low / medium / high")


class EvaluateRequest(BaseModel):
    trade_in_session_id: int | None = Field(None, description="FK to existing trade_in_sessions row")
    category: str = Field(..., description="Product category, e.g. 'refrigerator'")
    brand: str = Field(..., description="Brand name")
    model: str = Field("", description="Model identifier")
    age_years: float = Field(0.0, ge=0, description="Approx product age in years")
    condition_grade: str = Field(..., description="A / B / C / D or Excellent / Good / Fair / Poor")
    condition_score: float = Field(..., ge=0, le=100, description="Numeric condition 0-100")
    defects: list[DefectItem] = Field(default_factory=list)
    seller_asking_price: float | None = Field(None, ge=0, description="What the seller wants (KES)")
    seller_name: str | None = Field(None, description="Seller's full name")
    seller_phone: str | None = Field(None, description="Seller's phone number")
    image_urls: list[str] = Field(default_factory=list)
    image_data: list[str] = Field(default_factory=list, description="Base64-encoded photo data from frontend")
    retail_price: float = Field(..., gt=0, description="Original retail price KES")
    retail_price_source: str = Field("", description="Where retail price came from")


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
    valuation_session_id: str = Field(..., description="Session ID from the evaluation")
    expert_name: str = Field(..., description="Name of the expert providing feedback")
    expert_price: float = Field(..., gt=0, description="Expert's assessed price in KES")
    expert_reasoning: str | None = Field(None, description="Why the expert chose this price")


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
    seller_counter: float = Field(..., gt=0, description="Seller's counter-offer in KES")


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
    valuation_session_id: str | None = Field(None, description="FK to valuation session")
    seller_name: str = Field(..., description="Seller's full name")
    seller_phone: str = Field(..., description="Seller's phone number")
    appliance_description: str = Field("", description="E.g. Hisense 124L Fridge")
    condition_grade: str | None = Field(None)
    agreed_price: float | None = Field(None, ge=0)
    pickup_address: str = Field(..., description="Full pickup address")
    preferred_day: str | None = Field(None, description="E.g. Monday, Tomorrow")
    photo_count: int = Field(0, ge=0)


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

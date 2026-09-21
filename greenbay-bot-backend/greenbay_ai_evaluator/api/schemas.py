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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator


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
# Seller phone (Sep 2026): required, validated, stored normalised
# ---------------------------------------------------------------------------
# An accepted offer without a phone is not a lead anybody can call, and the
# same customer typed three ways used to be stored as three different numbers.
# Stored form: country code + national number, digits only, no "+", e.g.
# 254712345678. THE SAME RULES LIVE IN frontend/app.js (normalisePhone): the
# wizard must never submit a phone this function rejects, because a rejected
# submission is a lost evaluation. Change both together.
PHONE_ERROR_MESSAGE = (
    "Enter a valid phone number, e.g. 0712 345 678 or +254 712 345 678."
)

# country -> (calling code, national number pattern WITHOUT the leading 0)
_PHONE_RULES: dict[str, tuple[str, str]] = {
    "KE": ("254", r"[17]\d{8}"),        # 07XX XXX XXX and 01XX XXX XXX
    "UG": ("256", r"[2-9]\d{8}"),
    "NG": ("234", r"[789][01]\d{8}"),
}
_PHONE_SEPARATORS_RE = re.compile(r"[\s\-().]")


def normalise_phone(raw: Any, country: str = "KE") -> str | None:
    """Return the stored form of a seller phone, or None when it is not valid.

    Accepted, with spaces, dashes, dots or parentheses anywhere:
      - Kenya:   0712345678, 0112345678, 712345678, +254712345678,
                 254712345678, 00254712345678, +254 0712 345 678
                 -> 254712345678
      - Uganda / Nigeria: the same shapes with 256 / 234. A number written
        locally (leading 0 or bare) is read with *country*, which defaults to
        Kenya.
      - Any other country: international form only, 8 to 15 digits, stored
        as the digits: with a + or 00 prefix, or 11+ digits with no leading 0
        (a WhatsApp sender id).
    """
    if raw is None:
        return None
    text = _PHONE_SEPARATORS_RE.sub("", _strip_tags(str(raw)) or "")
    international = False
    if text.startswith("+"):
        international, text = True, text[1:]
    elif text.startswith("00"):
        international, text = True, text[2:]
    if not text.isdigit() or not text.isascii():
        return None

    # A number that carries one of our calling codes must fit that country.
    for code, national in _PHONE_RULES.values():
        if text.startswith(code) and (international or len(text) > 10):
            m = re.fullmatch(rf"{code}0?({national})", text)
            return f"{code}{m.group(1)}" if m else None

    # 11+ digits with no leading 0 cannot be a local number in KE/UG/NG, so it
    # is a full international number written without the "+". This is how the
    # WhatsApp caller (app/webhooks/flowcart.py) sends the sender's id.
    if international or (len(text) >= 11 and not text.startswith("0")):
        return text if 8 <= len(text) <= 15 and not text.startswith("0") else None

    code, national = _PHONE_RULES.get((country or "KE").upper().strip(), _PHONE_RULES["KE"])
    m = re.fullmatch(rf"0?({national})", text)
    return f"{code}{m.group(1)}" if m else None


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


# Storage limits for the attribution fields; they match the column widths on
# valuation_sessions (migration v630_attribution). Longer values are cut, not
# rejected.
ATTRIBUTION_MAX_LEN = {
    "utm_source": 200,
    "utm_medium": 200,
    "utm_campaign": 200,
    "utm_content": 200,
    "referrer": 500,
    "landing_url": 2000,
}


# Query parameters that may be kept on a stored landing URL: campaign tags and
# ad-click ids. Everything else is dropped, because a URL can carry anything:
# the ops dashboard is opened as dashboard.html?key=<admin key>, and a link in
# a message to a customer could carry a name or a phone. THE SAME LIST LIVES IN
# frontend/index.html (gbCleanUrl), which cleans what is sent to GA4.
ATTRIBUTION_QUERY_ALLOWLIST = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "utm_id",
    "gclid", "gbraid", "wbraid", "fbclid", "ttclid", "msclkid",
})
_SIMPLE_FRAGMENT_RE = re.compile(r"[A-Za-z0-9_\-]{0,64}")


def clean_attribution_url(url: str | None, *, keep_query: bool) -> str | None:
    """Reduce a URL to what attribution needs and nothing personal or secret.

    Landing URL (*keep_query* True): scheme, host, path, allow-listed query
    parameters, and a simple fragment such as ``#evaluate``. Referrer
    (*keep_query* False): scheme, host and path only. User-info
    (``user:pass@``) is always dropped. Never raises; an unparsable value is
    cut at the first ``?`` or ``#``.
    """
    if not url:
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        query = ""
        fragment = ""
        if keep_query:
            query = urlencode([
                (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
                if k.lower() in ATTRIBUTION_QUERY_ALLOWLIST
            ])
            if _SIMPLE_FRAGMENT_RE.fullmatch(parts.fragment or ""):
                fragment = parts.fragment
        return urlunsplit((parts.scheme, host, parts.path, query, fragment)) or None
    except ValueError:
        return re.split(r"[?#]", url, maxsplit=1)[0] or None


class EvaluationAttribution(BaseModel):
    """Campaign attribution captured by the frontend on first load.

    Every field is optional. Values are tag-stripped and truncated rather than
    rejected: attribution is analytics metadata and must never make an
    evaluation fail. Unknown keys are ignored for the same reason.
    """

    model_config = ConfigDict(extra="ignore")

    utm_source: str | None = Field(None, description="utm_source from the landing query string")
    utm_medium: str | None = Field(None, description="utm_medium from the landing query string")
    utm_campaign: str | None = Field(None, description="utm_campaign from the landing query string")
    utm_content: str | None = Field(None, description="utm_content from the landing query string")
    referrer: str | None = Field(None, description="document.referrer on first load")
    landing_url: str | None = Field(None, description="location.href on first load")

    @field_validator(
        "utm_source", "utm_medium", "utm_campaign", "utm_content", "referrer", "landing_url",
        mode="before",
    )
    @classmethod
    def sanitise(cls, v: Any, info: ValidationInfo) -> str | None:  # noqa: N805
        if v is None:
            return None
        if not isinstance(v, str):
            v = str(v)
        v = _strip_tags(v) or ""
        # Defence in depth: the frontend cleans these too, but a cached older
        # app.js (or any other client) sends the raw URL.
        if info.field_name == "referrer":
            v = clean_attribution_url(v, keep_query=False) or ""
        elif info.field_name == "landing_url":
            v = clean_attribution_url(v, keep_query=True) or ""
        v = v[:ATTRIBUTION_MAX_LEN.get(info.field_name or "", 200)]
        return v or None


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
    # v6.1 — appliance size (drives accurate per-size pricing). Optional: if the
    # user doesn't know, the backend infers it from the model number / photos.
    size_value: float | None = Field(
        None, ge=0, le=10_000,
        description="Numeric size: TV inches, washer kg, fridge/freezer litres",
    )
    size_unit: str | None = Field(
        None, max_length=20,
        description="Unit for size_value: 'inch' | 'kg' | 'litre'",
    )
    condition_grade: str = Field(
        ...,
        max_length=20,
        description="A / B / C / D or Excellent / Good / Fair / Poor",
    )
    condition_score: float = Field(..., ge=0, le=100, description="Numeric condition 0-100")
    defects: list[DefectItem] = Field(default_factory=list, max_length=20)
    seller_asking_price: float | None = Field(None, ge=0, le=50_000_000, description="What the seller wants (KES)")
    seller_name: str | None = Field(None, max_length=200, description="Seller's full name")
    seller_phone: str = Field(
        ...,
        max_length=30,
        description=(
            "Seller's phone number. REQUIRED (Sep 2026): the offer is only shown "
            "to a customer we can call back. Accepts 07.., 01.., +254.., 254.. "
            "with spaces or dashes; stored normalised as 2547XXXXXXXX."
        ),
    )
    image_urls: list[str] = Field(default_factory=list, max_length=8)
    image_data: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="Base64-encoded photo data from frontend (max 8)",
    )
    retail_price: float = Field(..., gt=0, le=50_000_000, description="Original retail price KES")
    retail_price_source: str = Field("", max_length=100, description="Where retail price came from")
    country: str = Field("KE", max_length=5, description="Country code: KE, UG, NG")
    # Campaign attribution (utm_*, referrer, landing URL). Optional and additive:
    # older clients that omit it are unaffected.
    attribution: EvaluationAttribution | None = Field(
        None, description="utm_* / referrer / landing_url captured by the frontend on first load",
    )

    @field_validator("category", "brand", "model", "condition_grade", "retail_price_source", "size_unit", mode="before")
    @classmethod
    def sanitise_strings(cls, v: str | None) -> str | None:  # noqa: N805
        return _strip_tags(v)

    @field_validator("seller_name", mode="before")
    @classmethod
    def sanitise_name(cls, v: str | None) -> str | None:  # noqa: N805
        return _strip_tags(v)

    @field_validator("seller_phone", mode="before")
    @classmethod
    def validate_phone(cls, v: Any) -> str:  # noqa: N805
        # Missing, null and blank all get the same customer-readable message.
        if v is None or not str(v).strip():
            raise ValueError(PHONE_ERROR_MESSAGE)
        return str(v).strip()

    @model_validator(mode="after")
    def normalise_seller_phone(self) -> "EvaluateRequest":
        # Runs after the fields so the local format can be read with `country`.
        normalised = normalise_phone(self.seller_phone, self.country)
        if not normalised:
            raise ValueError(f"seller_phone: {PHONE_ERROR_MESSAGE}")
        self.seller_phone = normalised
        return self


class EvaluateResponse(BaseModel):
    session_id: str
    estimated_resale_value: float
    confidence_score: float
    acquisition_ceiling: float
    opening_offer: float
    walkaway_limit: float
    decision: str  # "accept" | "negotiate" | "decline" | "review" | "reject" | "redirect_to_agents"
    decision_reason: str
    condition_grade: str
    risk_score: float
    comparable_count: int
    pricing_policy_version: str | None = None
    price_verification: dict | None = None
    # v6: Multi-country
    currency_code: str = "KES"
    country: str = "KE"
    # v6: Customer-facing message (used when confidence < 80%)
    customer_message: str | None = None
    # CR-7: Per-image rejection details
    rejected_images: list[dict] | None = None
    # CR-3: Agent redirect info
    redirect_info: dict | None = None
    # CR-1: Video analysis results
    video_analysis: dict | None = None


# ---------------------------------------------------------------------------
# v6: POST /tradein/{session_id}/accept-offer
# ---------------------------------------------------------------------------
class AcceptOfferResponse(BaseModel):
    session_id: str
    decision: str
    message: str


# ---------------------------------------------------------------------------
# v6: POST /tradein/{session_id}/rejection-choice
# ---------------------------------------------------------------------------
class RejectionChoiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    option: str = Field(
        ...,
        max_length=1,
        description="A (consignment), B (10/90 split), or C (talk to team)",
    )
    customer_asking_price: float | None = Field(
        None, ge=0, le=50_000_000,
        description="Customer's desired price (for consignment option A)",
    )

    @field_validator("option", mode="before")
    @classmethod
    def validate_option(cls, v: str) -> str:
        if v and v.upper() in ("A", "B", "C"):
            return v.upper()
        raise ValueError("option must be A, B, or C")


class RejectionChoiceResponse(BaseModel):
    session_id: str
    option: str
    message: str
    upfront_amount: float | None = None
    balance_amount: float | None = None


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

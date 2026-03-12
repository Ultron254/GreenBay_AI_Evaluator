"""
Risk and fraud scoring service — real implementation.

Detects potential fraud patterns:
  1. Duplicate sessions: same phone + category within N days
  2. Rapid submissions: too many sessions from same phone in short time
  3. Suspicious pricing: asking price far above retail
  4. Session anomalies: missing data patterns common in fraud

Scoring (0-100, higher = riskier):
  - 0-20: Low risk (normal behavior)
  - 21-50: Medium risk (some flags, proceed with caution)
  - 51-70: High risk (needs manual review)
  - 71-100: Critical risk (likely fraud, auto-decline)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from loguru import logger


@dataclass
class RiskResult:
    """Result of the risk/fraud assessment."""

    score: float  # 0-100 (higher = riskier)
    needs_review: bool
    method: str  # "rule_based" | "default"
    flags: list[str]


def assess_risk(
    *,
    phone_number: str = "",
    image_urls: list[str] | None = None,
    image_data: list[str] | None = None,
    category: str = "",
    seller_asking_price: float | None = None,
    retail_price: float | None = None,
    condition_grade: str = "",
    age_years: float = 0,
    db_session=None,
) -> RiskResult:
    """Return a risk score for the trade-in submission.

    Combines multiple rule-based heuristics to detect suspicious
    trade-in submissions.

    Parameters
    ----------
    phone_number : str
        Seller's phone number.
    image_urls : list[str], optional
        Uploaded image URLs.
    image_data : list[str], optional
        Base64-encoded image data.
    category : str
        Product category.
    seller_asking_price : float, optional
        What the seller is asking.
    retail_price : float, optional
        Original retail price.
    condition_grade : str
        A/B/C/D condition grade.
    age_years : float
        Product age.
    db_session : optional
        Active SQLAlchemy session for DB lookups.

    Returns
    -------
    RiskResult
    """
    risk_score = 0.0
    flags: list[str] = []

    # --- Rule 1: Duplicate session detection ---
    if db_session and phone_number:
        risk_score, flags = _check_duplicates(
            db_session, phone_number, category, risk_score, flags
        )

    # --- Rule 2: Suspicious pricing ---
    if seller_asking_price and retail_price and retail_price > 0:
        price_ratio = seller_asking_price / retail_price
        if price_ratio > 1.2:
            # Asking more than 120% of retail — very suspicious
            risk_score += 25.0
            flags.append(
                f"Asking price ({price_ratio:.0%} of retail) exceeds original retail value"
            )
        elif price_ratio > 0.9:
            # Asking 90%+ of retail for a used item — suspicious
            risk_score += 10.0
            flags.append(
                f"Asking price ({price_ratio:.0%} of retail) is unusually high for used item"
            )

    # --- Rule 3: Age vs condition mismatch ---
    if age_years and condition_grade:
        grade_upper = condition_grade.upper().strip()
        if age_years >= 5 and grade_upper == "A":
            risk_score += 15.0
            flags.append(
                f"Claims 'Excellent' condition for {age_years:.0f}-year-old product"
            )
        elif age_years >= 8 and grade_upper in ("A", "B"):
            risk_score += 10.0
            flags.append(
                f"Claims '{grade_upper}' grade for {age_years:.0f}-year-old product"
            )

    # --- Rule 4: No images provided ---
    total_images = len(image_urls or []) + len(image_data or [])
    if total_images == 0:
        risk_score += 10.0
        flags.append("No images provided — cannot verify product visually")
    elif total_images < 2:
        risk_score += 5.0
        flags.append("Only 1 image — limited visual verification")

    # --- Rule 5: Missing critical data ---
    missing_count = 0
    if not phone_number:
        missing_count += 1
    if not category:
        missing_count += 1
    if seller_asking_price is None or seller_asking_price <= 0:
        missing_count += 1

    if missing_count >= 2:
        risk_score += 10.0
        flags.append(f"Multiple data fields missing ({missing_count} fields)")

    # Cap at 100
    risk_score = min(100.0, risk_score)
    needs_review = risk_score > 50

    if flags:
        logger.info(
            f"Risk assessment: score={risk_score:.0f}, "
            f"flags={len(flags)}, review={needs_review}"
        )
    else:
        logger.debug(f"Risk assessment: score={risk_score:.0f}, no flags")

    return RiskResult(
        score=round(risk_score, 1),
        needs_review=needs_review,
        method="rule_based" if flags else "default",
        flags=flags,
    )


def _check_duplicates(
    db_session,
    phone_number: str,
    category: str,
    risk_score: float,
    flags: list[str],
) -> tuple[float, list[str]]:
    """Check for duplicate or rapid submissions from the same phone."""
    try:
        from app.database.models import TradeInSession

        now = datetime.now(timezone.utc)

        # Check sessions in the last 7 days from same phone
        week_ago = now - timedelta(days=7)
        recent_sessions = (
            db_session.query(TradeInSession)
            .filter(
                TradeInSession.phone_number == phone_number,
                TradeInSession.created_at >= week_ago,
            )
            .order_by(TradeInSession.created_at.desc())
            .all()
        )

        if not recent_sessions:
            return risk_score, flags

        session_count = len(recent_sessions)

        # Too many sessions in a week
        if session_count >= 10:
            risk_score += 30.0
            flags.append(
                f"Excessive submissions: {session_count} sessions in past 7 days"
            )
        elif session_count >= 5:
            risk_score += 15.0
            flags.append(
                f"High submission volume: {session_count} sessions in past 7 days"
            )

        # Duplicate category within 3 days
        if category:
            three_days_ago = now - timedelta(days=3)
            cat_lower = category.lower().strip()
            same_category = [
                s
                for s in recent_sessions
                if s.created_at >= three_days_ago
                and s.detected_category
                and cat_lower in s.detected_category.lower()
            ]
            if same_category:
                risk_score += 20.0
                flags.append(
                    f"Duplicate submission: same category '{category}' "
                    f"submitted {len(same_category)} time(s) in past 3 days"
                )

        # Rapid-fire submissions (more than 3 in last hour)
        hour_ago = now - timedelta(hours=1)
        recent_hour = [
            s for s in recent_sessions if s.created_at >= hour_ago
        ]
        if len(recent_hour) >= 3:
            risk_score += 20.0
            flags.append(
                f"Rapid submissions: {len(recent_hour)} sessions in the last hour"
            )

    except Exception as e:
        logger.warning(f"Duplicate check failed: {e}")

    return risk_score, flags

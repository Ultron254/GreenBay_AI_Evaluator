"""
Risk and fraud scoring service — Phase 1: default low-risk score.

# EXTENSION_POINT: Phase 2 — Duplicate image detection via perceptual hashing
#   def check_duplicate_images(image_urls: list[str], db_session) -> float:
#       '''Compute pHash for each image and compare against existing sessions.'''
#       ...
#
# EXTENSION_POINT: Phase 3 — Duplicate listing detection
#   def check_duplicate_listing(
#       phone_number: str, category: str, days: int, db_session
#   ) -> float:
#       '''Flag users submitting same category within N days.'''
#       ...
#
# EXTENSION_POINT: Phase 4 — Behavioral risk scoring
#   def score_behavior(phone_number: str, db_session) -> float:
#       '''Score based on submission frequency, rejection patterns,
#       price manipulation attempts.'''
#       ...
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger


@dataclass
class RiskResult:
    """Result of the risk/fraud assessment."""

    score: float  # 0-100 (higher = riskier)
    needs_review: bool
    method: str  # "default" | "duplicate_image" | "behavioral"
    flags: list[str]


def assess_risk(
    *,
    phone_number: str = "",
    image_urls: list[str] | None = None,
    category: str = "",
    db_session=None,
) -> RiskResult:
    """Return a risk score for the trade-in submission.

    Phase 1 always returns a default low-risk score of 10.

    Parameters
    ----------
    phone_number : str
        Seller's phone number.
    image_urls : list[str], optional
        Uploaded image URLs.
    category : str
        Product category.
    db_session : optional
        Active SQLAlchemy session for DB lookups.

    Returns
    -------
    RiskResult
    """
    # Phase 1: static low risk
    logger.debug(f"Risk assessment: default score 10 for {phone_number}")

    return RiskResult(
        score=10.0,
        needs_review=False,
        method="default",
        flags=[],
    )

"""
Image quality scoring service — Phase 1: default score.

Assesses **photo quality** (blur, lighting, resolution), NOT appliance
condition.  The existing GPT-4o vision analysis in
``complete_trade_in_assessment()`` continues to handle condition scoring.

# EXTENSION_POINT: Phase 2 — AWS Rekognition integration
#   def score_with_rekognition(image_urls: list[str]) -> float:
#       '''Call Rekognition DetectLabels + image quality metrics.'''
#       ...
#
# EXTENSION_POINT: Phase 3 — OpenCV local analysis
#   def score_with_opencv(image_bytes: list[bytes]) -> float:
#       '''Compute Laplacian variance for blur, histogram for exposure.'''
#       ...
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger


@dataclass
class ImageQualityResult:
    """Result of the image quality assessment."""

    score: float  # 0-100
    needs_review: bool
    method: str  # "default" | "rekognition" | "opencv"
    details: dict


def score_images(
    image_urls: list[str] | None = None,
) -> ImageQualityResult:
    """Return an image quality score.

    Phase 1 always returns a default score of 70 ("acceptable quality").
    This is intentionally a stub — real CV scoring is an extension point.

    Parameters
    ----------
    image_urls : list[str], optional
        URLs of uploaded images.

    Returns
    -------
    ImageQualityResult
    """
    # Phase 1: default score
    image_count = len(image_urls) if image_urls else 0

    # Slight bonus for providing multiple angles
    bonus = min(10.0, image_count * 2.0)
    score = min(100.0, 70.0 + bonus)

    logger.debug(
        f"Image quality score: {score} (default, {image_count} images)"
    )

    return ImageQualityResult(
        score=round(score, 1),
        needs_review=score < 30,
        method="default",
        details={
            "image_count": image_count,
            "note": "Phase 1 default scoring — CV integration pending",
        },
    )

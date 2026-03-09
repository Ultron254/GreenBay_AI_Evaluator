"""
Image quality scoring service — real implementation.

Assesses **photo quality** (blur, lighting, resolution) using OpenCV.
This does NOT score appliance condition — that's handled by the vision
service (Anthropic Claude).

Scoring breakdown (total 100):
  - Blur detection (Laplacian variance): 0-35 pts
  - Resolution (megapixels): 0-25 pts
  - Brightness (mean luminance): 0-20 pts
  - Contrast (std deviation): 0-10 pts
  - Image count bonus: 0-10 pts
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass, field

from loguru import logger


@dataclass
class ImageQualityResult:
    """Result of the image quality assessment."""

    score: float  # 0-100
    needs_review: bool
    method: str  # "opencv" | "default"
    details: dict


def _decode_image(data: str) -> "np.ndarray | None":
    """Decode a base64 or data-URL image string to an OpenCV ndarray."""
    try:
        import cv2
        import numpy as np

        # Strip data URL prefix if present
        if "," in data:
            data = data.split(",", 1)[1]

        raw = base64.b64decode(data)
        arr = np.frombuffer(raw, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        return img
    except Exception:
        return None


def _score_single_image(img: "np.ndarray") -> dict:
    """Score a single image on blur, resolution, brightness, contrast."""
    import cv2
    import numpy as np

    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # --- Blur (Laplacian variance) ---
    # Higher variance = sharper image.  Typical range: 10-2000+
    lap_var = cv2.Laplacian(gray, cv2.CV_64F).var()
    if lap_var >= 500:
        blur_score = 35.0
    elif lap_var >= 200:
        blur_score = 30.0
    elif lap_var >= 100:
        blur_score = 25.0
    elif lap_var >= 50:
        blur_score = 18.0
    elif lap_var >= 20:
        blur_score = 10.0
    else:
        blur_score = 3.0  # Very blurry

    # --- Resolution ---
    megapixels = (h * w) / 1_000_000
    if megapixels >= 2.0:
        res_score = 25.0
    elif megapixels >= 1.0:
        res_score = 20.0
    elif megapixels >= 0.5:
        res_score = 15.0
    elif megapixels >= 0.3:
        res_score = 10.0
    else:
        res_score = 5.0  # Very low res

    # --- Brightness (mean luminance) ---
    mean_brightness = float(np.mean(gray))
    # Good range: 80-180. Too dark (<50) or too bright (>220) is bad.
    if 80 <= mean_brightness <= 180:
        bright_score = 20.0
    elif 60 <= mean_brightness <= 200:
        bright_score = 15.0
    elif 40 <= mean_brightness <= 220:
        bright_score = 10.0
    else:
        bright_score = 4.0  # Very dark or blown out

    # --- Contrast (std deviation of pixel values) ---
    std_dev = float(np.std(gray))
    if std_dev >= 50:
        contrast_score = 10.0
    elif std_dev >= 30:
        contrast_score = 7.0
    elif std_dev >= 15:
        contrast_score = 4.0
    else:
        contrast_score = 1.0  # Very flat / low contrast

    total = blur_score + res_score + bright_score + contrast_score

    return {
        "width": w,
        "height": h,
        "megapixels": round(megapixels, 2),
        "laplacian_variance": round(lap_var, 1),
        "blur_score": blur_score,
        "mean_brightness": round(mean_brightness, 1),
        "brightness_score": bright_score,
        "std_deviation": round(std_dev, 1),
        "contrast_score": contrast_score,
        "resolution_score": res_score,
        "per_image_score": round(total, 1),
    }


def score_images(
    image_urls: list[str] | None = None,
    image_data: list[str] | None = None,
) -> ImageQualityResult:
    """Return an image quality score using OpenCV analysis.

    Analyzes blur, resolution, brightness, and contrast for each image.
    Falls back to a default score if OpenCV is unavailable or no images
    can be decoded.

    Parameters
    ----------
    image_urls : list[str], optional
        URLs of uploaded images (used for count only in Phase 1).
    image_data : list[str], optional
        Base64-encoded image data for real analysis.

    Returns
    -------
    ImageQualityResult
    """
    # Collect all image sources
    base64_images = list(image_data or [])

    # If no base64 data, fall back to default scoring based on count
    if not base64_images:
        image_count = len(image_urls or [])
        bonus = min(10.0, image_count * 2.0)
        score = min(100.0, 70.0 + bonus)
        logger.debug(f"Image quality: default score {score} ({image_count} URL-only images)")
        return ImageQualityResult(
            score=round(score, 1),
            needs_review=score < 30,
            method="default",
            details={
                "image_count": image_count,
                "note": "No base64 data provided, using default scoring",
            },
        )

    # Try OpenCV analysis
    try:
        import cv2  # noqa: F401
    except ImportError:
        logger.warning("OpenCV not installed, falling back to default scoring")
        bonus = min(10.0, len(base64_images) * 2.0)
        score = min(100.0, 70.0 + bonus)
        return ImageQualityResult(
            score=round(score, 1),
            needs_review=score < 30,
            method="default",
            details={"note": "OpenCV not available"},
        )

    # Analyze each image
    per_image_details = []
    per_image_scores = []

    for i, data in enumerate(base64_images[:8]):  # Cap at 8 images
        img = _decode_image(data)
        if img is None:
            per_image_details.append({"index": i, "error": "Failed to decode"})
            continue

        detail = _score_single_image(img)
        detail["index"] = i
        per_image_details.append(detail)
        per_image_scores.append(detail["per_image_score"])

    if not per_image_scores:
        # All images failed to decode
        return ImageQualityResult(
            score=50.0,
            needs_review=True,
            method="opencv",
            details={
                "image_count": len(base64_images),
                "decoded_count": 0,
                "note": "All images failed to decode",
            },
        )

    # Average the per-image scores (out of 90 max per image)
    avg_score = sum(per_image_scores) / len(per_image_scores)

    # Multi-angle bonus (up to 10 pts): more photos = better coverage
    count_bonus = min(10.0, len(per_image_scores) * 2.5)
    final_score = min(100.0, avg_score + count_bonus)

    logger.info(
        f"Image quality: {final_score:.1f}/100 "
        f"({len(per_image_scores)} images analyzed, "
        f"avg {avg_score:.1f}, bonus +{count_bonus:.1f})"
    )

    return ImageQualityResult(
        score=round(final_score, 1),
        needs_review=final_score < 30,
        method="opencv",
        details={
            "image_count": len(base64_images),
            "decoded_count": len(per_image_scores),
            "average_per_image": round(avg_score, 1),
            "count_bonus": count_bonus,
            "per_image": per_image_details,
        },
    )

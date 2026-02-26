"""
Vision analysis service using Claude Opus/Sonnet.

Uses Anthropic's Claude models for computer vision analysis of appliance photos:
- Brand & model identification
- Condition scoring and defect detection
- Safety hazard detection
- Photo quality assessment

Primary model: claude-opus-4-20250514
Fallback model: claude-sonnet-4-20250514
"""

from __future__ import annotations

import base64
import json
from typing import Any

from loguru import logger

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False
    logger.warning("anthropic package not installed — vision analysis will use fallback")


VISION_SYSTEM_PROMPT = """You are an expert appliance evaluator for GreenBay Market,
a secondhand appliance marketplace in Kenya. Analyze the provided photos and return
a structured JSON assessment.

IMPORTANT rules:
- Be specific and evidence-based — cite what you see in the photos
- Grade condition honestly (A=Excellent, B=Good, C=Fair, D=Poor)
- Flag any safety concerns (frayed cords, gas leaks, cracked glass, etc.)
- Identify brand and model from labels/logos if visible
- Note completeness (missing shelves, knobs, accessories)
- Score photo quality (sharp, well-lit = high score)

Return ONLY valid JSON with this schema:
{
  "brand_detected": "string or null",
  "model_detected": "string or null",
  "condition_grade": "A|B|C|D",
  "condition_score": 0-100,
  "defects": [{"type": "string", "description": "string", "severity": "low|medium|high"}],
  "safety_concerns": [{"issue": "string", "severity": "low|medium|high"}],
  "completeness_score": 0-100,
  "authenticity_signals": ["string"],
  "photo_quality_score": 0-100,
  "photo_quality_notes": "string",
  "estimated_age_years": null or float,
  "key_observations": ["string"],
  "recommended_retail_price_kes": null or float
}"""


async def analyze_images(
    images_base64: list[str],
    category: str | None = None,
    brand_hint: str | None = None,
    model_hint: str | None = None,
    api_key: str | None = None,
    primary_model: str = "claude-opus-4-20250514",
    fallback_model: str = "claude-sonnet-4-20250514",
) -> dict[str, Any]:
    """
    Analyze appliance photos using Claude vision.

    Args:
        images_base64: List of base64-encoded JPEG/PNG images
        category: Optional category hint (e.g. "refrigerator")
        brand_hint: Optional brand hint from seller
        model_hint: Optional model hint from seller
        api_key: Anthropic API key
        primary_model: Primary Claude model to use
        fallback_model: Fallback model if primary fails

    Returns:
        Structured analysis dict, or fallback stub if API unavailable
    """
    if not HAS_ANTHROPIC or not api_key:
        logger.info("Anthropic not configured — returning stub vision analysis")
        return _stub_analysis(category, brand_hint, model_hint)

    # Build the message content with images
    content: list[dict] = []

    for i, img_b64 in enumerate(images_base64[:8]):  # Max 8 images
        # Detect media type from base64 header
        media_type = "image/jpeg"
        if img_b64.startswith("data:"):
            header, img_b64 = img_b64.split(",", 1)
            if "png" in header:
                media_type = "image/png"

        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": img_b64,
            }
        })

    # Add text prompt
    context_parts = []
    if category:
        context_parts.append(f"Category: {category}")
    if brand_hint:
        context_parts.append(f"Seller says brand: {brand_hint}")
    if model_hint:
        context_parts.append(f"Seller says model: {model_hint}")

    prompt = "Analyze these appliance photos and provide a structured evaluation."
    if context_parts:
        prompt += "\n\nContext from seller:\n" + "\n".join(context_parts)

    content.append({"type": "text", "text": prompt})

    # Try primary, then fallback
    for model in [primary_model, fallback_model]:
        try:
            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model=model,
                max_tokens=2048,
                system=VISION_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content}],
            )

            # Extract JSON from response
            text = response.content[0].text
            # Try to parse JSON (may be wrapped in ```json ... ```)
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0]
            elif "```" in text:
                text = text.split("```")[1].split("```")[0]

            result = json.loads(text.strip())
            logger.info(f"Vision analysis complete via {model}")
            return result

        except Exception as e:
            logger.warning(f"Vision analysis failed with {model}: {e}")
            if model == fallback_model:
                logger.error("Both primary and fallback models failed")
                return _stub_analysis(category, brand_hint, model_hint)

    return _stub_analysis(category, brand_hint, model_hint)


def _stub_analysis(
    category: str | None = None,
    brand: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Fallback analysis when API is unavailable."""
    return {
        "brand_detected": brand,
        "model_detected": model,
        "condition_grade": "B",
        "condition_score": 65,
        "defects": [],
        "safety_concerns": [],
        "completeness_score": 80,
        "authenticity_signals": [],
        "photo_quality_score": 60,
        "photo_quality_notes": "Analysis unavailable — using defaults",
        "estimated_age_years": None,
        "key_observations": ["Automated vision analysis not configured"],
        "recommended_retail_price_kes": None,
    }

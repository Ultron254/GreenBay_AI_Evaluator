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

CRITICAL AGE-BASED CONDITION CONSTRAINTS:
- Grade A (Excellent) can ONLY be given to products under 2 years old that
  appear near-perfect with no visible wear
- Products 5+ years old must be Grade B or lower REGARDLESS of appearance
  (age causes internal component degradation not visible in photos)
- Products 10+ years old must be Grade C or lower
- The customer-reported age MUST factor into your condition assessment
- If the seller says the product is old but it looks new in photos, trust
  the seller's age claim and downgrade accordingly

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
    age_years: float | None = None,
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
        age_years: Customer-reported age of the product in years
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

    # Add text prompt with age context for condition grading
    context_parts = []
    if category:
        context_parts.append(f"Category: {category}")
    if brand_hint:
        context_parts.append(f"Seller says brand: {brand_hint}")
    if model_hint:
        context_parts.append(f"Seller says model: {model_hint}")
    if age_years is not None and age_years > 0:
        context_parts.append(f"Customer-reported age: {age_years:.1f} years")
        if age_years >= 10:
            context_parts.append(
                "IMPORTANT: This product is 10+ years old. Maximum grade is C regardless of appearance."
            )
        elif age_years >= 5:
            context_parts.append(
                "IMPORTANT: This product is 5+ years old. Maximum grade is B regardless of appearance."
            )

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
        "photo_quality_notes": "Analysis unavailable, using defaults",
        "estimated_age_years": None,
        "key_observations": ["Automated vision analysis not configured"],
        "recommended_retail_price_kes": None,
    }


MODEL_LABEL_PROMPT = """You are an expert at reading appliance model labels and stickers.
Analyze this photo of an appliance model label/sticker and extract:

1. The model number exactly as printed
2. Any serial number visible
3. The brand name
4. Manufacturing date or year if visible
5. Power specifications (voltage, wattage) if visible
6. Any other relevant technical specifications

The seller says the model is: "{typed_model}"
The seller says the brand is: "{brand}"
The seller says the category is: "{category}"

Return ONLY valid JSON:
{{
  "model_number": "exact model number from label or null",
  "model_verified": true if the label matches the typed model (case-insensitive),
  "brand_from_label": "brand as printed or null",
  "serial_number": "serial if visible or null",
  "manufacture_date": "date/year if visible or null",
  "power_specs": "wattage/voltage if visible or null",
  "other_specs": "any other specs visible or null",
  "specs_summary": "one-line summary of what was found",
  "retail_price": null,
  "release_year": year as integer or null
}}"""


def analyze_model_label(
    image_data: str,
    typed_model: str = "",
    category: str = "",
    brand: str = "",
    api_key: str | None = None,
    model: str = "claude-sonnet-4-20250514",
) -> dict[str, Any]:
    """
    Analyze a model label photo to extract and verify the model number.

    Args:
        image_data: base64-encoded data URL of the model label photo
        typed_model: The model number the seller typed in
        category: Product category
        brand: Product brand
        api_key: Anthropic API key (auto-detected if not provided)

    Returns:
        Dict with model_verified, model_number, specs_summary, etc.
    """
    import os

    if not api_key:
        api_key = os.environ.get("ANTHROPIC_API_KEY")

    if not HAS_ANTHROPIC or not api_key:
        logger.info("Anthropic not configured, returning stub model label analysis")
        return {
            "model_verified": bool(typed_model),
            "model_number": typed_model or None,
            "specs_summary": f"Model {typed_model} recorded (vision verification unavailable)" if typed_model else None,
            "retail_price": None,
            "release_year": None,
        }

    try:
        # Parse the data URL
        media_type = "image/jpeg"
        img_b64 = image_data
        if image_data.startswith("data:"):
            header, img_b64 = image_data.split(",", 1)
            if "png" in header:
                media_type = "image/png"

        prompt = MODEL_LABEL_PROMPT.format(
            typed_model=typed_model or "not provided",
            brand=brand or "not provided",
            category=category or "not provided",
        )

        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": img_b64,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }],
        )

        # Parse response
        text = response.content[0].text
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]

        result = json.loads(text.strip())
        logger.info(f"Model label analysis complete: {result.get('model_number')}")
        return result

    except Exception as e:
        logger.warning(f"Model label analysis failed: {e}")
        return {
            "model_verified": bool(typed_model),
            "model_number": typed_model or None,
            "specs_summary": f"Model {typed_model} recorded (label analysis failed)" if typed_model else None,
            "retail_price": None,
            "release_year": None,
        }

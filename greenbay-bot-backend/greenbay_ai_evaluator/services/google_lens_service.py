"""
Google Cloud Vision product identification service.

Uses Google Cloud Vision API's WEB_DETECTION feature (equivalent to Google Lens)
to identify products from images, find matching products online, and discover
retail prices and specifications.

Also uses LABEL_DETECTION for appliance type classification and
LOGO_DETECTION for brand verification.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from loguru import logger


@dataclass
class ProductIdentification:
    """Result from Google Cloud Vision product identification."""

    product_name: str | None = None
    brand: str | None = None
    model: str | None = None
    category: str | None = None
    specifications: dict[str, str] = field(default_factory=dict)
    estimated_retail_price_kes: float | None = None
    release_year: int | None = None
    web_entities: list[dict[str, Any]] = field(default_factory=list)
    shopping_results: list[dict[str, Any]] = field(default_factory=list)
    similar_images: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    logos: list[str] = field(default_factory=list)
    confidence: float = 0.0  # 0-100
    raw_description: str = ""


# Simple in-memory cache keyed by image hash
_cache: dict[str, ProductIdentification] = {}


def _image_hash(image_b64: str) -> str:
    """Compute a short hash of the image for caching."""
    raw = image_b64[:500] + image_b64[-500:] if len(image_b64) > 1000 else image_b64
    return hashlib.md5(raw.encode()).hexdigest()


def _extract_kes_prices(text: str) -> list[float]:
    """Extract KES prices from text."""
    patterns = [
        r'KES\s*[\d,]+(?:\.\d+)?',
        r'Ksh\s*[\d,]+(?:\.\d+)?',
        r'KSh\s*[\d,]+(?:\.\d+)?',
        r'Kshs?\s*[\d,]+(?:\.\d+)?',
        r'[\d,]+(?:\.\d+)?\s*KES',
    ]
    prices = []
    for pat in patterns:
        for match in re.finditer(pat, text, re.IGNORECASE):
            digits = re.sub(r'[^0-9.]', '', match.group())
            try:
                val = float(digits)
                if 500 <= val <= 2_000_000:
                    prices.append(val)
            except ValueError:
                pass
    return prices


def _extract_usd_prices(text: str) -> list[float]:
    """Extract USD prices and convert to KES (approx 155 KES/USD)."""
    patterns = [
        r'\$\s*[\d,]+(?:\.\d+)?',
        r'USD\s*[\d,]+(?:\.\d+)?',
    ]
    prices = []
    for pat in patterns:
        for match in re.finditer(pat, text, re.IGNORECASE):
            digits = re.sub(r'[^0-9.]', '', match.group())
            try:
                val = float(digits)
                if 5 <= val <= 15_000:
                    prices.append(val * 155)  # Convert to KES
            except ValueError:
                pass
    return prices


def identify_product(
    image_b64: str,
    *,
    category_hint: str = "",
    brand_hint: str = "",
    model_hint: str = "",
    google_cloud_api_key: str | None = None,
) -> ProductIdentification:
    """
    Identify a product using Google Cloud Vision API's WEB_DETECTION.

    This is the equivalent of Google Lens — it identifies objects in images,
    finds matching products online, and returns web entities with descriptions.

    Args:
        image_b64: Base64-encoded image (with or without data: prefix)
        category_hint: Optional category hint from user
        brand_hint: Optional brand hint from user
        model_hint: Optional model hint from user
        google_cloud_api_key: Google Cloud API key with Vision API enabled

    Returns:
        ProductIdentification with product details, prices, and confidence
    """
    result = ProductIdentification()

    # Check cache first
    img_hash = _image_hash(image_b64)
    if img_hash in _cache:
        logger.info(f"Google Lens cache hit: {img_hash}")
        return _cache[img_hash]

    if not google_cloud_api_key:
        logger.warning("Google Cloud API key not set — skipping product identification")
        return result

    try:
        import httpx

        # Strip data URL prefix if present
        if image_b64.startswith("data:"):
            image_b64 = image_b64.split(",", 1)[1]

        # Build the request for Google Cloud Vision API
        url = f"https://vision.googleapis.com/v1/images:annotate?key={google_cloud_api_key}"

        request_body = {
            "requests": [{
                "image": {"content": image_b64},
                "features": [
                    {"type": "WEB_DETECTION", "maxResults": 20},
                    {"type": "LABEL_DETECTION", "maxResults": 15},
                    {"type": "LOGO_DETECTION", "maxResults": 5},
                    {"type": "TEXT_DETECTION", "maxResults": 5},
                ],
            }]
        }

        # Make the API call
        with httpx.Client(timeout=20.0) as client:
            response = client.post(url, json=request_body)
            response.raise_for_status()
            data = response.json()

        if not data.get("responses"):
            logger.warning("Google Vision returned empty response")
            return result

        resp = data["responses"][0]

        # Check for errors
        if resp.get("error"):
            logger.error(f"Google Vision API error: {resp['error']}")
            return result

        # Process WEB_DETECTION (Google Lens equivalent)
        web = resp.get("webDetection", {})
        all_text_parts = []

        # Web entities — these are the product identifications
        for entity in web.get("webEntities", []):
            desc = entity.get("description", "")
            score = entity.get("score", 0)
            if desc:
                result.web_entities.append({
                    "description": desc,
                    "score": score,
                })
                all_text_parts.append(desc)

        # Best guess labels — Google's top guess for what the product is
        for label in web.get("bestGuessLabels", []):
            guess = label.get("label", "")
            if guess:
                all_text_parts.append(guess)
                if not result.product_name:
                    result.product_name = guess

        # Pages with matching images — shopping sites, product pages
        for page in web.get("pagesWithMatchingImages", []):
            title = page.get("pageTitle", "")
            url = page.get("url", "")
            if title and url:
                result.shopping_results.append({
                    "title": title,
                    "url": url,
                })
                all_text_parts.append(title)

        # Similar images
        for img in web.get("visuallySimilarImages", [])[:5]:
            if img.get("url"):
                result.similar_images.append(img["url"])

        # Process LABEL_DETECTION — appliance type classification
        for label in resp.get("labelAnnotations", []):
            desc = label.get("description", "")
            if desc:
                result.labels.append(desc)

        # Process LOGO_DETECTION — brand verification
        for logo in resp.get("logoAnnotations", []):
            desc = logo.get("description", "")
            if desc:
                result.logos.append(desc)
                if not result.brand:
                    result.brand = desc

        # Process TEXT_DETECTION — any text visible on the product
        text_annotations = resp.get("textAnnotations", [])
        full_text = text_annotations[0].get("description", "") if text_annotations else ""

        # Try to extract model number from OCR text
        if full_text and not result.model:
            # Common model number patterns
            model_patterns = [
                r'(?:Model|MOD|M/N)[:\s]*([A-Z0-9][\w\-/.]+)',
                r'([A-Z]{2,}\d{2,}[\w\-/.]*)',
                r'([A-Z]\w{3,}\d{2,}\w*)',
            ]
            for pat in model_patterns:
                m = re.search(pat, full_text, re.IGNORECASE)
                if m:
                    result.model = m.group(1).strip()
                    break

        # ---- Intelligence: Extract product info from combined text ----
        combined_text = " ".join(all_text_parts)
        result.raw_description = combined_text[:500]

        # Extract brand from web entities if not from logo
        if not result.brand and result.web_entities:
            # Known brand names to look for
            known_brands = [
                "Samsung", "LG", "Hisense", "VON", "Ramtons", "TCL", "Haier",
                "ARMCO", "Midea", "MIKA", "Kenwood", "Tefal", "Toshiba",
                "Simfer", "NUNIX", "Moulinex", "Braun", "Philips", "Bosch",
                "Whirlpool", "GE", "Electrolux", "Sharp", "Panasonic",
            ]
            for brand_name in known_brands:
                if brand_name.lower() in combined_text.lower():
                    result.brand = brand_name
                    break

        # Try to determine category from labels
        category_map = {
            "refrigerator": ["refrigerator", "fridge", "freezer", "cooling"],
            "washing_machine": ["washing machine", "washer", "dryer", "laundry"],
            "tv_monitor": ["television", "tv", "monitor", "screen", "display"],
            "cooker_oven": ["cooker", "oven", "stove", "range", "gas cooker"],
            "microwave": ["microwave"],
            "small_kitchen": ["blender", "mixer", "kettle", "toaster", "food processor"],
        }
        labels_lower = " ".join(result.labels).lower() + " " + combined_text.lower()
        for cat, keywords in category_map.items():
            if any(kw in labels_lower for kw in keywords):
                result.category = cat
                break

        # Extract prices from web results
        all_prices_kes = []
        for sr in result.shopping_results:
            title = sr.get("title", "")
            all_prices_kes.extend(_extract_kes_prices(title))
            all_prices_kes.extend(_extract_usd_prices(title))

        # Also try to extract from combined text
        all_prices_kes.extend(_extract_kes_prices(combined_text))
        all_prices_kes.extend(_extract_usd_prices(combined_text))

        if all_prices_kes:
            # Use median price as estimate
            all_prices_kes.sort()
            median_idx = len(all_prices_kes) // 2
            result.estimated_retail_price_kes = all_prices_kes[median_idx]

        # Extract year from text
        year_match = re.search(r'\b(20[12]\d)\b', combined_text)
        if year_match:
            year = int(year_match.group(1))
            if 2010 <= year <= 2026:
                result.release_year = year

        # Apply hints if no detection
        if not result.brand and brand_hint:
            result.brand = brand_hint
        if not result.model and model_hint:
            result.model = model_hint
        if not result.category and category_hint:
            result.category = category_hint

        # Compute confidence
        confidence = 0.0
        if result.product_name:
            confidence += 25.0
        if result.brand:
            confidence += 20.0
        if result.model:
            confidence += 15.0
        if result.web_entities:
            confidence += min(20.0, len(result.web_entities) * 2.0)
        if result.shopping_results:
            confidence += min(10.0, len(result.shopping_results) * 2.0)
        if result.estimated_retail_price_kes:
            confidence += 10.0
        result.confidence = min(100.0, confidence)

        # Cache the result
        _cache[img_hash] = result

        logger.info(
            f"Google Lens identification: product={result.product_name}, "
            f"brand={result.brand}, model={result.model}, "
            f"price={result.estimated_retail_price_kes}, "
            f"confidence={result.confidence}, "
            f"entities={len(result.web_entities)}, "
            f"shopping={len(result.shopping_results)}"
        )

    except ImportError:
        logger.warning("httpx package not installed — skipping Google Vision")
    except Exception as e:
        logger.error(f"Google Cloud Vision API failed: {e}")

    return result


def identify_product_multi(
    images_b64: list[str],
    **kwargs,
) -> ProductIdentification:
    """
    Identify a product from multiple images, merging results.

    Uses the first image for primary identification, then enriches
    with any additional info from subsequent images.
    """
    if not images_b64:
        return ProductIdentification()

    # Use the first image (usually the best/front view) as primary
    primary = identify_product(images_b64[0], **kwargs)

    # If confidence is low and we have more images, try the second
    if primary.confidence < 40 and len(images_b64) > 1:
        secondary = identify_product(images_b64[1], **kwargs)
        # Merge: prefer secondary if it found more
        if secondary.confidence > primary.confidence:
            if secondary.product_name and not primary.product_name:
                primary.product_name = secondary.product_name
            if secondary.brand and not primary.brand:
                primary.brand = secondary.brand
            if secondary.model and not primary.model:
                primary.model = secondary.model
            if secondary.estimated_retail_price_kes and not primary.estimated_retail_price_kes:
                primary.estimated_retail_price_kes = secondary.estimated_retail_price_kes
            primary.web_entities.extend(secondary.web_entities)
            primary.shopping_results.extend(secondary.shopping_results)
            primary.confidence = max(primary.confidence, secondary.confidence)

    return primary

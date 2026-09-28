"""
Internet price lookup service (v6).

Primary: Gemini 2.5 Flash with Google Search grounding.
Fallback: Tavily web search (commented out, kept for rollback).

Searches for retail prices in the target country's local market and currency.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from loguru import logger


@dataclass
class InternetPriceResult:
    """Result from internet price lookup."""

    launch_price: float | None = None
    current_resale_low: float | None = None
    current_resale_high: float | None = None
    sources: list[dict[str, str]] = field(default_factory=list)
    confidence: float = 0.0
    raw_snippets: list[str] = field(default_factory=list)
    currency: str = "KES"
    # WHY the lookup did or did not produce a price (Sep 2026). ``status`` is a
    # short machine-readable token (see LOOKUP_* below), ``status_detail`` a
    # one-line human explanation. Neither ever contains a credential.
    status: str = ""
    status_detail: str = ""
    # Per-provider outcome of the dual-source lookup, filled in by
    # search_internet_price: {"gemini": {"status", "detail"}, "sonar": {...}}.
    providers: dict[str, dict[str, str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Lookup outcome tokens (shared by the Gemini and Sonar lookups)
# ---------------------------------------------------------------------------
LOOKUP_OK = "ok"
LOOKUP_NOT_CONFIGURED = "not_configured"   # no key / credentials: source is off
LOOKUP_AUTH_FAILED = "auth_failed"         # could not obtain a token
LOOKUP_HTTP_ERROR = "http_error"           # provider answered non-200
LOOKUP_TIMEOUT = "timeout"                 # provider did not answer in time
LOOKUP_REQUEST_FAILED = "request_failed"   # network / DNS / TLS error
LOOKUP_BAD_RESPONSE = "bad_response"       # 200 but not the documented shape
LOOKUP_EMPTY_ANSWER = "empty_answer"       # 200 with no text in the answer
LOOKUP_PARSE_FAILURE = "parse_failure"     # text came back, no price readable
LOOKUP_NULL_PRICE = "null_price"           # provider said: no price for this item
LOOKUP_OUT_OF_BAND = "out_of_band"         # price outside the category sanity band

# Outcomes that mean the PROVIDER was unreachable or broken, as opposed to the
# provider answering "I found nothing". Only these count as an outage.
LOOKUP_OUTAGE_STATUSES = frozenset({
    LOOKUP_AUTH_FAILED, LOOKUP_HTTP_ERROR, LOOKUP_TIMEOUT,
    LOOKUP_REQUEST_FAILED, LOOKUP_BAD_RESPONSE, LOOKUP_EMPTY_ANSWER,
})


# ---------------------------------------------------------------------------
# Price sanity ranges by category (in KES-equivalent)
# ---------------------------------------------------------------------------
_PRICE_SANITY: dict[str, tuple[float, float]] = {
    "refrigerator": (8_000, 300_000),
    "fridge": (8_000, 300_000),
    "freezer": (8_000, 250_000),
    "cooker": (5_000, 200_000),
    "cooker_oven": (5_000, 200_000),
    "tv": (5_000, 500_000),
    "tv_monitor": (5_000, 500_000),
    "washing_machine": (10_000, 300_000),
    "microwave": (3_000, 80_000),
    "water_dispenser": (5_000, 80_000),
    "soundbar": (3_000, 100_000),
    "woofer": (2_000, 80_000),
}
_DEFAULT_SANITY = (1_000, 500_000)

_COUNTRY_NAMES = {"KE": "Kenya", "UG": "Uganda", "NG": "Nigeria"}
_COUNTRY_CURRENCIES = {"KE": "KES", "UG": "UGX", "NG": "NGN"}

# Approx KES per 1 unit of local currency. Used ONLY to scale the KES-denominated
# sanity bands into the target currency so a legitimate local-currency price is
# not falsely rejected. (Not used for customer-facing conversion.)
_KES_PER_LOCAL = {"KES": 1.0, "UGX": 0.0357, "NGN": 0.085}


def _sanity_band_for(cat_key: str, currency: str) -> tuple[float, float]:
    """KES sanity band scaled into the evaluation's local currency."""
    lo, hi = _PRICE_SANITY.get(cat_key, _DEFAULT_SANITY)
    rate = _KES_PER_LOCAL.get(currency, 1.0)
    if rate and rate != 1.0:
        return lo / rate, hi / rate
    return lo, hi


# ---------------------------------------------------------------------------
# Gemini Google Search grounding (v6 primary)
# ---------------------------------------------------------------------------
def _extract_json_from_text(text: str) -> dict | None:
    """Parse a JSON object from model output, tolerant of fences."""
    if not text:
        return None
    stripped = text.strip()
    stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
    stripped = re.sub(r"\s*```$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", stripped, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _format_size_hint(size_value: float | None, size_unit: str | None) -> str:
    """Build a human phrase like '55 inch' / '8 kg' / '250 litre' for the prompt."""
    if not size_value or size_value <= 0 or not size_unit:
        return ""
    unit = size_unit.lower().strip()
    if unit in ("inch", "inches", '"', "in"):
        return f"{size_value:g} inch"
    if unit in ("kg", "kgs", "kilogram", "kilograms"):
        return f"{size_value:g} kg"
    if unit in ("litre", "litres", "liter", "liters", "l"):
        return f"{size_value:g} litre"
    return f"{size_value:g} {unit}"


def gemini_price_research(
    *,
    brand: str,
    model: str,
    category: str,
    country: str = "KE",
    size_value: float | None = None,
    size_unit: str | None = None,
) -> InternetPriceResult:
    """Call Gemini 2.5 Flash with Google Search grounding for retail price.

    Uses the existing Vertex AI service account — no new credentials needed.
    The size hint (inches / kg / litres) is critical: without it a 43" and a
    55" TV produce identical prompts and identical (wrong) prices.
    """
    result = InternetPriceResult()
    country_name = _COUNTRY_NAMES.get(country, "Kenya")
    currency = _COUNTRY_CURRENCIES.get(country, "KES")
    result.currency = currency

    try:
        from greenbay_ai_evaluator.services.vertex_ai_service import (
            _get_access_token,
            _build_endpoint,
        )
        from app.config import get_settings
        settings = get_settings()
    except Exception as e:
        logger.warning(f"Gemini price research: import/config failed: {e}")
        result.status = LOOKUP_NOT_CONFIGURED
        result.status_detail = f"import/config failed: {type(e).__name__}"
        return result

    if not settings.google_vertex_credentials_file:
        logger.warning("Gemini price research: no Vertex credentials")
        result.status = LOOKUP_NOT_CONFIGURED
        result.status_detail = "GOOGLE_VERTEX_CREDENTIALS_FILE not set"
        return result

    size_hint = _format_size_hint(size_value, size_unit)
    # Build the most specific product description we can.
    parts = [p for p in (brand, model, size_hint) if p]
    product_desc = " ".join(parts).strip() or category

    prompt = (
        f"You are a pricing researcher. Using Google Search, find the current "
        f"NEW retail price of this exact appliance in {country_name}:\n"
        f"  Brand: {brand or 'unknown'}\n"
        f"  Model: {model or 'unknown'}\n"
        f"  Size/capacity: {size_hint or 'unknown'}\n"
        f"  Category: {category}\n\n"
        f"Search mainstream {country_name} retailers (e.g. Jumia, Kilimall, "
        f"brand stores, electronics shops). The size/capacity MUST match — a "
        f"43-inch TV and a 55-inch TV have very different prices, so do not "
        f"return a generic category price. "
        f"Return ONLY a JSON object with these exact fields: "
        f'{{"new_price": <number in {currency}>, "currency": "{currency}", '
        f'"matched_product": "<the exact product/title you priced>", '
        f'"sources": [<list of source URLs>]}}. '
        f"If you cannot find a price for this specific size/model, return "
        f'{{"new_price": null}} rather than guessing.'
    )

    try:
        token = _get_access_token()
    except Exception as e:
        logger.warning(f"Gemini price research: auth failed: {e}")
        result.status = LOOKUP_AUTH_FAILED
        result.status_detail = f"could not obtain a Vertex token: {type(e).__name__}"
        return result

    # Build endpoint for the search-grounded model
    s = settings
    endpoint = (
        f"https://{s.google_vertex_region}-aiplatform.googleapis.com/v1/"
        f"projects/{s.google_vertex_project}/locations/{s.google_vertex_region}/"
        f"publishers/google/models/{s.google_vertex_model}:generateContent"
    )

    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "tools": [{"googleSearch": {}}],
        "generationConfig": {
            "temperature": 0.1,
            # Raised from 512: with Google Search grounding the grounded answer
            # plus citations needs headroom, otherwise the JSON gets truncated.
            "maxOutputTokens": 2048,
            # gemini-2.5-flash has "thinking" ON by default; thinking tokens
            # count against maxOutputTokens and were eating the entire budget,
            # leaving NO visible text (root cause of "returned NO price").
            # Disable it for this factual lookup.
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    _GEMINI_BACKOFFS = (2, 6, 12)  # seconds; used for 429/transient retries
    _MAX_ATTEMPTS = 3
    # Outcome of the LAST attempt, reported if no attempt yields a price.
    result.status = LOOKUP_REQUEST_FAILED
    result.status_detail = "no attempt completed"
    for attempt in range(_MAX_ATTEMPTS):
        try:
            import requests as _req
            # Grounded search has been observed at ~35-38s; a 30s timeout was
            # killing the first attempt every time. Give it real headroom.
            resp = _req.post(endpoint, headers=headers, json=body, timeout=50)
        except Exception as e:
            logger.warning(f"Gemini price research: request failed (attempt {attempt+1}): {e}")
            result.status, result.status_detail = _classify_request_exception(e)
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_GEMINI_BACKOFFS[attempt])
            continue

        if resp.status_code != 200:
            logger.warning(
                f"Gemini price research: HTTP {resp.status_code} "
                f"(attempt {attempt+1}): {resp.text[:200]}"
            )
            result.status = LOOKUP_HTTP_ERROR
            result.status_detail = f"HTTP {resp.status_code}"
            # Rate limited / quota exhausted — honor Retry-After then back off.
            if (resp.status_code == 429 or "RESOURCE_EXHAUSTED" in resp.text) and attempt < _MAX_ATTEMPTS - 1:
                retry_after = resp.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else _GEMINI_BACKOFFS[attempt]
                except (TypeError, ValueError):
                    delay = _GEMINI_BACKOFFS[attempt]
                logger.warning(f"Gemini price research: rate limited — backing off {delay}s")
                time.sleep(delay)
                continue
            # Self-heal: some Vertex API versions reject thinkingConfig with a
            # 400. Strip it and let the next attempt run without it.
            if resp.status_code == 400 and "thinking" in resp.text.lower():
                body["generationConfig"].pop("thinkingConfig", None)
                logger.warning("Gemini price research: removed thinkingConfig and will retry")
            continue

        finish_reason = ""
        try:
            data = resp.json()
            candidate = (data.get("candidates") or [{}])[0]
            finish_reason = candidate.get("finishReason", "")
            parts = (candidate.get("content") or {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        except Exception:
            text = ""

        if not text:
            logger.warning(
                f"Gemini price research: empty text (attempt {attempt+1}, "
                f"finishReason={finish_reason!r}) — raising token budget if MAX_TOKENS"
            )
            result.status = LOOKUP_EMPTY_ANSWER
            result.status_detail = f"no text in the answer (finishReason={finish_reason!r})"
        result.raw_snippets.append(text[:500])

        # Extract grounding sources. Vertex has shipped these under a few
        # different shapes; check all known locations so issue #8's
        # justification gets the real URLs the price was grounded on.
        grounding = candidate.get("groundingMetadata", {}) or {}
        seen_urls: set[str] = set()

        def _add_src(title: str, url: str) -> None:
            if url and url not in seen_urls:
                seen_urls.add(url)
                result.sources.append({"title": title or "", "url": url})

        for chunk in grounding.get("groundingChunks", []) or []:
            web = (chunk.get("web") or {}) if isinstance(chunk, dict) else {}
            _add_src(web.get("title", ""), web.get("uri", ""))
        # Alternate/legacy shapes
        for attr in grounding.get("groundingAttributions", []) or []:
            web = (attr.get("web") or {}) if isinstance(attr, dict) else {}
            _add_src(web.get("title", ""), web.get("uri", "") or web.get("url", ""))
        # citationMetadata shape (cites the URLs a passage was drawn from)
        cm = candidate.get("citationMetadata", {}) or {}
        for c in (cm.get("citationSources") or cm.get("citations") or []):
            if isinstance(c, dict):
                _add_src(c.get("title", ""), c.get("uri", "") or c.get("url", ""))
        # searchEntryPoint.renderedContent embeds source <a href="..."> links
        sep = grounding.get("searchEntryPoint") or {}
        rendered = sep.get("renderedContent", "") if isinstance(sep, dict) else ""
        if rendered:
            for href in re.findall(r'href="(https?://[^"]+)"', rendered):
                _add_src("", href)
        for q in grounding.get("webSearchQueries", []) or []:
            result.raw_snippets.append(f"search_query: {q}")

        if not result.sources:
            # Surface the actual metadata keys so we can map the real shape
            # from the next health-check without guessing.
            logger.warning(
                f"Gemini price research: price found but 0 sources captured; "
                f"groundingMetadata keys={list(grounding.keys())}"
            )

        parsed = _extract_json_from_text(text)
        if parsed and parsed.get("new_price"):
            try:
                price = float(parsed["new_price"])
            except (TypeError, ValueError):
                logger.warning(f"Gemini price research: unparsable price: {parsed['new_price']}")
                result.status = LOOKUP_PARSE_FAILURE
                result.status_detail = "new_price was not a number"
                continue

            cat_key = category.lower().strip()
            lo, hi = _sanity_band_for(cat_key, currency)
            if lo <= price <= hi:
                result.launch_price = price
                result.current_resale_low = price * 0.7
                result.current_resale_high = price * 0.95
                result.confidence = min(75.0, 40.0 + len(result.sources) * 10.0)
                result.status = LOOKUP_OK
                result.status_detail = f"{len(result.sources)} sources"
                logger.info(
                    f"Gemini price research: {product_desc} → "
                    f"{currency} {price:,.0f} (confidence {result.confidence})"
                )
                return result
            else:
                logger.warning(
                    f"Gemini price research: price {price} outside sanity "
                    f"range [{lo}-{hi}] for {cat_key}, retrying..."
                )
                result.status = LOOKUP_OUT_OF_BAND
                result.status_detail = f"price outside the sanity band for {cat_key}"
                continue
        else:
            logger.warning(
                f"Gemini price research: no JSON/price in response "
                f"(attempt {attempt+1}, finishReason={finish_reason!r}); "
                f"text snippet: {text[:200]!r}"
            )
            if text:
                if isinstance(parsed, dict) and "new_price" in parsed:
                    result.status = LOOKUP_NULL_PRICE
                    result.status_detail = "the model found no price for this item"
                else:
                    result.status = LOOKUP_PARSE_FAILURE
                    result.status_detail = "no JSON price in the answer"

    return result


# ---------------------------------------------------------------------------
# Shared helpers for reading a provider's answer (Sep 2026)
# ---------------------------------------------------------------------------
def _classify_request_exception(exc: Exception) -> tuple[str, str]:
    """Map a transport exception to (status, detail). Only the exception TYPE
    is reported: exception text can embed request headers or URLs."""
    name = type(exc).__name__
    if "timeout" in name.lower():
        return LOOKUP_TIMEOUT, f"no answer in time ({name})"
    return LOOKUP_REQUEST_FAILED, f"network error ({name})"


def _redact(text: str, *secrets: str) -> str:
    """Collapse whitespace and blank out any secret that a provider echoed."""
    out = " ".join(str(text or "").split())
    for secret in secrets:
        if secret and len(secret) >= 6:
            out = out.replace(secret, "***")
    return out


_SONAR_HTTP_HINTS: dict[int, str] = {
    400: "request rejected: check PERPLEXITY_MODEL and the request parameters",
    401: "key rejected: invalid or revoked key, or the prepaid credit balance is exhausted",
    402: "payment required: the prepaid credit balance is exhausted",
    403: "key not allowed to use this API or model",
    404: "model or endpoint not found: check PERPLEXITY_MODEL",
    429: "rate limited or quota exhausted",
}


def _sonar_http_detail(status_code: int, body: str, api_key: str) -> str:
    """One line explaining a non-200 from Perplexity, safe to show in the
    health report: status, the provider's own error message, and a hint."""
    message = ""
    try:
        err = (json.loads(body) or {}).get("error")
        if isinstance(err, dict):
            message = str(err.get("message") or err.get("type") or "")
        elif err:
            message = str(err)
    except (ValueError, AttributeError, TypeError):
        # Not JSON (e.g. an HTML error page). Report its size, not its markup.
        message = ""
    hint = _SONAR_HTTP_HINTS.get(status_code) or (
        "Perplexity server error, usually transient" if status_code >= 500 else ""
    )
    parts = [f"HTTP {status_code}"]
    if message:
        parts.append(_redact(message, api_key)[:140])
    if hint:
        parts.append(hint)
    return " | ".join(parts)


_CITATION_MARK_RE = re.compile(r"\[\d{1,3}\]")
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
# A number with optional comma thousands separators and decimals: 42999,
# 42,999.00. A space is NOT a separator: "12,995 365 days" is two numbers.
_AMOUNT = r"\d{1,3}(?:,\d{3})+(?!\d)(?:\.\d+)?|\d+(?:\.\d+)?"
# The "new_price" field of an answer whose JSON did not parse. The value must
# END the field: next comes the following key (a comma then a quote), the
# closing brace, or the end of the text. So a range such as 42999-45999 or
# "42,999 to 45,999" is not read as its first number, and "42,999 - 45,999"
# is not read as 42.
_NEW_PRICE_FIELD_RE = re.compile(
    r'"new_price"\s*:\s*"?\s*(?:[A-Za-z₦/=.]{0,5}\s*)?(' + _AMOUNT + r')\s*(?:/=)?\s*"?\s*(?=,\s*"|\}|$)'
)
_NEW_PRICE_NULL_RE = re.compile(r'"new_price"\s*:\s*(?:null|None|"")', re.IGNORECASE)


def _coerce_price(value: Any) -> float | None:
    """Read a price the way providers actually write it: 42999, 42999.0,
    "42,999", "KES 42,999", "KSh. 42,999.00", "42,999/=". None when the value
    is not a single positive, finite number: a list or dict, a range, a
    negative, "inf", free text."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if number > 0 and number != float("inf") and number == number else None
    if not isinstance(value, str):
        return None  # a list or a dict is not a price
    if "-" in value or "–" in value:
        return None  # a negative or a range
    found = re.findall(_AMOUNT, value)
    if len(found) != 1:
        return None
    try:
        number = float(found[0].replace(",", ""))
    except ValueError:
        return None
    return number if number > 0 and number != float("inf") else None


def _clean_answer_text(text: str) -> str:
    """Drop reasoning blocks and [1]-style citation marks, which break JSON."""
    return _CITATION_MARK_RE.sub("", _THINK_BLOCK_RE.sub("", text or "")).strip()


def _price_from_loose_text(text: str) -> float | None:
    """Last-resort reader for an answer whose JSON did not parse: the
    ``"new_price": 42,999`` field, whose thousands separator is what made the
    JSON invalid. Nothing else.

    Prose is deliberately NOT read. "Samsung UA43T5300 KES 42,999" contains
    5300 next to a currency word, "a similar 32 inch model is KES 18,999"
    prices the wrong product, and "KES 3,999 a month" is an instalment. A
    wrong LOW reading wins the Gemini cross-check (a conflict takes the lower
    price), so a sentence must never become a price.
    """
    m = _NEW_PRICE_FIELD_RE.search(text)
    return _coerce_price(m.group(1)) if m else None


def _read_price_answer(text: str, currency: str) -> tuple[float | None, str, str, dict]:
    """Turn a provider's text answer into (price, status, detail, parsed_json).

    Pure function (no network), so every answer shape seen in production can be
    pinned by a unit test.
    """
    cleaned = _clean_answer_text(text)
    if not cleaned:
        return None, LOOKUP_EMPTY_ANSWER, "the answer contained no text", {}
    parsed = _extract_json_from_text(cleaned)
    if isinstance(parsed, dict) and "new_price" in parsed:
        raw = parsed.get("new_price")
        if raw is None or (isinstance(raw, str) and raw.strip().lower() in ("", "null", "none", "n/a")):
            return None, LOOKUP_NULL_PRICE, "the model found no price for this item", parsed
        price = _coerce_price(raw)
        if price is None:
            return None, LOOKUP_PARSE_FAILURE, "new_price was not a readable number", parsed
        return price, LOOKUP_OK, "json", parsed
    # An explicit null always wins, whatever else the answer goes on to say.
    if _NEW_PRICE_NULL_RE.search(cleaned):
        return None, LOOKUP_NULL_PRICE, "the model found no price for this item", {}
    price = _price_from_loose_text(cleaned)
    if price is not None:
        return price, LOOKUP_OK, "new_price field of an answer that was not valid JSON", {}
    return None, LOOKUP_PARSE_FAILURE, "no price could be read from the answer", {}


# ---------------------------------------------------------------------------
# Perplexity Sonar (v6.3) — second independent grounded new-price source
# ---------------------------------------------------------------------------
_SONAR_ENDPOINT = "https://api.perplexity.ai/chat/completions"
_SONAR_DEFAULT_MODEL = "sonar"


def sonar_price_research(
    *,
    brand: str,
    model: str,
    category: str,
    country: str = "KE",
    size_value: float | None = None,
    size_unit: str | None = None,
) -> InternetPriceResult:
    """Look up the NEW retail price via the Perplexity Sonar API (live web
    search with citations). Used as an independent cross-check on Gemini —
    single-source lookups were the main cause of new-price noise (same model
    priced 35,500 one day and 63,999 the next).

    Requires PERPLEXITY_API_KEY; returns an empty result (status
    ``not_configured``) when it is not set, so the system degrades to
    Gemini-only. Every other way of returning no price sets ``status`` and
    ``status_detail`` so the health probe can say WHY (Sep 2026: production
    reported "Sonar returned NO price" for weeks with no way to tell a dead
    key from a parse failure)."""
    result = InternetPriceResult()
    country_name = _COUNTRY_NAMES.get(country, "Kenya")
    currency = _COUNTRY_CURRENCIES.get(country, "KES")
    result.currency = currency

    sonar_model = _SONAR_DEFAULT_MODEL
    try:
        from app.config import get_settings
        _settings = get_settings()
        api_key = (getattr(_settings, "perplexity_api_key", None) or "").strip()
        sonar_model = (getattr(_settings, "perplexity_model", "") or "").strip() or _SONAR_DEFAULT_MODEL
    except Exception:  # noqa: BLE001
        api_key = ""
    if not api_key:
        result.status = LOOKUP_NOT_CONFIGURED
        result.status_detail = "PERPLEXITY_API_KEY not set"
        return result

    size_hint = _format_size_hint(size_value, size_unit)
    # Demand a size match only when a size is known. With "Size/capacity:
    # unknown" the old wording ("MUST match exactly") steered the model to
    # answer null for any item whose size the customer did not give.
    size_rule = (
        "The size/capacity MUST match exactly. " if size_hint
        else "If the model number identifies one specific product, price that product. "
    )
    prompt = (
        f"Find the current NEW retail price in {country_name} of this exact "
        f"appliance: Brand: {brand or 'unknown'}; Model: {model or 'unknown'}; "
        f"Size/capacity: {size_hint or 'unknown'}; Category: {category}. "
        f"Check mainstream {country_name} retailers (Jumia, Kilimall, brand "
        f"stores, electronics shops). {size_rule}"
        f"Respond with ONLY a JSON object: "
        f'{{"new_price": <plain number in {currency}, no thousands separators>, '
        f'"currency": "{currency}", '
        f'"matched_product": "<exact product you priced>"}}. '
        f'If no price for this specific model/size exists, return {{"new_price": null}}.'
    )

    try:
        import requests as _req
        resp = _req.post(
            _SONAR_ENDPOINT,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": sonar_model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 500,
            },
            timeout=45,
        )
    except Exception as e:  # noqa: BLE001
        result.status, result.status_detail = _classify_request_exception(e)
        # Type only: the exception text can carry the Authorization header.
        logger.warning(f"Sonar price research failed: {result.status_detail}")
        return result

    try:
        if resp.status_code != 200:
            result.status = LOOKUP_HTTP_ERROR
            result.status_detail = _sonar_http_detail(resp.status_code, resp.text, api_key)
            logger.warning(f"Sonar price research: {result.status_detail}")
            return result
        try:
            data = resp.json()
            message = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
            text = message.get("content") or ""
            if isinstance(text, list):  # content parts: [{"type":"text","text":...}]
                text = "".join(
                    str(part.get("text", "")) for part in text if isinstance(part, dict)
                )
        except Exception as e:  # noqa: BLE001
            result.status = LOOKUP_BAD_RESPONSE
            result.status_detail = f"HTTP 200 but not the documented JSON shape ({type(e).__name__})"
            logger.warning(f"Sonar price research: {result.status_detail}")
            return result

        price, status, detail, parsed = _read_price_answer(str(text), currency)
        if price is None:
            result.status, result.status_detail = status, detail
            result.raw_snippets.append(f"sonar_answer: {_redact(text, api_key)[:200]}")
            logger.warning(
                f"Sonar price research: no price [{status}] {detail}; "
                f"answer: {_redact(text, api_key)[:200]!r}"
            )
            return result

        lo, hi = _sanity_band_for(category.lower().strip(), currency)
        if not (lo <= price <= hi):
            result.status = LOOKUP_OUT_OF_BAND
            result.status_detail = (
                f"{price:,.0f} {currency} outside the sanity band "
                f"[{lo:,.0f}, {hi:,.0f}] for {category}"
            )
            logger.warning(f"Sonar price research: {result.status_detail} — discarded")
            return result
        result.launch_price = price
        result.confidence = 60.0
        # Perplexity has been migrating from top-level "citations" (list of
        # URLs) to "search_results" (list of {title,url,...}) — accept both so
        # evidence URLs don't silently vanish from the audit trail.
        cite_urls: list[str] = [str(u) for u in (data.get("citations") or [])]
        for sr in (data.get("search_results") or []):
            if isinstance(sr, dict) and sr.get("url"):
                cite_urls.append(str(sr["url"]))
        seen: set[str] = set()
        for url in cite_urls:
            if url in seen:
                continue
            seen.add(url)
            result.sources.append({"title": "sonar_citation", "url": url, "price": str(price)})
            if len(seen) >= 6:
                break
        if parsed.get("matched_product"):
            result.raw_snippets.append(f"sonar_matched: {parsed['matched_product']}")
        result.status = LOOKUP_OK
        result.status_detail = f"{len(result.sources)} citations ({detail})"
        logger.info(f"Sonar price research: {price} {currency} for {brand} {model}")
    except Exception as e:  # noqa: BLE001
        result.launch_price = None
        result.status = LOOKUP_BAD_RESPONSE
        result.status_detail = f"unexpected error reading the answer ({type(e).__name__})"
        logger.warning(f"Sonar price research failed: {result.status_detail}")
    return result


def combine_new_price_signals(
    gemini_price: float | None, sonar_price: float | None
) -> tuple[float | None, str]:
    """Merge two independent grounded new-price lookups into one estimate.

    - Both agree (within 40%): average them — two independent confirmations.
    - Both present but conflicting: take the LOWER one. Wrong-variant errors
      are almost always inflations (premium bundle/washer-dryer priced instead
      of the base model), and the matrix cross-check downstream still corrects
      a too-low pick when curated data exists.
    - One present: use it. Neither: None.

    Pure function — unit-testable.
    """
    g = float(gemini_price) if gemini_price and gemini_price > 0 else None
    s = float(sonar_price) if sonar_price and sonar_price > 0 else None
    if g and s:
        if max(g, s) / min(g, s) <= 1.4:
            return round((g + s) / 2.0, 2), "gemini+sonar agree"
        return min(g, s), "gemini/sonar conflict -> lower"
    if g:
        return g, "gemini only"
    if s:
        return s, "sonar only"
    return None, "none"


# ---------------------------------------------------------------------------
# Public entry point (v6.3 — Gemini primary + Sonar cross-check)
# ---------------------------------------------------------------------------
def search_internet_price(
    *,
    brand: str,
    model: str,
    category: str,
    condition: str = "",
    country: str = "KE",
    size_value: float | None = None,
    size_unit: str | None = None,
) -> InternetPriceResult:
    """Search for the NEW retail price using two independent grounded sources
    (Gemini Google-Search grounding + Perplexity Sonar) and merge them."""
    gemini = gemini_price_research(
        brand=brand, model=model, category=category, country=country,
        size_value=size_value, size_unit=size_unit,
    )
    sonar = sonar_price_research(
        brand=brand, model=model, category=category, country=country,
        size_value=size_value, size_unit=size_unit,
    )

    providers = {
        "gemini": {"status": gemini.status, "detail": gemini.status_detail},
        "sonar": {"status": sonar.status, "detail": sonar.status_detail},
    }
    gemini.providers = providers
    sonar.providers = providers

    combined, how = combine_new_price_signals(gemini.launch_price, sonar.launch_price)
    if combined is None:
        return gemini  # preserves gemini's currency/snippets even when empty

    # Base the result on the richer Gemini payload, overridden with the merged
    # price + the union of sources for full transparency.
    result = gemini if gemini.launch_price else sonar
    result.launch_price = combined
    result.sources = (gemini.sources or []) + (sonar.sources or [])
    if gemini.launch_price and sonar.launch_price:
        if "agree" in how:
            # Two independent confirmations — boost confidence.
            result.confidence = min(95.0, max(gemini.confidence, sonar.confidence) + 15.0)
        else:
            # Conflict resolved conservatively — disagreement is a reason for
            # LESS certainty, never more.
            result.confidence = min(gemini.confidence, sonar.confidence)
        result.raw_snippets.append(
            f"dual_source: gemini={gemini.launch_price} sonar={sonar.launch_price} -> {combined} ({how})"
        )
    logger.info(f"New-price lookup [{how}]: {combined} for {brand} {model}")
    return result


def internet_lookup_outage(result: InternetPriceResult | None) -> str:
    """"" unless the new-price lookup was DOWN: every configured provider
    failed for an infrastructure reason (HTTP error, timeout, auth...). A
    provider that answered "no price for this item" is an answer, not an
    outage. Returns a short description for the pricing justification."""
    if result is None or result.launch_price:
        return ""
    configured = {
        name: p for name, p in (result.providers or {}).items()
        if p.get("status") != LOOKUP_NOT_CONFIGURED
    }
    if not configured:
        return ""
    if all(p.get("status") in LOOKUP_OUTAGE_STATUSES for p in configured.values()):
        return "; ".join(
            f"{name}: {p.get('status')} ({p.get('detail')})" for name, p in sorted(configured.items())
        )
    return ""


# ---------------------------------------------------------------------------
# TAVILY FALLBACK — kept for rollback, not called in v6
# ---------------------------------------------------------------------------
# def _extract_kes_prices(text: str) -> list[float]:
#     patterns = [
#         r'KES\s*[\d,]+(?:\.\d+)?',
#         r'Ksh\s*[\d,]+(?:\.\d+)?',
#         r'KSh\s*[\d,]+(?:\.\d+)?',
#         r'Kshs?\s*[\d,]+(?:\.\d+)?',
#         r'[\d,]+(?:\.\d+)?\s*KES',
#         r'[\d,]+(?:\.\d+)?\s*Ksh',
#     ]
#     prices = []
#     for pat in patterns:
#         for match in re.finditer(pat, text, re.IGNORECASE):
#             digits = re.sub(r'[^0-9.]', '', match.group())
#             try:
#                 val = float(digits)
#                 if 500 <= val <= 2_000_000:
#                     prices.append(val)
#             except ValueError:
#                 pass
#     return prices
#
# def search_internet_price_tavily(*, brand, model, category, condition=""):
#     """TAVILY FALLBACK — uncomment to re-enable."""
#     result = InternetPriceResult()
#     try:
#         from app.config import get_settings
#         settings = get_settings()
#         api_key = settings.tavily_api_key
#         if not api_key:
#             return result
#         from tavily import TavilyClient
#         client = TavilyClient(api_key=api_key)
#         product_desc = f"{brand} {model}".strip() or category
#         queries = [
#             f"{product_desc} price Kenya KES",
#             f"{product_desc} {category} retail price",
#         ]
#         all_prices = []
#         for query in queries:
#             try:
#                 response = client.search(query=query, search_depth="basic", max_results=5, include_answer=True)
#                 answer = response.get("answer", "")
#                 if answer:
#                     result.raw_snippets.append(answer)
#                     all_prices.extend(_extract_kes_prices(answer))
#                 for r in response.get("results", []):
#                     content = r.get("content", "")
#                     title = r.get("title", "")
#                     url = r.get("url", "")
#                     snippet = f"{title}: {content}"
#                     prices_found = _extract_kes_prices(snippet)
#                     all_prices.extend(prices_found)
#                     if prices_found:
#                         result.sources.append({"title": title, "url": url, "price": str(prices_found[0])})
#                     result.raw_snippets.append(snippet[:200])
#             except Exception as e:
#                 continue
#         if all_prices:
#             all_prices.sort()
#             median = all_prices[len(all_prices) // 2]
#             filtered = [p for p in all_prices if p <= median * 3]
#             if filtered:
#                 result.launch_price = max(filtered)
#                 result.current_resale_low = min(filtered)
#                 result.current_resale_high = filtered[int(len(filtered) * 0.75)] if len(filtered) > 2 else max(filtered)
#                 result.confidence = min(90.0, len(filtered) * 15.0)
#     except ImportError:
#         pass
#     except Exception as e:
#         logger.error(f"Tavily price lookup failed: {e}")
#     return result

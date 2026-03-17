"""
Flowcart webhook for WhatsApp integration.

Receives inbound messages from Flowcart, manages conversation state,
and sends responses via the Flowcart API.

Endpoint:
  POST /webhook/flowcart  — Receives WhatsApp messages
  GET  /webhook/flowcart   — Health check / verification
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from loguru import logger
from pydantic import BaseModel, Field

from app.config import get_settings

flowcart_router = APIRouter()


# ---------------------------------------------------------------------------
# Conversation states
# ---------------------------------------------------------------------------
class ConvoState(str, Enum):
    IDLE = "idle"
    GREETING = "greeting"
    CATEGORY = "category"
    BRAND = "brand"
    MODEL = "model"
    AGE = "age"
    CONDITION = "condition"
    OWNERSHIP = "ownership"
    ISSUES = "issues"
    PHOTOS = "photos"
    PRICE_ASK = "price_ask"
    ANALYZING = "analyzing"
    NEGOTIATING = "negotiating"
    DEAL_CLOSED = "deal_closed"


# ---------------------------------------------------------------------------
# In-memory session store (swap with Redis in production)
# ---------------------------------------------------------------------------
_wa_sessions: dict[str, dict] = {}


def _get_session(phone: str) -> dict:
    if phone not in _wa_sessions:
        _wa_sessions[phone] = {
            "session_id": f"wa_{uuid.uuid4().hex[:12]}",
            "phone": phone,
            "state": ConvoState.IDLE,
            "language": "en",
            "answers": {},
            "photos": [],
            "evaluation": None,
            "negotiation": {"round": 0, "status": "idle"},
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        }
    return _wa_sessions[phone]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class FlowcartMessage(BaseModel):
    type: str = Field(..., description="Event type, e.g. 'message.received'")
    from_number: str = Field("", alias="from", description="Sender phone number")
    text: str | None = Field(None, description="Message text")
    media_url: str | None = Field(None, description="Media URL for images/voice")
    media_type: str | None = Field(None, description="Media content type")
    timestamp: str | None = None


class FlowcartResponse(BaseModel):
    status: str = "ok"
    session_id: str | None = None
    reply_text: str | None = None
    reply_buttons: list[dict] | None = None


# ---------------------------------------------------------------------------
# Webhook verification (GET)
# ---------------------------------------------------------------------------
@flowcart_router.get("/flowcart")
async def flowcart_verify(challenge: str = ""):
    """Webhook verification for Flowcart setup."""
    return {"challenge": challenge, "status": "verified"}


# ---------------------------------------------------------------------------
# Webhook (POST)
# ---------------------------------------------------------------------------
@flowcart_router.post("/flowcart", response_model=FlowcartResponse)
async def flowcart_webhook(
    request: Request,
    x_flowcart_signature: str | None = Header(None),
):
    """
    Receive inbound WhatsApp messages from Flowcart.
    Verifies HMAC signature, processes message, returns reply.
    """
    settings = get_settings()
    body = await request.body()

    # HMAC verification (if secret configured)
    if settings.flowcart_webhook_secret and x_flowcart_signature:
        expected = hmac.new(
            settings.flowcart_webhook_secret.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(expected, x_flowcart_signature):
            logger.warning("Flowcart webhook HMAC verification failed")
            raise HTTPException(403, "Invalid signature")

    # Parse payload
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON")

    event_type = payload.get("type", "")
    if event_type != "message.received":
        return FlowcartResponse(status="ignored")

    phone = payload.get("from", "unknown")
    text = payload.get("text", "").strip()
    media_url = payload.get("media_url")

    session = _get_session(phone)
    reply = process_message(session, text, media_url)

    return FlowcartResponse(
        status="ok",
        session_id=session["session_id"],
        reply_text=reply["text"],
        reply_buttons=reply.get("buttons"),
    )


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------
def process_message(session: dict, text: str, media_url: str | None) -> dict:
    """
    Process an inbound message and advance the conversation state.
    Returns a dict with 'text' and optional 'buttons'.
    """
    current_state = session["state"]
    text_lower = text.lower().strip()

    # Auto-detect language
    swahili_words = ["habari", "niaje", "sasa", "mambo", "nataka", "kuuza"]
    if any(w in text_lower for w in swahili_words) and session["language"] == "en":
        session["language"] = "sw"

    # --- State handlers ---
    if current_state == ConvoState.IDLE:
        session["state"] = ConvoState.CATEGORY
        return {
            "text": (
                "Habari! 👋 I'm Kay from GreenBay.\n\n"
                "I can give you an instant offer for your used appliance — "
                "fridge, TV, washing machine, cooker, you name it!\n\n"
                "What type of appliance are you selling?"
            ),
            "buttons": [
                {"type": "reply", "title": "🧊 Refrigerator"},
                {"type": "reply", "title": "🧺 Washing Machine"},
                {"type": "reply", "title": "📺 TV / Monitor"},
            ],
        }

    if current_state == ConvoState.CATEGORY:
        # Map various inputs to categories
        cat_map = {
            "fridge": "refrigerator", "refrigerator": "refrigerator",
            "washing": "washing_machine", "washer": "washing_machine",
            "tv": "tv_monitor", "television": "tv_monitor", "monitor": "tv_monitor",
            "cooker": "cooker_oven", "oven": "cooker_oven",
            "microwave": "microwave",
            "ac": "air_conditioner", "air": "air_conditioner",
            "water": "water_dispenser", "dispenser": "water_dispenser",
        }

        found = None
        for key, val in cat_map.items():
            if key in text_lower:
                found = val
                break

        if not found:
            return {
                "text": "I didn't catch that. What type of appliance is it? 🤔",
                "buttons": [
                    {"type": "reply", "title": "🧊 Fridge"},
                    {"type": "reply", "title": "📺 TV"},
                    {"type": "reply", "title": "🧺 Washer"},
                ],
            }

        session["answers"]["category"] = found
        session["state"] = ConvoState.BRAND
        return {
            "text": f"A {found.replace('_', ' ').title()}! 👍\n\nWhat brand is it?",
            "buttons": [
                {"type": "reply", "title": "Samsung"},
                {"type": "reply", "title": "LG"},
                {"type": "reply", "title": "Hisense"},
            ],
        }

    if current_state == ConvoState.BRAND:
        session["answers"]["brand"] = text.strip()
        session["state"] = ConvoState.MODEL
        return {
            "text": f"{text.strip()} — nice! 🏷️\n\nDo you know the model name or number? (e.g. RT34, WW90T, 43LM6300)",
            "buttons": [
                {"type": "reply", "title": "I don't know"},
            ],
        }

    if current_state == ConvoState.MODEL:
        if "don't know" in text_lower or "not sure" in text_lower or "no" == text_lower:
            session["answers"]["model"] = ""
        else:
            session["answers"]["model"] = text.strip()
        session["state"] = ConvoState.AGE
        return {
            "text": "How old is it (approximately)?",
            "buttons": [
                {"type": "reply", "title": "Under 1 year"},
                {"type": "reply", "title": "1-3 years"},
                {"type": "reply", "title": "3-5 years"},
            ],
        }

    if current_state == ConvoState.AGE:
        # Parse age
        age = 2.0  # default
        if "under 1" in text_lower or "new" in text_lower:
            age = 0.5
        elif "1-3" in text_lower or "1 to 3" in text_lower:
            age = 2.0
        elif "3-5" in text_lower or "3 to 5" in text_lower:
            age = 4.0
        elif "5" in text_lower or "old" in text_lower:
            age = 6.0

        session["answers"]["age_years"] = age
        session["state"] = ConvoState.CONDITION
        return {
            "text": "Got it! What condition is it in? Be honest 😊",
            "buttons": [
                {"type": "reply", "title": "✨ Like New"},
                {"type": "reply", "title": "👍 Good / Used"},
                {"type": "reply", "title": "⚠️ Has Issues"},
            ],
        }

    if current_state == ConvoState.CONDITION:
        grade = "B"
        score = 65
        if "new" in text_lower or "excellent" in text_lower:
            grade, score = "A", 90
        elif "good" in text_lower or "used" in text_lower:
            grade, score = "B", 65
        elif "issue" in text_lower or "problem" in text_lower or "broken" in text_lower:
            grade, score = "C", 40

        session["answers"]["condition_grade"] = grade
        session["answers"]["condition_score"] = score
        session["state"] = ConvoState.PHOTOS
        return {
            "text": (
                f"Condition: Grade {grade} 📊\n\n"
                "Now please send me at least 3 photos:\n"
                "📸 Front view\n"
                "📸 Back / Model label\n"
                "📸 Any damage areas\n\n"
                "Send them one by one — I'll count!"
            ),
        }

    if current_state == ConvoState.PHOTOS:
        if media_url:
            session["photos"].append(media_url)
            count = len(session["photos"])
            if count < 3:
                return {"text": f"📸 Photo {count}/3 received. Send {3 - count} more!"}
            else:
                session["state"] = ConvoState.PRICE_ASK
                return {
                    "text": f"📸 All {count} photos received! ✅\n\nLast question — what price are you hoping for (in KES)?",
                    "buttons": [
                        {"type": "reply", "title": "💡 Make me an offer"},
                    ],
                }
        else:
            return {"text": "Please send photos of your appliance 📸"}

    if current_state == ConvoState.PRICE_ASK:
        price = None
        if "offer" in text_lower or "you decide" in text_lower:
            price = None
        else:
            try:
                cleaned = text_lower.replace(",", "").replace("kes", "").strip()
                if cleaned.endswith("k"):
                    price = float(cleaned[:-1]) * 1000
                else:
                    price = float(cleaned)
            except ValueError:
                price = None

        session["answers"]["seller_asking_price"] = price
        session["state"] = ConvoState.ANALYZING

        # Call the REAL evaluator engine (5-point pricing)
        eval_result = _call_evaluator(session)

        if eval_result:
            offer = eval_result.get("opening_offer", 0)
            session["evaluation"] = eval_result
            session["evaluation_session_id"] = eval_result.get("session_id")
            session["state"] = ConvoState.NEGOTIATING

            # Build verification info
            pv = eval_result.get("price_verification")
            source_info = ""
            if pv and pv.get("num_sources", 0) > 1:
                source_info = f"\n📊 Price verified against {pv['num_sources']} market sources"

            return {
                "text": (
                    f"🔍 Analyzing your {session['answers'].get('brand', 'appliance')}...\n\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"💰 GreenBay Offer:\n"
                    f"*KES {offer:,.0f}*\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"{source_info}\n"
                    f"Grade {eval_result.get('condition_grade', 'B')} | "
                    f"Confidence: {eval_result.get('confidence_score', 0):.0f}%\n\n"
                    f"Valid for 7 days.\n"
                    f"Free pickup + same-day M-Pesa payment!"
                ),
                "buttons": [
                    {"type": "reply", "title": "✅ Accept"},
                    {"type": "reply", "title": "💬 Counter"},
                    {"type": "reply", "title": "❌ Decline"},
                ],
            }
        else:
            # Fallback to quick estimate if evaluator API fails
            offer = _quick_estimate(session)
            session["evaluation"] = {"opening_offer": offer}
            session["state"] = ConvoState.NEGOTIATING

            return {
                "text": (
                    f"🔍 Analyzing your {session['answers'].get('brand', 'appliance')}...\n\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"💰 GreenBay Offer:\n"
                    f"*KES {offer:,.0f}*\n"
                    f"━━━━━━━━━━━━━━━━\n\n"
                    f"Valid for 7 days.\n"
                    f"Free pickup + same-day M-Pesa payment!"
                ),
                "buttons": [
                    {"type": "reply", "title": "✅ Accept"},
                    {"type": "reply", "title": "💬 Counter"},
                    {"type": "reply", "title": "❌ Decline"},
                ],
            }

    if current_state == ConvoState.NEGOTIATING:
        eval_data = session.get("evaluation", {})
        offer = eval_data.get("opening_offer", 0) if isinstance(eval_data, dict) else eval_data

        if "accept" in text_lower or "yes" in text_lower or "deal" in text_lower:
            session["state"] = ConvoState.DEAL_CLOSED
            return {
                "text": (
                    "🎉 Deal confirmed!\n\n"
                    f"Amount: KES {offer:,.0f}\n"
                    f"Ref: GB-{session['session_id']}\n\n"
                    "📍 Our team will call within 24 hours to arrange FREE pickup.\n"
                    "⚡ Payment via M-Pesa — same day.\n\n"
                    "Asante sana! 💚"
                ),
            }
        elif "decline" in text_lower or "no" in text_lower:
            session["state"] = ConvoState.DEAL_CLOSED
            return {
                "text": (
                    f"I understand. Our offer of KES {offer:,.0f} "
                    "stands for 7 days.\n\n"
                    "Feel free to reach out anytime! 💚"
                ),
            }
        else:
            # Counter-offer — try to call the real counter API
            counter_result = _handle_counter(session, text)
            if counter_result:
                return counter_result

            return {
                "text": (
                    f"I appreciate the counter. Unfortunately, KES {offer:,.0f} "
                    "is the best I can offer for this unit based on current market data.\n\n"
                    "Would you like to accept?"
                ),
                "buttons": [
                    {"type": "reply", "title": "✅ Accept"},
                    {"type": "reply", "title": "❌ No thanks"},
                ],
            }

    if current_state == ConvoState.DEAL_CLOSED:
        session["state"] = ConvoState.IDLE
        _wa_sessions.pop(session["phone"], None)
        return {
            "text": "Would you like to sell another appliance? Just say 'sell' to start! 🔄",
        }

    # Fallback
    return {"text": "Type 'sell' to start a new valuation! 📱"}


# ---------------------------------------------------------------------------
# Image download helper
# ---------------------------------------------------------------------------
def _download_image_as_base64(media_url: str) -> str | None:
    """Download an image from a Flowcart/WhatsApp media URL and return as base64."""
    import base64

    import httpx

    try:
        settings = get_settings()
        headers = {}
        # If this is a WhatsApp media URL, add auth token
        if "graph.facebook.com" in media_url or "whatsapp" in media_url.lower():
            token = getattr(settings, "whatsapp_api_token", None)
            if token:
                headers["Authorization"] = f"Bearer {token}"

        with httpx.Client(timeout=15.0) as client:
            resp = client.get(media_url, headers=headers, follow_redirects=True)
            resp.raise_for_status()
            return base64.b64encode(resp.content).decode("utf-8")
    except Exception as e:
        logger.warning(f"Failed to download image from {media_url}: {e}")
        return None


# ---------------------------------------------------------------------------
# Real evaluator API call
# ---------------------------------------------------------------------------
def _call_evaluator(session: dict) -> dict | None:
    """Call the real /tradein/evaluate endpoint with session data.

    Downloads photos from media URLs, encodes as base64, and submits
    to the same evaluator engine that the web frontend uses.
    """
    import httpx

    answers = session.get("answers", {})
    photos = session.get("photos", [])

    # Download and encode images as base64
    image_data = []
    for url in photos:
        b64 = _download_image_as_base64(url)
        if b64:
            image_data.append(b64)

    # Map category retail price estimates for the required retail_price field
    cat_retail = {
        "refrigerator": 65000, "washing_machine": 55000, "tv_monitor": 45000,
        "cooker_oven": 40000, "microwave": 15000, "air_conditioner": 50000,
        "water_dispenser": 20000, "other": 30000,
    }
    retail = cat_retail.get(answers.get("category", "other"), 30000)

    # Build the EvaluateRequest payload
    payload = {
        "category": answers.get("category", "other"),
        "brand": answers.get("brand", "Unknown"),
        "model": answers.get("model", ""),
        "age_years": answers.get("age_years", 2.0),
        "condition_grade": answers.get("condition_grade", "B"),
        "condition_score": answers.get("condition_score", 65),
        "defects": [],
        "seller_asking_price": answers.get("seller_asking_price"),
        "seller_name": None,
        "seller_phone": session.get("phone"),
        "image_urls": photos,
        "image_data": image_data,
        "retail_price": retail,
        "retail_price_source": "whatsapp_category_estimate",
    }

    try:
        # Internal call to our own API
        with httpx.Client(timeout=120.0) as client:
            resp = client.post(
                "http://127.0.0.1:8000/tradein/evaluate",
                json=payload,
            )
            resp.raise_for_status()
            result = resp.json()
            logger.info(
                f"WhatsApp evaluation complete: session={result.get('session_id')}, "
                f"offer={result.get('opening_offer')}, "
                f"decision={result.get('decision')}"
            )
            return result
    except Exception as e:
        logger.error(f"Evaluator API call failed for WhatsApp session: {e}")
        return None


# ---------------------------------------------------------------------------
# Counter-offer via real API
# ---------------------------------------------------------------------------
def _handle_counter(session: dict, text: str) -> dict | None:
    """Process a counter-offer through the real negotiation API."""
    import httpx

    eval_session_id = session.get("evaluation_session_id")
    if not eval_session_id:
        return None

    # Parse counter amount
    try:
        cleaned = text.lower().replace(",", "").replace("kes", "").strip()
        if cleaned.endswith("k"):
            counter_amount = float(cleaned[:-1]) * 1000
        else:
            counter_amount = float(cleaned)
    except ValueError:
        return None

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                f"http://127.0.0.1:8000/tradein/{eval_session_id}/counter",
                json={"seller_counter": counter_amount},
            )
            resp.raise_for_status()
            result = resp.json()

        decision = result.get("decision", "")
        system_offer = result.get("system_offer", 0)
        rounds_remaining = result.get("rounds_remaining", 0)

        # Update session with latest offer
        if isinstance(session.get("evaluation"), dict):
            session["evaluation"]["opening_offer"] = system_offer

        if decision == "accept":
            session["state"] = ConvoState.DEAL_CLOSED
            return {
                "text": (
                    f"✅ We can do *KES {system_offer:,.0f}*!\n\n"
                    "🎉 Deal confirmed!\n"
                    f"Ref: GB-{session['session_id']}\n\n"
                    "📍 Our team will call within 24 hours for FREE pickup.\n"
                    "⚡ M-Pesa payment — same day.\n\n"
                    "Asante sana! 💚"
                ),
            }
        elif decision == "counter":
            return {
                "text": (
                    f"I can adjust to *KES {system_offer:,.0f}*.\n"
                    f"({rounds_remaining} negotiation rounds remaining)\n\n"
                    "Would this work for you?"
                ),
                "buttons": [
                    {"type": "reply", "title": "✅ Accept"},
                    {"type": "reply", "title": "❌ No thanks"},
                ],
            }
        else:
            return None

    except Exception as e:
        logger.warning(f"Counter API call failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Fallback estimator (used when evaluator API is unavailable)
# ---------------------------------------------------------------------------
def _quick_estimate(session: dict) -> float:
    """Quick locally-computed estimate — fallback only."""
    cat_retail = {
        "refrigerator": 65000, "washing_machine": 55000, "tv_monitor": 45000,
        "cooker_oven": 40000, "microwave": 15000, "air_conditioner": 50000,
        "water_dispenser": 20000, "other": 30000,
    }
    grade_mult = {"A": 0.55, "B": 0.45, "C": 0.35, "D": 0.20}

    retail = cat_retail.get(session["answers"].get("category", "other"), 30000)
    age = session["answers"].get("age_years", 2)
    grade = session["answers"].get("condition_grade", "B")

    depr = max(0.05, 0.85 ** age)
    offer = retail * depr * grade_mult.get(grade, 0.45)
    return round(offer / 100) * 100


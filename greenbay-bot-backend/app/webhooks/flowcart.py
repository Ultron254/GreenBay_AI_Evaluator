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
        session["state"] = ConvoState.AGE
        return {
            "text": f"{text.strip()} — nice! 🏷️\n\nHow old is it (approximately)?",
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

        # In production, trigger evaluation API here
        offer = _quick_estimate(session)
        session["evaluation"] = offer
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
        if "accept" in text_lower or "yes" in text_lower or "deal" in text_lower:
            session["state"] = ConvoState.DEAL_CLOSED
            return {
                "text": (
                    "🎉 Deal confirmed!\n\n"
                    f"Amount: KES {session['evaluation']:,.0f}\n"
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
                    f"I understand. Our offer of KES {session['evaluation']:,.0f} "
                    "stands for 7 days.\n\n"
                    "Feel free to reach out anytime! 💚"
                ),
            }
        else:
            # Counter-offer
            return {
                "text": (
                    f"I appreciate the counter. Unfortunately, KES {session['evaluation']:,.0f} "
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


def _quick_estimate(session: dict) -> float:
    """Quick locally-computed estimate for WhatsApp (no API call)."""
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

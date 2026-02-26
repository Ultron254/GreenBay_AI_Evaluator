"""
Chat router for the GreenBay AI Evaluator web app.

Endpoints:
  POST /tradein/chat    — Conversational AI (text + optional images)
  POST /tradein/upload  — Photo upload with validation
"""

from __future__ import annotations

import base64
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from loguru import logger
from pydantic import BaseModel, Field

from app.config import get_settings
from greenbay_ai_evaluator.services.vision_service import analyze_images

chat_router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    session_id: str | None = Field(None, description="Existing session ID or null for new")
    message: str = Field(..., description="User message text")
    images: list[str] = Field(default_factory=list, description="Base64-encoded images")
    context: dict[str, Any] = Field(default_factory=dict, description="Current wizard answers")


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    analysis: dict[str, Any] | None = None
    suggestions: list[str] = Field(default_factory=list)


class UploadResponse(BaseModel):
    filename: str
    size_bytes: int
    content_type: str
    image_base64: str


# ---------------------------------------------------------------------------
# In-memory session store (swap with Redis in production)
# ---------------------------------------------------------------------------
_sessions: dict[str, dict] = {}


def _get_or_create_session(session_id: str | None) -> tuple[str, dict]:
    if session_id and session_id in _sessions:
        return session_id, _sessions[session_id]

    sid = session_id or f"web_{uuid.uuid4().hex[:12]}"
    _sessions[sid] = {
        "created_at": datetime.utcnow().isoformat(),
        "channel": "web",
        "history": [],
        "context": {},
    }
    return sid, _sessions[sid]


# ---------------------------------------------------------------------------
# POST /tradein/chat
# ---------------------------------------------------------------------------
@chat_router.post("/chat", response_model=ChatResponse)
async def chat_endpoint(req: ChatRequest):
    """
    Conversational AI endpoint for the web app.
    Accepts text + optional images, returns AI response.
    """
    settings = get_settings()
    sid, session = _get_or_create_session(req.session_id)

    # Store message in history
    session["history"].append({
        "role": "user",
        "content": req.message,
        "has_images": len(req.images) > 0,
        "timestamp": datetime.utcnow().isoformat(),
    })

    # Merge context
    session["context"].update(req.context)

    analysis = None
    suggestions = []

    # If images are provided, run vision analysis
    if req.images:
        try:
            analysis = await analyze_images(
                images_base64=req.images,
                category=session["context"].get("category"),
                brand_hint=session["context"].get("brand"),
                model_hint=session["context"].get("model"),
                api_key=settings.anthropic_api_key,
                primary_model=settings.anthropic_primary_model,
                fallback_model=settings.anthropic_fallback_model,
            )
            logger.info(f"Vision analysis completed for session {sid}")
        except Exception as e:
            logger.error(f"Vision analysis error: {e}")

    # Build reply
    reply = _build_reply(req.message, session["context"], analysis)

    # Store bot reply in history
    session["history"].append({
        "role": "assistant",
        "content": reply,
        "timestamp": datetime.utcnow().isoformat(),
    })

    return ChatResponse(
        session_id=sid,
        reply=reply,
        analysis=analysis,
        suggestions=suggestions,
    )


def _build_reply(
    message: str,
    context: dict[str, Any],
    analysis: dict[str, Any] | None,
) -> str:
    """Build a contextual reply based on the current conversation state."""
    msg_lower = message.lower().strip()

    # Greeting
    greetings = ["hi", "hello", "hey", "habari", "sasa", "mambo", "niaje"]
    if any(msg_lower.startswith(g) for g in greetings):
        return (
            "Hey! 👋 I'm Kay, your GreenBay evaluator. "
            "I'll help you get the best price for your appliance. "
            "What kind of appliance are you selling?"
        )

    # If vision analysis was done
    if analysis:
        brand = analysis.get("brand_detected") or context.get("brand", "your appliance")
        grade = analysis.get("condition_grade", "B")
        score = analysis.get("condition_score", 65)
        defect_count = len(analysis.get("defects", []))
        safety = analysis.get("safety_concerns", [])

        parts = [f"I've analyzed your photos of the **{brand}**!"]
        parts.append(f"📊 Condition: Grade **{grade}** (score: {score}/100)")

        if defect_count > 0:
            parts.append(f"🔍 I found {defect_count} defect{'s' if defect_count != 1 else ''}")

        if safety:
            parts.append("⚠️ **Safety note:** " + safety[0].get("issue", "Please check for safety issues"))

        parts.append("Shall I proceed with the valuation?")
        return "\n\n".join(parts)

    # Generic acknowledgement
    if context.get("category"):
        cat = context["category"].replace("_", " ").title()
        return f"Got it! A {cat}. Let me know the brand and I'll start looking up market prices. 🔍"

    return (
        "Thanks for sharing! Please continue filling in the form "
        "— I'll have your valuation ready once we have all the details. 😊"
    )


# ---------------------------------------------------------------------------
# POST /tradein/upload
# ---------------------------------------------------------------------------
@chat_router.post("/upload", response_model=UploadResponse)
async def upload_photo(file: UploadFile = File(...)):
    """
    Upload a single photo for analysis.
    Returns the image as base64 for client-side rendering.
    """
    # Validate content type
    allowed = {"image/jpeg", "image/png", "image/webp"}
    if file.content_type not in allowed:
        raise HTTPException(400, f"Invalid file type: {file.content_type}. Allowed: {', '.join(allowed)}")

    # Read file
    contents = await file.read()
    size = len(contents)

    # Max 10MB
    if size > 10 * 1024 * 1024:
        raise HTTPException(400, f"File too large: {size} bytes. Maximum 10MB.")

    # Encode as base64
    b64 = base64.b64encode(contents).decode("utf-8")
    data_url = f"data:{file.content_type};base64,{b64}"

    return UploadResponse(
        filename=file.filename or "upload.jpg",
        size_bytes=size,
        content_type=file.content_type,
        image_base64=data_url,
    )

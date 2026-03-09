"""
Chat router for the GreenBay AI Evaluator web app.

Endpoints:
  POST /tradein/chat    - Conversational AI (text + optional images)
  POST /tradein/upload  - Photo upload with validation
"""

from __future__ import annotations

import base64
import json
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
# Session store: Redis with in-memory fallback
# ---------------------------------------------------------------------------
_SESSION_TTL = 86400  # 24 hours
_redis_client = None
_redis_warned = False
_memory_sessions: dict[str, dict] = {}  # fallback


def _get_redis():
    """Get or create Redis client. Returns None if unavailable."""
    global _redis_client, _redis_warned
    if _redis_client is not None:
        return _redis_client
    try:
        import redis
        settings = get_settings()
        _redis_client = redis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            db=settings.redis_db,
            password=settings.redis_password,
            decode_responses=True,
            socket_connect_timeout=2,
        )
        _redis_client.ping()
        logger.info(f"Chat sessions using Redis at {settings.redis_host}:{settings.redis_port}")
        return _redis_client
    except Exception as e:
        if not _redis_warned:
            logger.warning(f"Redis unavailable ({e}), using in-memory sessions")
            _redis_warned = True
        _redis_client = None
        return None


def _get_or_create_session(session_id: str | None) -> tuple[str, dict]:
    r = _get_redis()
    if r:
        # Redis path
        if session_id:
            data = r.get(f"chat_session:{session_id}")
            if data:
                return session_id, json.loads(data)

        sid = session_id or f"web_{uuid.uuid4().hex[:12]}"
        session = {
            "created_at": datetime.utcnow().isoformat(),
            "channel": "web",
            "history": [],
            "context": {},
        }
        r.setex(f"chat_session:{sid}", _SESSION_TTL, json.dumps(session))
        return sid, session
    else:
        # In-memory fallback
        if session_id and session_id in _memory_sessions:
            return session_id, _memory_sessions[session_id]

        sid = session_id or f"web_{uuid.uuid4().hex[:12]}"
        _memory_sessions[sid] = {
            "created_at": datetime.utcnow().isoformat(),
            "channel": "web",
            "history": [],
            "context": {},
        }
        return sid, _memory_sessions[sid]


def _save_session(session_id: str, session: dict):
    """Persist session back to Redis (no-op for in-memory)."""
    r = _get_redis()
    if r:
        r.setex(f"chat_session:{session_id}", _SESSION_TTL, json.dumps(session))


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

    # Persist session to Redis
    _save_session(sid, session)

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
    """Build a contextual reply using Claude LLM, with rule-based fallback."""
    settings = get_settings()

    # Try LLM-powered reply if API key is available
    if settings.anthropic_api_key:
        try:
            return _llm_reply(message, context, analysis, settings)
        except Exception as e:
            logger.warning(f"LLM reply failed, using fallback: {e}")

    # Fallback: rule-based replies
    return _rule_based_reply(message, context, analysis)


def _llm_reply(
    message: str,
    context: dict[str, Any],
    analysis: dict[str, Any] | None,
    settings,
) -> str:
    """Generate a reply using Claude."""
    try:
        import anthropic
    except ImportError:
        return _rule_based_reply(message, context, analysis)

    system_prompt = """You are Kay, the GreenBay Market AI evaluator assistant.
You help sellers in Kenya get the best price for their used appliances and electronics.

Key rules:
- Be warm, friendly, and professional (like a Kenyan market expert)
- Keep responses SHORT (2-3 sentences max)
- Reference the wizard steps on the left panel when guiding the user
- Never make up prices or valuations, only the engine does that
- If the user asks about pricing, tell them to complete all steps for an accurate offer
- Never use markdown formatting (no **, no ##, no bullet points)
- Use Kenyan English naturally
- Do NOT reveal internal scores, grades, or technical details
- Currency is always KES (Kenya Shillings)"""

    # Build context summary
    ctx_parts = []
    if context.get("category"):
        ctx_parts.append(f"Category: {context['category'].replace('_', ' ').title()}")
    if context.get("brand"):
        ctx_parts.append(f"Brand: {context['brand']}")
    if context.get("model"):
        ctx_parts.append(f"Model: {context['model']}")
    if context.get("condition"):
        ctx_parts.append(f"Condition: {context['condition']}")
    if context.get("age") is not None:
        ctx_parts.append(f"Age: {context['age']} years")

    user_content = message
    if ctx_parts:
        user_content += f"\n\n[Wizard context: {', '.join(ctx_parts)}]"
    if analysis:
        user_content += f"\n\n[Vision analysis: grade={analysis.get('condition_grade')}, score={analysis.get('condition_score')}, defects={len(analysis.get('defects', []))}]"

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    response = client.messages.create(
        model=settings.anthropic_fallback_model,  # Use Sonnet for speed
        max_tokens=200,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )

    reply = response.content[0].text.strip()
    # Clean up any markdown that slipped through
    reply = reply.replace("**", "").replace("##", "").replace("- ", "")
    return reply


def _rule_based_reply(
    message: str,
    context: dict[str, Any],
    analysis: dict[str, Any] | None,
) -> str:
    """Fallback rule-based reply when LLM is unavailable."""
    msg_lower = message.lower().strip()

    greetings = ["hi", "hello", "hey", "habari", "sasa", "mambo", "niaje"]
    if any(msg_lower.startswith(g) for g in greetings):
        return (
            "Hey! I'm Kay, your GreenBay evaluator. "
            "I'll help you get the best price for your appliance. "
            "What kind of appliance are you selling?"
        )

    if analysis:
        brand = analysis.get("brand_detected") or context.get("brand", "your appliance")
        grade = analysis.get("condition_grade", "B")
        score = analysis.get("condition_score", 65)
        defect_count = len(analysis.get("defects", []))

        parts = [f"I've analyzed your photos of the {brand}!"]
        parts.append(f"Condition: Grade {grade} (score: {score}/100)")
        if defect_count > 0:
            parts.append(f"I found {defect_count} defect{'s' if defect_count != 1 else ''}")
        parts.append("Shall I proceed with the valuation?")
        return " ".join(parts)

    if context.get("category"):
        cat = context["category"].replace("_", " ").title()
        return f"Got it! A {cat}. Let me know the brand and I'll start looking up market prices."

    return (
        "Thanks for sharing! Please continue filling in the form "
        "and I'll have your valuation ready once we have all the details."
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

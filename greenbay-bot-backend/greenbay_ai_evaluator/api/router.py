"""
FastAPI router for the GreenBay AI Evaluator.

Endpoints:
  POST  /evaluate              — Run deterministic valuation
  POST  /{session_id}/counter  — Process a seller counter-offer
  GET   /{session_id}          — Retrieve full session detail
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from sqlalchemy.orm import Session

from app.database.db import get_db
from greenbay_ai_evaluator.api.schemas import (
    CounterRequest,
    CounterResponse,
    DecisionLedgerOut,
    EvaluateRequest,
    EvaluateResponse,
    NegotiationRoundOut,
    SessionDetailResponse,
)
from greenbay_ai_evaluator.config import CONDITION_GRADE_MAP
from greenbay_ai_evaluator.engine.negotiation_engine import (
    NegotiationState,
    process_counter,
)
from greenbay_ai_evaluator.engine.offer_engine import (
    Comparable,
    PricingPolicyData,
    compute_valuation,
)
from greenbay_ai_evaluator.models.evaluator_models import (
    DecisionLedger,
    NegotiationRound,
    PricingPolicy,
    ValuationSession,
)
from greenbay_ai_evaluator.services.comparables_service import get_comparables
from greenbay_ai_evaluator.services.image_quality_service import score_images
from greenbay_ai_evaluator.services.risk_service import assess_risk
from greenbay_ai_evaluator.services.vision_service import analyze_images

evaluator_router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _load_policy(category: str, db: Session) -> PricingPolicy:
    """Load active pricing policy for *category* or raise 400."""
    policy = (
        db.query(PricingPolicy)
        .filter(
            PricingPolicy.category == category.lower().strip(),
            PricingPolicy.is_active.is_(True),
        )
        .first()
    )
    if not policy:
        raise HTTPException(
            status_code=400,
            detail=f"No active pricing policy found for category '{category}'.",
        )
    return policy


def _policy_to_data(policy: PricingPolicy) -> PricingPolicyData:
    return PricingPolicyData(
        category=policy.category,
        margin_pct=policy.margin_pct,
        max_offer_pct=policy.max_offer_pct,
        walkaway_pct=policy.walkaway_pct,
        depreciation_year1=policy.depreciation_year1 or 0.20,
        depreciation_year2_3=policy.depreciation_year2_3 or 0.12,
        depreciation_year4_5=policy.depreciation_year4_5 or 0.10,
        depreciation_year6_plus=policy.depreciation_year6_plus or 0.08,
        round_step_pct=policy.round_step_pct or 0.05,
        max_negotiation_rounds=policy.max_negotiation_rounds or 3,
        brand_premium_json=policy.brand_premium_json or {},
        condition_multiplier_json=policy.condition_multiplier_json or {},
    )


def _policy_snapshot(policy: PricingPolicy) -> dict:
    return {
        "category": policy.category,
        "margin_pct": policy.margin_pct,
        "max_offer_pct": policy.max_offer_pct,
        "walkaway_pct": policy.walkaway_pct,
        "depreciation_year1": policy.depreciation_year1,
        "depreciation_year2_3": policy.depreciation_year2_3,
        "depreciation_year4_5": policy.depreciation_year4_5,
        "depreciation_year6_plus": policy.depreciation_year6_plus,
        "round_step_pct": policy.round_step_pct,
        "max_negotiation_rounds": policy.max_negotiation_rounds,
        "brand_premium_json": policy.brand_premium_json,
        "condition_multiplier_json": policy.condition_multiplier_json,
        "updated_at": str(policy.updated_at) if policy.updated_at else str(policy.created_at),
    }


# ---------------------------------------------------------------------------
# POST /evaluate
# ---------------------------------------------------------------------------
@evaluator_router.post("/evaluate", response_model=EvaluateResponse)
def evaluate_trade_in(req: EvaluateRequest, db: Session = Depends(get_db)):
    """Create a deterministic valuation for a trade-in submission."""
    try:
        # 1. Load pricing policy
        policy = _load_policy(req.category, db)
        policy_data = _policy_to_data(policy)

        # 2. Normalize condition grade
        grade = CONDITION_GRADE_MAP.get(req.condition_grade.lower().strip(), "C")

        # 3. Gather comparables
        comp_result = get_comparables(
            category=req.category,
            brand=req.brand,
            model=req.model,
            db_session=db,
        )

        # 3b. Run vision analysis if photos were submitted
        vision_result = None
        if req.image_data:
            try:
                import asyncio
                from app.config import get_settings
                settings = get_settings()
                vision_result = asyncio.get_event_loop().run_until_complete(
                    analyze_images(
                        images_base64=req.image_data[:8],
                        category=req.category,
                        brand_hint=req.brand,
                        model_hint=req.model,
                        api_key=settings.anthropic_api_key,
                    )
                )
                logger.info(f"Vision analysis complete: grade={vision_result.get('condition_grade')}")
            except Exception as ve:
                logger.warning(f"Vision analysis skipped: {ve}")

        # 4. Score images
        iq_result = score_images(image_urls=req.image_urls)

        # 5. Assess risk
        risk_result = assess_risk(
            category=req.category,
            image_urls=req.image_urls,
        )

        # 5b. Merge vision results into scoring if available
        if vision_result:
            # Use vision condition score if higher confidence
            vision_cond = vision_result.get("condition_score")
            if vision_cond is not None:
                # Blend: 60% vision, 40% seller-reported
                req.condition_score = vision_cond * 0.6 + req.condition_score * 0.4
            # Add vision-detected defects
            for defect in vision_result.get("defects", []):
                defects_dicts = [d.model_dump() for d in req.defects]
                defects_dicts.append(defect)
            # Use vision photo quality if available
            viq = vision_result.get("photo_quality_score")
            if viq is not None:
                iq_result.score = viq

        # 6. Run deterministic offer engine
        defects_dicts = [d.model_dump() for d in req.defects]
        result = compute_valuation(
            category=req.category,
            brand=req.brand,
            model=req.model,
            age_years=req.age_years,
            condition_grade=grade,
            condition_score=req.condition_score,
            defects=defects_dicts,
            seller_asking_price=req.seller_asking_price,
            image_quality_score=iq_result.score,
            risk_score=risk_result.score,
            comparables=comp_result.comparables,
            pricing_policy=policy_data,
            retail_price=req.retail_price,
        )

        # 7. Persist valuation session
        vs = ValuationSession(
            trade_in_session_id=req.trade_in_session_id,
            category=req.category,
            brand=req.brand,
            model=req.model,
            age_years=req.age_years,
            condition_grade=grade,
            condition_score=req.condition_score,
            defects=defects_dicts,
            seller_asking_price=req.seller_asking_price,
            image_quality_score=iq_result.score,
            risk_score=risk_result.score,
            retail_price=req.retail_price,
            estimated_resale_value=result.estimated_resale_value,
            confidence_score=result.confidence_score,
            acquisition_ceiling=result.acquisition_ceiling,
            opening_offer=result.opening_offer,
            walkaway_limit=result.walkaway_limit,
            decision=result.decision,
            decision_reason=result.decision_reason,
            pricing_policy_snapshot=_policy_snapshot(policy),
            comparable_data={
                "count": comp_result.count,
                "weighted_average": comp_result.weighted_average,
                "confidence": comp_result.confidence,
            },
        )
        db.add(vs)
        db.flush()  # Populate vs.id before creating ledger entries

        # 8. Decision ledger entry
        ledger = DecisionLedger(
            valuation_session_id=vs.id,
            event_type="evaluation_created",
            actor="system",
            data={
                "decision": result.decision,
                "opening_offer": result.opening_offer,
                "ceiling": result.acquisition_ceiling,
                "resale_value": result.estimated_resale_value,
            },
        )
        db.add(ledger)

        # 9. If decision is negotiate or accept, also log offer_made
        if result.decision in ("negotiate", "accept"):
            offer_ledger = DecisionLedger(
                valuation_session_id=vs.id,
                event_type="offer_made",
                actor="system",
                data={
                    "offer_amount": result.opening_offer,
                    "ceiling": result.acquisition_ceiling,
                },
            )
            db.add(offer_ledger)

            # Also persist round 1 (system opening offer)
            round1 = NegotiationRound(
                valuation_session_id=vs.id,
                round_number=1,
                actor="system",
                offer_amount=result.opening_offer,
                ceiling_at_time=result.acquisition_ceiling,
                decision=result.decision,
                reason=result.decision_reason,
            )
            db.add(round1)

        db.commit()
        db.refresh(vs)

        logger.info(
            f"Valuation created: session={vs.id}, decision={result.decision}, "
            f"offer={result.opening_offer}"
        )

        return EvaluateResponse(
            session_id=vs.id,
            estimated_resale_value=result.estimated_resale_value,
            confidence_score=result.confidence_score,
            acquisition_ceiling=result.acquisition_ceiling,
            opening_offer=result.opening_offer,
            walkaway_limit=result.walkaway_limit,
            decision=result.decision,
            decision_reason=result.decision_reason,
            condition_grade=grade,
            risk_score=risk_result.score,
            comparable_count=comp_result.count,
            pricing_policy_version=(
                str(policy.updated_at) if policy.updated_at else str(policy.created_at)
            ),
        )

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        import traceback
        tb = traceback.format_exc()
        logger.opt(raw=True).error(f"Evaluation failed: {tb}\n")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# POST /{session_id}/counter
# ---------------------------------------------------------------------------
@evaluator_router.post("/{session_id}/counter", response_model=CounterResponse)
def counter_offer(session_id: str, req: CounterRequest, db: Session = Depends(get_db)):
    """Process a seller counter-offer against an existing valuation session."""
    try:
        # 1. Load valuation session
        vs = db.query(ValuationSession).filter(ValuationSession.id == session_id).first()
        if not vs:
            raise HTTPException(status_code=404, detail=f"Valuation session '{session_id}' not found.")

        # 2. Check session is not already finalized
        if vs.decision in ("accept", "decline"):
            raise HTTPException(
                status_code=409,
                detail=f"Session already finalized with decision '{vs.decision}'.",
            )

        # 3. Load pricing policy for negotiation params
        policy = _load_policy(vs.category, db)
        policy_data = _policy_to_data(policy)

        # 4. Determine current negotiation state
        rounds = (
            db.query(NegotiationRound)
            .filter(NegotiationRound.valuation_session_id == session_id)
            .order_by(NegotiationRound.round_number.desc())
            .all()
        )

        last_system_offer = vs.opening_offer
        current_round = 0
        for r in rounds:
            if r.actor == "system":
                last_system_offer = r.offer_amount
                break
        current_round = len(rounds)

        state = NegotiationState(
            current_round=current_round,
            previous_system_offer=last_system_offer,
            acquisition_ceiling=vs.acquisition_ceiling,
            walkaway_limit=vs.walkaway_limit,
        )

        # 5. Record seller's counter as a round
        seller_round = NegotiationRound(
            valuation_session_id=session_id,
            round_number=current_round + 1,
            actor="seller",
            offer_amount=req.seller_counter,
            ceiling_at_time=vs.acquisition_ceiling,
            decision="counter",
            reason=f"Seller counter-offer of KES {req.seller_counter:,.0f}",
        )
        db.add(seller_round)

        # 6. Decision ledger: counter_received
        db.add(DecisionLedger(
            valuation_session_id=session_id,
            event_type="counter_received",
            actor="seller",
            data={"seller_counter": req.seller_counter, "round": current_round + 1},
        ))

        # 7. Run negotiation engine
        neg_result = process_counter(
            seller_counter=req.seller_counter,
            state=state,
            max_negotiation_rounds=policy_data.max_negotiation_rounds,
            round_step_pct=policy_data.round_step_pct,
        )

        # 8. Record system's response as a round
        system_round = NegotiationRound(
            valuation_session_id=session_id,
            round_number=neg_result.round_number + 1,  # after seller's
            actor="system",
            offer_amount=neg_result.system_offer,
            ceiling_at_time=vs.acquisition_ceiling,
            decision=neg_result.decision,
            reason=neg_result.reason,
        )
        db.add(system_round)

        # 9. Decision ledger: system response
        event_type = {
            "accept": "accepted",
            "counter": "counter_offered",
            "decline": "declined",
        }.get(neg_result.decision, neg_result.decision)

        db.add(DecisionLedger(
            valuation_session_id=session_id,
            event_type=event_type,
            actor="system",
            data={
                "system_offer": neg_result.system_offer,
                "seller_counter": req.seller_counter,
                "round": neg_result.round_number,
                "decision": neg_result.decision,
            },
        ))

        # 10. Update session decision if finalized
        if neg_result.decision in ("accept", "decline"):
            vs.decision = neg_result.decision
            vs.decision_reason = neg_result.reason

        db.commit()

        logger.info(
            f"Counter processed: session={session_id}, "
            f"seller={req.seller_counter}, decision={neg_result.decision}, "
            f"system_offer={neg_result.system_offer}"
        )

        return CounterResponse(
            round_number=neg_result.round_number,
            decision=neg_result.decision,
            system_offer=neg_result.system_offer,
            ceiling=neg_result.ceiling,
            reason=neg_result.reason,
            rounds_remaining=neg_result.rounds_remaining,
        )

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Counter processing failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Counter processing failed: {e}")


# ---------------------------------------------------------------------------
# GET /{session_id}
# ---------------------------------------------------------------------------
@evaluator_router.get("/{session_id}", response_model=SessionDetailResponse)
def get_session_detail(session_id: str, db: Session = Depends(get_db)):
    """Retrieve full valuation session with negotiation rounds and ledger."""
    vs = db.query(ValuationSession).filter(ValuationSession.id == session_id).first()
    if not vs:
        raise HTTPException(status_code=404, detail=f"Valuation session '{session_id}' not found.")

    rounds = (
        db.query(NegotiationRound)
        .filter(NegotiationRound.valuation_session_id == session_id)
        .order_by(NegotiationRound.round_number)
        .all()
    )

    ledger = (
        db.query(DecisionLedger)
        .filter(DecisionLedger.valuation_session_id == session_id)
        .order_by(DecisionLedger.created_at)
        .all()
    )

    return SessionDetailResponse(
        session_id=vs.id,
        trade_in_session_id=vs.trade_in_session_id,
        category=vs.category,
        brand=vs.brand,
        model=vs.model,
        age_years=vs.age_years,
        condition_grade=vs.condition_grade,
        condition_score=vs.condition_score,
        defects=vs.defects,
        seller_asking_price=vs.seller_asking_price,
        image_quality_score=vs.image_quality_score,
        risk_score=vs.risk_score,
        retail_price=vs.retail_price,
        estimated_resale_value=vs.estimated_resale_value,
        confidence_score=vs.confidence_score,
        acquisition_ceiling=vs.acquisition_ceiling,
        opening_offer=vs.opening_offer,
        walkaway_limit=vs.walkaway_limit,
        decision=vs.decision,
        decision_reason=vs.decision_reason,
        pricing_policy_snapshot=vs.pricing_policy_snapshot,
        comparable_data=vs.comparable_data,
        created_at=str(vs.created_at) if vs.created_at else None,
        negotiation_rounds=[
            NegotiationRoundOut(
                round_number=r.round_number,
                actor=r.actor,
                offer_amount=r.offer_amount,
                ceiling_at_time=r.ceiling_at_time,
                decision=r.decision,
                reason=r.reason,
                created_at=str(r.created_at) if r.created_at else "",
            )
            for r in rounds
        ],
        decision_ledger=[
            DecisionLedgerOut(
                event_type=entry.event_type,
                actor=entry.actor,
                data=entry.data,
                created_at=str(entry.created_at) if entry.created_at else "",
            )
            for entry in ledger
        ],
    )


# ---------------------------------------------------------------------------
# POST /model-lookup
# ---------------------------------------------------------------------------
from pydantic import BaseModel as PydanticBaseModel


class ModelLookupRequest(PydanticBaseModel):
    image_data: str  # base64 data URL of model label photo
    typed_model: str = ""
    category: str = ""
    brand: str = ""


class ModelLookupResponse(PydanticBaseModel):
    model_verified: bool = False
    model_number: str | None = None
    specs_summary: str | None = None
    retail_price: float | None = None
    release_year: int | None = None


@evaluator_router.post("/model-lookup", response_model=ModelLookupResponse)
def model_lookup(req: ModelLookupRequest):
    """
    Accept a photo of a model label, OCR the model number,
    and look up specs, retail price, and release date.
    """
    try:
        from greenbay_ai_evaluator.services.vision_service import analyze_model_label

        result = analyze_model_label(
            image_data=req.image_data,
            typed_model=req.typed_model,
            category=req.category,
            brand=req.brand,
        )

        logger.info(
            f"Model lookup: typed={req.typed_model}, "
            f"verified={result.get('model_verified')}, "
            f"found={result.get('model_number')}"
        )

        return ModelLookupResponse(
            model_verified=result.get("model_verified", False),
            model_number=result.get("model_number"),
            specs_summary=result.get("specs_summary"),
            retail_price=result.get("retail_price"),
            release_year=result.get("release_year"),
        )

    except Exception as e:
        logger.warning(f"Model lookup failed (non-critical): {e}")
        # Non-critical endpoint: return unverified response
        return ModelLookupResponse(
            model_verified=False,
            model_number=req.typed_model or None,
            specs_summary=None,
        )

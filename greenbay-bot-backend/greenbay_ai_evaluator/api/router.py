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
    ExpertFeedbackRequest,
    ExpertFeedbackResponse,
    InventoryStatsOut,
    NegotiationRoundOut,
    PickupNotifyRequest,
    PickupNotifyResponse,
    RelatedProductOut,
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
    reconcile_retail_price,
)
from greenbay_ai_evaluator.models.evaluator_models import (
    DecisionLedger,
    NegotiationRound,
    PricingPolicy,
    ValuationSession,
)
from greenbay_ai_evaluator.services.comparables_service import get_comparables
from greenbay_ai_evaluator.services.image_quality_service import score_images
from greenbay_ai_evaluator.services.market_price_service import search_internet_price
from greenbay_ai_evaluator.services.marketplace_scraper import get_marketplace_prices
from greenbay_ai_evaluator.services.risk_service import assess_risk
from greenbay_ai_evaluator.services.vision_service import analyze_images
from greenbay_ai_evaluator.services.google_lens_service import identify_product_multi

from app.database.models import ExpertPriceFeedback, PickupRequest, ShopifyProduct

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
                import concurrent.futures
                from app.config import get_settings
                settings = get_settings()

                def _run_vision():
                    loop = asyncio.new_event_loop()
                    try:
                        return loop.run_until_complete(
                            analyze_images(
                                images_base64=req.image_data[:8],
                                category=req.category,
                                brand_hint=req.brand,
                                model_hint=req.model,
                                api_key=settings.anthropic_api_key,
                            )
                        )
                    finally:
                        loop.close()

                with concurrent.futures.ThreadPoolExecutor() as pool:
                    vision_result = pool.submit(_run_vision).result(timeout=120)
                logger.info(f"Vision analysis complete: grade={vision_result.get('condition_grade')}")
            except Exception as ve:
                logger.warning(f"Vision analysis skipped: {ve}")

        # 3c. Google Cloud Vision product identification (Google Lens equivalent)
        lens_result = None
        if req.image_data:
            try:
                from app.config import get_settings
                settings_for_lens = get_settings()
                lens_result = identify_product_multi(
                    images_b64=req.image_data[:3],  # Use first 3 images
                    category_hint=req.category,
                    brand_hint=req.brand,
                    model_hint=req.model,
                    google_cloud_api_key=settings_for_lens.google_cloud_api_key,
                )
                if lens_result and lens_result.confidence > 20:
                    logger.info(
                        f"Google Lens identified: {lens_result.product_name}, "
                        f"brand={lens_result.brand}, model={lens_result.model}, "
                        f"price={lens_result.estimated_retail_price_kes}"
                    )
                    # Enrich request data with Google Lens findings
                    if lens_result.brand and not req.brand:
                        req.brand = lens_result.brand
                    if lens_result.model and not req.model:
                        req.model = lens_result.model
                    # Use Google Lens retail price as a reference if we don't have one
                    if lens_result.estimated_retail_price_kes and (
                        not req.retail_price or req.retail_price <= 0
                    ):
                        req.retail_price = lens_result.estimated_retail_price_kes
                        req.retail_price_source = "google_lens"
                        logger.info(f"Using Google Lens retail price: {req.retail_price}")
            except Exception as le:
                logger.warning(f"Google Lens identification failed: {le}")

        # 4. Score images (OpenCV if base64 data available)
        iq_result = score_images(
            image_urls=req.image_urls,
            image_data=req.image_data,
        )

        # 5. Assess risk (rule-based fraud detection)
        risk_result = assess_risk(
            category=req.category,
            image_urls=req.image_urls,
            image_data=req.image_data,
            seller_asking_price=req.seller_asking_price,
            retail_price=req.retail_price,
            condition_grade=req.condition_grade,
            age_years=req.age_years,
            db_session=db,
        )

        # 5b. Build defects list from request
        defects_dicts = [d.model_dump() for d in req.defects]

        # 5c. Merge vision results into scoring if available
        if vision_result:
            # Use vision condition score if higher confidence
            vision_cond = vision_result.get("condition_score")
            if vision_cond is not None:
                # Blend: 60% vision, 40% seller-reported
                req.condition_score = vision_cond * 0.6 + req.condition_score * 0.4
            # Add vision-detected defects to the list
            for defect in vision_result.get("defects", []):
                defects_dicts.append(defect)
            # Use vision photo quality if available
            viq = vision_result.get("photo_quality_score")
            if viq is not None:
                iq_result.score = viq

        # 6a. Quality rejection pre-screening
        HARD_REJECT_KEYWORDS = {
            "rust", "rusted", "corrosion", "heavy_dent", "major_crack",
            "missing_part", "broken", "shattered", "severe_damage",
        }
        defect_types = {d.get("type", "").lower() for d in defects_dicts}
        defect_descs = " ".join(d.get("description", "").lower() for d in defects_dicts)

        is_hard_reject = bool(defect_types & HARD_REJECT_KEYWORDS) or any(
            kw in defect_descs for kw in ["rust", "rusted", "heavy dent", "major crack"]
        )
        grade_lower = req.condition_grade.lower().strip()
        if grade_lower in ("d", "poor", "not working"):
            is_hard_reject = True

        is_review_flag = req.age_years > 5 or grade_lower in ("partially working",)

        # 6a. Multi-source price verification (5-point system)
        price_verification = None
        internet_price = None
        marketplace_avg = None
        shopify_avg = None
        expert_avg = None
        internet_result = None
        mkt_result = None

        # --- Source A: Internet price lookup (Tavily) ---
        try:
            internet_result = search_internet_price(
                brand=req.brand, model=req.model,
                category=req.category, condition=req.condition_grade,
            )
            internet_price = internet_result.launch_price
            logger.info(f"Internet price: {internet_price} (confidence: {internet_result.confidence})")
        except Exception as e:
            logger.warning(f"Internet price lookup failed: {e}")

        # --- Source B: Live marketplace scraping (Jiji/Jumia) ---
        try:
            import asyncio
            import concurrent.futures

            def _run_marketplace():
                loop = asyncio.new_event_loop()
                try:
                    return loop.run_until_complete(
                        get_marketplace_prices(
                            brand=req.brand, model=req.model,
                            category=req.category,
                        )
                    )
                finally:
                    loop.close()

            with concurrent.futures.ThreadPoolExecutor() as pool:
                mkt_result = pool.submit(_run_marketplace).result(timeout=15)
            marketplace_avg = mkt_result.avg_price if mkt_result.count > 0 else None
            logger.info(f"Marketplace: avg={marketplace_avg}, count={mkt_result.count}")
        except Exception as e:
            logger.warning(f"Marketplace scraping failed: {e}")

        # --- Source C: Shopify inventory average ---
        try:
            if comp_result.sources_breakdown and comp_result.sources_breakdown.get("shopify", 0) > 0:
                shopify_avg = comp_result.weighted_average
            logger.info(f"Shopify inventory: avg={shopify_avg}")
        except Exception as e:
            logger.warning(f"Shopify lookup failed: {e}")

        # --- Source D: Expert feedback average ---
        try:
            if comp_result.sources_breakdown and comp_result.sources_breakdown.get("expert", 0) > 0:
                expert_comps = [c for c in comp_result.comparables if c.weight >= 2.5]
                if expert_comps:
                    expert_avg = sum(c.resale_price for c in expert_comps) / len(expert_comps)
            logger.info(f"Expert feedback: avg={expert_avg}")
        except Exception as e:
            logger.warning(f"Expert feedback lookup failed: {e}")

        # --- Reconcile all sources ---
        try:
            price_verification = reconcile_retail_price(
                frontend_price=req.retail_price,
                internet_price=internet_price,
                marketplace_avg=marketplace_avg,
                shopify_avg=shopify_avg,
                expert_avg=expert_avg,
            )

            # Add detailed breakdown if sources responded
            if internet_result:
                price_verification["internet_data"] = {
                    "launch_price": internet_result.launch_price,
                    "resale_range": [internet_result.current_resale_low, internet_result.current_resale_high],
                    "sources": internet_result.sources[:3],
                    "confidence": internet_result.confidence,
                }
            if mkt_result:
                price_verification["marketplace_data"] = {
                    "avg_price": mkt_result.avg_price,
                    "price_range": [mkt_result.min_price, mkt_result.max_price],
                    "listing_count": mkt_result.count,
                    "confidence": mkt_result.confidence,
                }
            if lens_result and lens_result.confidence > 0:
                price_verification["google_lens_data"] = {
                    "product_name": lens_result.product_name,
                    "brand": lens_result.brand,
                    "model": lens_result.model,
                    "category": lens_result.category,
                    "estimated_retail_price": lens_result.estimated_retail_price_kes,
                    "release_year": lens_result.release_year,
                    "labels": lens_result.labels[:5],
                    "web_entities": [
                        {"description": e["description"], "score": e.get("score", 0)}
                        for e in lens_result.web_entities[:5]
                    ],
                    "confidence": lens_result.confidence,
                }

            logger.info(
                f"Price verification: {price_verification['num_sources']} sources, "
                f"reconciled={price_verification['reconciled_price']}, "
                f"frontend={req.retail_price}"
            )
        except Exception as pve:
            logger.warning(f"Price reconciliation failed: {pve}")

        # 6b. Run deterministic offer engine with price verification
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
            price_verification=price_verification,
        )

        # Override decision based on rejection pre-screening
        if is_hard_reject:
            reject_reasons = []
            if defect_types & HARD_REJECT_KEYWORDS:
                reject_reasons.append(f"Detected issues: {', '.join(defect_types & HARD_REJECT_KEYWORDS)}")
            if grade_lower in ("d", "poor", "not working"):
                reject_reasons.append(f"Condition grade too low: {req.condition_grade}")
            result.decision = "reject"
            result.decision_reason = "Product does not meet minimum quality standards. " + "; ".join(reject_reasons)
            logger.info(f"Hard reject: {reject_reasons}")
        elif is_review_flag and result.decision not in ("reject",):
            review_reasons = []
            if req.age_years > 5:
                review_reasons.append(f"Product age ({req.age_years:.0f} years) exceeds 5-year threshold")
            if grade_lower in ("partially working",):
                review_reasons.append("Product is only partially working")
            result.decision = "review"
            result.decision_reason = "Manual review required. " + "; ".join(review_reasons)
            logger.info(f"Flagged for review: {review_reasons}")

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
            seller_name=req.seller_name,
            seller_phone=req.seller_phone,
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
            price_verification=price_verification,
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
# POST /notify-pickup  (MUST be above /{session_id} wildcard)
# ---------------------------------------------------------------------------
NEWTON_PHONE = "254715284353"  # Newton's WhatsApp number


@evaluator_router.post("/notify-pickup", response_model=PickupNotifyResponse)
def notify_pickup(req: PickupNotifyRequest, db: Session = Depends(get_db)):
    """Save a pickup request and return a WhatsApp deep-link to notify Newton."""
    try:
        pickup = PickupRequest(
            valuation_session_id=req.valuation_session_id,
            seller_name=req.seller_name,
            seller_phone=req.seller_phone,
            appliance_description=req.appliance_description,
            condition_grade=req.condition_grade,
            agreed_price=req.agreed_price,
            pickup_address=req.pickup_address,
            preferred_day=req.preferred_day,
            photo_count=req.photo_count,
            status="pending",
        )
        db.add(pickup)
        db.commit()
        db.refresh(pickup)

        # Build WhatsApp deep-link message
        price_str = f"KES {req.agreed_price:,.0f}" if req.agreed_price else "TBD"
        msg = (
            f"\U0001f7e2 *NEW PICKUP REQUEST #{pickup.id}*\n\n"
            f"\U0001f4e6 *Item:* {req.appliance_description}\n"
            f"\U0001f464 *Seller:* {req.seller_name}\n"
            f"\U0001f4de *Phone:* {req.seller_phone}\n"
            f"\U0001f4b0 *Agreed Price:* {price_str}\n"
            f"\U0001f4cd *Address:* {req.pickup_address}\n"
            f"\U0001f4c5 *Preferred Day:* {req.preferred_day or 'Any'}\n"
            f"\U0001f4f8 *Photos:* {req.photo_count}\n"
            f"\u2b50 *Condition:* {req.condition_grade or 'N/A'}"
        )
        import urllib.parse
        wa_link = f"https://wa.me/{NEWTON_PHONE}?text={urllib.parse.quote(msg)}"

        logger.info(f"Pickup request #{pickup.id} created for {req.seller_name}")

        return PickupNotifyResponse(
            id=pickup.id,
            status="pending",
            whatsapp_link=wa_link,
            message="Pickup request saved. Newton has been notified.",
        )
    except Exception as e:
        logger.error(f"Pickup notification failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# GET /pickup-requests  (MUST be above /{session_id} wildcard)
# ---------------------------------------------------------------------------
@evaluator_router.get("/pickup-requests")
def list_pickup_requests(status: str = "pending", db: Session = Depends(get_db)):
    """List pickup requests, filtered by status."""
    q = db.query(PickupRequest)
    if status != "all":
        q = q.filter(PickupRequest.status == status)
    pickups = q.order_by(PickupRequest.created_at.desc()).all()
    return [
        {
            "id": p.id,
            "seller_name": p.seller_name,
            "seller_phone": p.seller_phone,
            "appliance": p.appliance_description,
            "agreed_price": p.agreed_price,
            "pickup_address": p.pickup_address,
            "preferred_day": p.preferred_day,
            "status": p.status,
            "created_at": str(p.created_at) if p.created_at else None,
        }
        for p in pickups
    ]


# ---------------------------------------------------------------------------
# GET /related-products  (MUST be above /{session_id} wildcard)
# ---------------------------------------------------------------------------
_CATEGORY_MAP = {
    "refrigerator": ["REFRIGERATORS"],
    "fridge": ["REFRIGERATORS"],
    "washing_machine": ["Washing Machine"],
    "washer": ["Washing Machine"],
    "tv": ["TV & Home Entertainment"],
    "television": ["TV & Home Entertainment"],
    "cooker": ["COOKERS"],
    "stove": ["COOKERS"],
    "microwave": ["MICROWAVE"],
    "freezer": ["FREEZER"],
    "chiller": ["CHILLERS"],
    "laptop": ["Computers & Laptops"],
    "computer": ["Computers & Laptops"],
}


@evaluator_router.get("/related-products", response_model=list[RelatedProductOut])
def get_related_products(
    category: str = "",
    limit: int = 6,
    db: Session = Depends(get_db),
):
    """Return related products from our Shopify inventory."""
    results: list[RelatedProductOut] = []

    # Map evaluator category to Shopify product_type
    shopify_types = _CATEGORY_MAP.get(category.lower().strip(), [])

    # Same-category products first
    if shopify_types:
        same_cat = (
            db.query(ShopifyProduct)
            .filter(
                ShopifyProduct.is_active.is_(True),
                ShopifyProduct.available.is_(True),
                ShopifyProduct.product_type.in_(shopify_types),
            )
            .order_by(ShopifyProduct.price.asc())
            .limit(limit)
            .all()
        )
        for p in same_cat:
            results.append(RelatedProductOut(
                title=p.title,
                price=p.price,
                compare_at_price=p.compare_at_price,
                image_url=p.image_url,
                product_url=p.product_url,
                product_type=p.product_type,
                available=p.available,
            ))

    # Fill remaining with popular items from other categories
    remaining = limit - len(results)
    if remaining > 0:
        existing_ids = {r.title for r in results}
        other = (
            db.query(ShopifyProduct)
            .filter(
                ShopifyProduct.is_active.is_(True),
                ShopifyProduct.available.is_(True),
            )
            .order_by(ShopifyProduct.price.asc())
            .limit(remaining + len(results))  # fetch extra to skip dupes
            .all()
        )
        for p in other:
            if p.title not in existing_ids and len(results) < limit:
                results.append(RelatedProductOut(
                    title=p.title,
                    price=p.price,
                    compare_at_price=p.compare_at_price,
                    image_url=p.image_url,
                    product_url=p.product_url,
                    product_type=p.product_type,
                    available=p.available,
                ))

    return results


# ---------------------------------------------------------------------------
# POST /expert-feedback  (MUST be above /{session_id} wildcard)
# ---------------------------------------------------------------------------
@evaluator_router.post("/expert-feedback", response_model=ExpertFeedbackResponse)
def submit_expert_feedback(req: ExpertFeedbackRequest, db: Session = Depends(get_db)):
    """Submit expert pricing feedback for a valuation session.

    Used by experienced sales agents during pilot phase to teach the AI.
    """
    try:
        # Look up the valuation session to get product details
        vs = db.query(ValuationSession).filter(
            ValuationSession.id == int(req.valuation_session_id)
        ).first()

        system_price = None
        category = None
        brand = None
        model_name = None
        condition = None
        age = None
        images = None

        if vs:
            system_price = vs.opening_offer
            category = vs.category
            brand = vs.brand
            model_name = vs.model
            condition = vs.condition_grade
            age = vs.age_years
            images = vs.defects  # JSON field that may contain image refs

        price_diff = (req.expert_price - system_price) if system_price else None

        feedback = ExpertPriceFeedback(
            valuation_session_id=req.valuation_session_id,
            expert_name=req.expert_name,
            expert_price=req.expert_price,
            expert_reasoning=req.expert_reasoning,
            product_category=category,
            brand=brand,
            model=model_name,
            condition_grade=condition,
            age_years=age,
            system_price=system_price,
            price_difference=price_diff,
            images_json=images,
            specs_json={
                "category": category,
                "brand": brand,
                "model": model_name,
                "condition": condition,
                "age_years": age,
            },
        )
        db.add(feedback)
        db.commit()
        db.refresh(feedback)

        logger.info(
            f"Expert feedback: session={req.valuation_session_id}, "
            f"expert={req.expert_name}, price={req.expert_price}, "
            f"system={system_price}, diff={price_diff}"
        )

        return ExpertFeedbackResponse(
            id=feedback.id,
            valuation_session_id=req.valuation_session_id,
            expert_name=req.expert_name,
            expert_price=req.expert_price,
            system_price=system_price,
            price_difference=price_diff,
            message=f"Thank you! Your pricing expertise has been recorded and will help improve future valuations.",
        )

    except Exception as e:
        db.rollback()
        logger.error(f"Expert feedback failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# GET /expert-feedback/{session_id}  (MUST be above /{session_id} wildcard)
# ---------------------------------------------------------------------------
@evaluator_router.get("/expert-feedback/{session_id}")
def get_expert_feedback(session_id: str, db: Session = Depends(get_db)):
    """Retrieve expert feedback for a specific valuation session."""
    feedbacks = db.query(ExpertPriceFeedback).filter(
        ExpertPriceFeedback.valuation_session_id == session_id
    ).all()

    return [
        {
            "id": f.id,
            "expert_name": f.expert_name,
            "expert_price": f.expert_price,
            "expert_reasoning": f.expert_reasoning,
            "system_price": f.system_price,
            "price_difference": f.price_difference,
            "created_at": str(f.created_at),
        }
        for f in feedbacks
    ]


# ---------------------------------------------------------------------------
# GET /inventory-stats  (MUST be above /{session_id} wildcard)
# ---------------------------------------------------------------------------
@evaluator_router.get("/inventory-stats", response_model=InventoryStatsOut)
def get_inventory_stats(db: Session = Depends(get_db)):
    """Return summary of Shopify inventory in our DB."""
    from sqlalchemy import func as sqla_func

    total = db.query(ShopifyProduct).filter(ShopifyProduct.is_active.is_(True)).count()

    # Count by category
    cats = (
        db.query(ShopifyProduct.product_type, sqla_func.count())
        .filter(ShopifyProduct.is_active.is_(True))
        .group_by(ShopifyProduct.product_type)
        .all()
    )
    by_category = {cat or "Unknown": cnt for cat, cnt in cats}

    # Last scrape time
    latest = (
        db.query(sqla_func.max(ShopifyProduct.last_seen_at))
        .filter(ShopifyProduct.is_active.is_(True))
        .scalar()
    )

    return InventoryStatsOut(
        total_active=total,
        by_category=by_category,
        last_scrape=str(latest) if latest else None,
    )


# ---------------------------------------------------------------------------
# GET /{session_id}  — WILDCARD: must be LAST
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


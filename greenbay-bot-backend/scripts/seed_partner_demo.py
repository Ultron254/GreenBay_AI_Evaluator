"""Demo data for the partner trade-in pages, ops page and Pulse screens.

Fills each configured partner with sample leads, credits in every state,
QR scans and starts, one lead waiting for a price, and a few redemptions,
so the ops page, the settlement report and the Pulse "Trade-ins" screen have
something real-looking to show.

Every row is marked as test data: the model starts with ZZTEST-DEMO, which
the purge routine (POST /tradein/admin/purge-test-records) and Pulse already
skip. Customer names are invented.

Refuses to run against anything but a local SQLite database unless you pass
--allow-non-sqlite (do not do that on production).

    python scripts/seed_partner_demo.py                # uses DATABASE_URL
    python scripts/seed_partner_demo.py --reset        # wipe demo rows first
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEMO = "ZZTEST-DEMO"
NAMES = ["Wanjiku Kamau", "Otieno Ouma", "Achieng Atieno", "Mwangi Njoroge", "Chebet Kiprono",
         "Nafula Wekesa", "Kamau Mutua", "Akinyi Odhiambo", "Wairimu Gitau", "Barasa Wanyama"]
ITEMS = {
    "mika": [("refrigerator", "MRNF202XDMV", 3), ("cooker_oven", "MST50PI31GOKG", 5), ("microwave", "MMWMSKH2011W", 2),
             ("washing_machine", "MWAFT2207", 6), ("refrigerator", "MRDCD138XDM", 13), ("small_kitchen", "MKT1802", 4)],
    "vision-plus": [("tv_monitor", "VP8843SW", 3), ("tv_monitor", "VP8855UHD", 4), ("refrigerator", "VPR220", 5),
                    ("cooker_oven", "VPSC3150B", 2), ("washing_machine", "VPW7KG", 7), ("microwave", "VPMW20", 12)],
}


def _db():
    from app.database.db import Base, SessionLocal, engine
    import app.database.models  # noqa: F401
    import greenbay_ai_evaluator.models.evaluator_models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    return SessionLocal(), engine


def reset(db) -> int:
    from greenbay_ai_evaluator.models.evaluator_models import ValuationSession
    from greenbay_ai_evaluator.models.partner_models import (
        PartnerCredit, PartnerCreditEvent, PartnerFunnelEvent,
    )

    ids = [r.id for r in db.query(ValuationSession.id).filter(ValuationSession.model.like(f"{DEMO}%"))]
    credit_ids = [c.id for c in db.query(PartnerCredit.id).filter(PartnerCredit.valuation_session_id.in_(ids))] if ids else []
    if credit_ids:
        db.query(PartnerCreditEvent).filter(PartnerCreditEvent.credit_id.in_(credit_ids)).delete(synchronize_session=False)
        db.query(PartnerCredit).filter(PartnerCredit.id.in_(credit_ids)).delete(synchronize_session=False)
    # Demo scans and starts carry no session and cannot be told apart from real
    # ones, so they are left alone; on a local demo database that is harmless.
    if ids:
        db.query(PartnerFunnelEvent).filter(PartnerFunnelEvent.valuation_session_id.in_(ids)).delete(synchronize_session=False)
        db.query(ValuationSession).filter(ValuationSession.id.in_(ids)).delete(synchronize_session=False)
    db.commit()
    return len(ids)


def seed(db, *, seed_value: int = 7) -> dict[str, int]:
    from greenbay_ai_evaluator.models.evaluator_models import ValuationSession
    from greenbay_ai_evaluator.partners import ledger, rules
    from greenbay_ai_evaluator.partners.config import all_partners

    rnd = random.Random(seed_value)
    now = datetime.now(timezone.utc)
    made: dict[str, int] = {}
    for cfg in all_partners():
        codes = [p for p in cfg.placements]
        items = ITEMS.get(cfg.slug) or [(cfg.categories[0], "MODEL", 3)]
        # Scans and starts (the top of the funnel).
        for _ in range(rnd.randint(70, 110)):
            p = rnd.choice(codes) if codes else None
            ledger.record_funnel(db, cfg.slug, "scan", qr_code=p.code if p else None,
                                 channel=p.channel if p else "direct", shop=p.shop if p else None)
        for _ in range(rnd.randint(35, 55)):
            p = rnd.choice(codes) if codes else None
            ledger.record_funnel(db, cfg.slug, "start", qr_code=p.code if p else None,
                                 channel=p.channel if p else "direct", shop=p.shop if p else None)
        # Finished evaluations with credits in every state, plus one for review.
        plan = ["offered", "accepted", "accepted", "pending_validation", "validated", "issued", "issued",
                "partially_redeemed", "redeemed", "cancelled", "review"]
        for i, state in enumerate(plan):
            cat, model, age = items[i % len(items)]
            p = codes[i % len(codes)] if codes else None
            buy = cfg.buy_categories[i % len(cfg.buy_categories)].id.replace("_", "-") if cfg.buy_categories else None
            offer = float(rnd.choice([500, 2400, 3800, 5200, 6500, 8200, 11400, 14800]))
            vs = ValuationSession(
                id=str(uuid.uuid4()), category=cat, brand=cfg.brands[0], model=f"{DEMO}-{model}", age_years=age,
                condition_grade=rnd.choice("ABBC"), condition_score=70, seller_name=NAMES[(i + len(cfg.slug)) % len(NAMES)],
                seller_phone=f"2547{rnd.randint(10000000, 99999999)}", retail_price=offer * 3,
                confidence_score=86 if state != "review" else 58, risk_score=12, opening_offer=offer,
                acquisition_ceiling=offer * 1.1, decision="negotiate" if state != "review" else "review",
                country="KE", currency_code=cfg.currency, partner_id=cfg.slug,
                partner_channel=p.channel if p else "direct", partner_shop=p.shop if p else None,
                partner_placement=p.placement if p else None, partner_qr_code=p.code if p else None,
                partner_source=rules.source_tag(cfg.slug, p.channel if p else "direct", p.shop if p else None,
                                                p.placement if p else None),
                buy_intent_category=buy, buy_intent_model=rnd.choice([None, None, "budget 40k", "43 inch"]),
                created_at=now - timedelta(days=rnd.randint(0, 25), hours=rnd.randint(0, 20)),
            )
            credit_offer = rules.credit_offer(cfg, decision=vs.decision, opening_offer=offer, age_years=age,
                                              risk_score=vs.risk_score)
            vs.partner_offer = credit_offer
            db.add(vs)
            db.flush()
            ledger.record_funnel(db, cfg.slug, "complete", qr_code=vs.partner_qr_code, channel=vs.partner_channel,
                                 shop=vs.partner_shop, valuation_session_id=vs.id)
            if state == "review" or credit_offer is None:
                continue
            c = ledger.create_offered(db, cfg, vs, amount=credit_offer)
            ledger.record_funnel(db, cfg.slug, "offer", qr_code=vs.partner_qr_code, channel=vs.partner_channel,
                                 shop=vs.partner_shop, valuation_session_id=vs.id, credit_id=c.id, amount=credit_offer)
            if state == "offered":
                continue
            ledger.accept(db, c, amount=credit_offer)
            vs.final_decision, vs.final_offer = "accepted", credit_offer
            if state == "accepted":
                continue
            if state == "cancelled":
                ledger.cancel(db, c, actor="Demo CX", note="Customer kept the item")
                continue
            ledger.start_validation(db, c, method="technician" if c.bulky else "drop_greenbay", actor="Demo Technician")
            if state == "pending_validation":
                continue
            ledger.validate(db, c, actor="Demo Technician")
            if state == "validated":
                continue
            ledger.issue(db, c, actor="Demo CX")
            if state == "partially_redeemed":
                ledger.redeem(db, partner_id=cfg.slug, voucher_code=c.voucher_code, amount=round(c.balance / 2),
                              actor="Demo CX", note=f"{cfg.name} receipt DEMO-{i}")
            elif state == "redeemed":
                ledger.redeem(db, partner_id=cfg.slug, voucher_code=c.voucher_code, amount=c.balance,
                              actor="Demo CX", note=f"{cfg.name} receipt DEMO-{i}")
        db.commit()
        made[cfg.slug] = len(plan)
    return made


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--reset", action="store_true", help="delete earlier demo rows first")
    ap.add_argument("--allow-non-sqlite", action="store_true", help="seed a non-SQLite database (never production)")
    args = ap.parse_args()
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("sqlite") and not args.allow_non_sqlite:
        print("Refusing: DATABASE_URL is not SQLite. This script is for local demos.", file=sys.stderr)
        return 2
    db, _ = _db()
    try:
        if args.reset:
            print(f"Removed {reset(db)} demo evaluations.")
        print("Seeded:", seed(db))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

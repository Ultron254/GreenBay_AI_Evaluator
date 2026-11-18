"""Remove partner ledger rows that belong to test evaluations.

Called by POST /tradein/admin/purge-test-records after every deploy. A test
evaluation is one whose model or seller name matches the probe pattern
(ZZSMOKE, ZZTEST, ZZDIAG, ...). Its partner credit, the credit's audit trail
and its funnel events are deleted; the evaluation row itself is left, as the
purge has always left valuation_sessions alone.
"""

from __future__ import annotations

from typing import Any


def purge_partner_test_rows(pattern, *, dry_run: bool = True, db=None) -> dict[str, Any]:
    from greenbay_ai_evaluator.models.evaluator_models import ValuationSession
    from greenbay_ai_evaluator.models.partner_models import (
        PartnerCredit, PartnerCreditEvent, PartnerFunnelEvent,
    )

    own = db is None
    if own:
        from app.database.db import SessionLocal

        db = SessionLocal()
    try:
        rows = db.query(ValuationSession.id, ValuationSession.model, ValuationSession.seller_name).filter(
            ValuationSession.partner_id.isnot(None)).all()
        ids = [r.id for r in rows if pattern.search(f"{r.model or ''} {r.seller_name or ''}")]
        credit_ids = [c.id for c in db.query(PartnerCredit.id).filter(
            PartnerCredit.valuation_session_id.in_(ids))] if ids else []
        out = {"evaluations": len(ids), "credits": len(credit_ids)}
        if not dry_run and ids:
            if credit_ids:
                db.query(PartnerCreditEvent).filter(PartnerCreditEvent.credit_id.in_(credit_ids)).delete(
                    synchronize_session=False)
                db.query(PartnerCredit).filter(PartnerCredit.id.in_(credit_ids)).delete(synchronize_session=False)
            db.query(PartnerFunnelEvent).filter(PartnerFunnelEvent.valuation_session_id.in_(ids)).delete(
                synchronize_session=False)
            db.commit()
        return out
    finally:
        if own:
            db.close()

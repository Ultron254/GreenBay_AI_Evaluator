"""Public partner routes: the partner pages, QR short links, page config and
the "visitor started" ping.

Mounted from app/main.py:

    GET  /mika, /vision-plus, ...         the wizard, branded by partner config
    GET  /q/{code}                        QR / short link: log the scan, redirect
    GET  /tradein/partners/{slug}/config  public config (no emails, no rules)
    POST /tradein/partners/{slug}/events  {"type": "start", "qr_code": ...}

Nothing here needs a key: everything is either public page data or a count.
Admin and partner-scoped data routes come with the ledger views (stage 4).
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from loguru import logger
from sqlalchemy.orm import Session

from app.database.db import get_db
from greenbay_ai_evaluator.partners import ledger, pages
from greenbay_ai_evaluator.partners.config import all_partners, get_partner, public_config
from greenbay_ai_evaluator.partners.service import lookup_code

partner_api_router = APIRouter()   # mounted under /tradein
partner_page_router = APIRouter()  # mounted at the site root

# Public pings the browser may send. Everything else (complete, offer,
# accept, validated, issued, redeemed) is written by the server itself.
PUBLIC_EVENTS = frozenset({"start"})

_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}


def render_partner_page(slug: str) -> str:
    """The partner's page: partner.html with the shared wizard dropped in."""
    if get_partner(slug) is None:
        raise HTTPException(404, "Not found")
    return pages.render(slug)


def register_partner_pages(app) -> None:
    """One GET route per configured partner path (/mika, /vision-plus)."""
    for cfg in all_partners():
        path = cfg.path.rstrip("/") or f"/{cfg.slug}"

        def _make(slug: str):
            async def _serve():
                return HTMLResponse(render_partner_page(slug), headers=_NO_CACHE)

            async def _redirect():
                return RedirectResponse(url=path_for(slug), status_code=308)

            return _serve, _redirect

        serve, redirect = _make(cfg.slug)
        app.add_api_route(path, serve, methods=["GET"], include_in_schema=False)
        app.add_api_route(path + "/", redirect, methods=["GET"], include_in_schema=False)
        logger.info(f"Partner page {cfg.name} at {path}")


def path_for(slug: str) -> str:
    cfg = get_partner(slug)
    return (cfg.path.rstrip("/") or f"/{cfg.slug}") if cfg else "/app/"


@partner_page_router.get("/q/{code}", include_in_schema=False)
def qr_redirect(code: str, db: Session = Depends(get_db)):
    """A QR scan or short link. Log the scan, then open the partner page with
    the code, so the evaluation that follows is labelled with it."""
    clean = re.sub(r"[^A-Z0-9-]+", "", (code or "").upper())[:24]
    hit = lookup_code(db, clean)
    if hit is None or get_partner(hit["partner_id"]) is None:
        # A dead or mistyped code still lands somewhere useful.
        return RedirectResponse(url="/app/", status_code=302)
    try:
        ledger.record_funnel(db, hit["partner_id"], "scan", qr_code=hit["code"],
                             channel=hit["channel"], shop=hit["shop"])
        db.commit()
    except Exception as e:  # noqa: BLE001, a count must never block the visitor
        db.rollback()
        logger.warning(f"QR scan log failed for {clean}: {e}")
    return RedirectResponse(url=f"{path_for(hit['partner_id'])}?qr={hit['code']}", status_code=302,
                            headers=_NO_CACHE)


@partner_api_router.get("/partners/{slug}/config")
def partner_config(slug: str):
    cfg = get_partner(slug)
    if cfg is None:
        raise HTTPException(404, "Unknown partner")
    return public_config(cfg)


@partner_api_router.post("/partners/{slug}/events", status_code=204)
def partner_event(slug: str, body: dict | None = Body(default=None), db: Session = Depends(get_db)):
    cfg = get_partner(slug)
    if cfg is None:
        raise HTTPException(404, "Unknown partner")
    body = body if isinstance(body, dict) else {}
    event_type = str(body.get("type") or "").strip().lower()
    if event_type not in PUBLIC_EVENTS:
        raise HTTPException(422, "Unknown event type")
    hit = lookup_code(db, str(body.get("qr_code") or "")[:24] or None)
    if hit and hit["partner_id"] != cfg.slug:
        hit = None
    try:
        ledger.record_funnel(
            db, cfg.slug, event_type,
            qr_code=hit["code"] if hit else None,
            channel=hit["channel"] if hit else "direct",
            shop=hit["shop"] if hit else None,
        )
        db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.warning(f"partner {cfg.slug}: {event_type} ping not recorded: {e}")
    return Response(status_code=204)

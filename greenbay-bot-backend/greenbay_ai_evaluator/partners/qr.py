"""QR codes, short links and print-ready sticker sheets for partner swaps.

A QR code is a short link, ``https://evaluate.greenbay.market/q/<CODE>``,
that logs the scan and opens the partner's page with the code attached (see
partners/api.py). This module makes:

* ``new_code``        the next free code for a partner, shop and placement,
                      e.g. MK-ROY1 (Mika, Roysambu) or VP-WEB3
* ``qr_svg``          one QR code as a plain SVG, for websites and designers
* ``sheet_svg``       an A4 sheet of four stickers as SVG
* ``sheet_pdf``       the same sheet as PDF, for the printer

Every sticker carries the partner's official logo and GreenBay's official
wordmark (white versions on the partner's band colour, embedded from the
files in frontend/partners/), the partner's sticker headline, the QR code,
the short link written out for people who cannot scan, "Valued and bought by
GreenBay", and the code and placement in small type so a sticker found in a
shop can be traced. Logos are never redrawn or typeset.

QR codes use error correction level Q (25 percent) so a scuffed counter
sticker still scans, and keep the standard four-module quiet zone.
"""

from __future__ import annotations

import base64
import io
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Iterable
from xml.sax.saxutils import escape

from greenbay_ai_evaluator.partners.config import PartnerConfig

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
GREENBAY_WORDMARK_WHITE = "/app/partners/greenbay/greenbay-wordmark-white.png"

CHANNELS = {"shop": "SHP", "website": "WEB", "social": "SOC", "print": "PRN", "staff": "STF", "event": "EVT"}
_SLUG = re.compile(r"[^a-z0-9]+")

# Sticker and sheet geometry, millimetres. Four 90 x 125 mm stickers on A4.
PAGE_W, PAGE_H = 210.0, 297.0
ST_W, ST_H = 90.0, 125.0
GAP_X = (PAGE_W - 2 * ST_W) / 3
GAP_Y = (PAGE_H - 2 * ST_H) / 3
BAND_H = 26.0
QR_MM = 54.0
QR_TOP = 52.0


def public_base() -> str:
    return (os.environ.get("PARTNER_PUBLIC_BASE") or "https://evaluate.greenbay.market").rstrip("/")


def short_url(code: str) -> str:
    return f"{public_base()}/q/{code}"


def short_url_display(code: str) -> str:
    return short_url(code).split("://", 1)[-1]


def slug(text: str | None, n: int = 40) -> str:
    return _SLUG.sub("-", (text or "").strip().lower()).strip("-")[:n]


def new_code(cfg: PartnerConfig, *, channel: str, shop: str | None, taken: Iterable[str]) -> str:
    """MK-ROY1 for a Roysambu shop sticker, MK-WEB3 for a third web link."""
    if shop:
        stem = re.sub(r"[^A-Z]", "", shop.upper())[:3] or "SHP"
    else:
        stem = CHANNELS.get(channel, "GEN")
    taken = {t.upper() for t in taken}
    n = 1
    while f"{cfg.ref_prefix}-{stem}{n}" in taken:
        n += 1
    return f"{cfg.ref_prefix}-{stem}{n}"


# ---------------------------------------------------------------------------
# QR matrix
# ---------------------------------------------------------------------------
def _matrix(data: str) -> list[list[bool]]:
    import segno

    q = segno.make(data, error="q", micro=False, boost_error=False)
    return [[bool(cell) for cell in row] for row in q.matrix_iter(border=4)]


def _qr_path(matrix: list[list[bool]], x0: float, y0: float, size: float) -> str:
    """One SVG path of unit squares, merged per row run, scaled to *size*."""
    n = len(matrix)
    m = size / n
    parts = []
    for r, row in enumerate(matrix):
        c = 0
        while c < n:
            if row[c]:
                start = c
                while c < n and row[c]:
                    c += 1
                parts.append(f"M{x0 + start * m:.3f} {y0 + r * m:.3f}h{(c - start) * m:.3f}v{m:.3f}h{-(c - start) * m:.3f}z")
            else:
                c += 1
    return "".join(parts)


def qr_svg(code: str, *, ink: str = "#111111", size_px: int = 600) -> str:
    matrix = _matrix(short_url(code))
    n = len(matrix)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" width="{size_px}" height="{size_px}" '
        f'shape-rendering="crispEdges"><title>{escape(short_url(code))}</title>'
        f'<rect width="{n}" height="{n}" fill="#FFFFFF"/>'
        f'<path fill="{_safe_colour(ink, "#111111")}" d="{_qr_path(matrix, 0, 0, n)}"/></svg>'
    )


# ---------------------------------------------------------------------------
# assets
# ---------------------------------------------------------------------------
_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _safe_colour(value: str | None, default: str) -> str:
    return value if value and _HEX.match(value) else default


def _asset_path(web_path: str) -> Path:
    if not web_path.startswith("/app/"):
        raise ValueError(f"asset outside /app/: {web_path}")
    p = (FRONTEND / web_path[len("/app/"):]).resolve()
    if FRONTEND.resolve() not in p.parents:
        raise ValueError(f"asset outside frontend/: {web_path}")
    return p


@lru_cache(maxsize=32)
def _png_info(web_path: str) -> tuple[str, float]:
    """(data URI, width / height) for a PNG logo file."""
    p = _asset_path(web_path)
    raw = p.read_bytes()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{web_path} is not a PNG")
    w = int.from_bytes(raw[16:20], "big")
    h = int.from_bytes(raw[20:24], "big")
    return "data:image/png;base64," + base64.b64encode(raw).decode(), w / h


def _print_logo(cfg: PartnerConfig) -> str:
    """The white partner logo as a PNG (print tools need raster here)."""
    t = cfg.theme or {}
    return t.get("logo_on_dark_png") or t.get("logo_on_dark") or t.get("logo")


def sticker_text(cfg: PartnerConfig) -> tuple[str, str]:
    c = cfg.copy_text or {}
    return (
        c.get("sticker_headline") or f"Swap your old {cfg.name}",
        c.get("sticker_sub") or f"Scan for a fair offer, paid as {cfg.name} credit.",
    )


def _colours(cfg: PartnerConfig) -> dict[str, str]:
    col = (cfg.theme or {}).get("colors") or {}
    ink = _safe_colour(col.get("ink"), "#111111")
    return {
        "band": _safe_colour(col.get("band"), ink),
        "ink": ink,
        "muted": _safe_colour(col.get("muted"), "#555555"),
        "line": _safe_colour(col.get("line"), "#D6D0C4"),
    }


def _wrap(text: str, max_chars: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + (1 if cur else 0) > max_chars and cur:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines[:2]


# ---------------------------------------------------------------------------
# SVG sheet
# ---------------------------------------------------------------------------
def _sticker_svg(cfg: PartnerConfig, code: dict, x: float, y: float) -> str:
    col = _colours(cfg)
    headline, sub = sticker_text(cfg)
    _, logo_ratio = _png_info(_print_logo(cfg))
    _, gb_ratio = _png_info(GREENBAY_WORDMARK_WHITE)
    logo_h = 10.0
    logo_w = min(logo_h * logo_ratio, 42.0)
    logo_h = logo_w / logo_ratio
    gb_h = 4.6
    gb_w = gb_h * gb_ratio
    gb_x = x + ST_W - 7 - gb_w
    qr_x = x + (ST_W - QR_MM) / 2
    matrix = _matrix(short_url(code["code"]))
    tag = " · ".join(p for p in (code["code"], code.get("label") or "") if p)
    sub_lines = _wrap(sub, 46)
    out = [
        f'<g>',
        f'<rect x="{x}" y="{y}" width="{ST_W}" height="{ST_H}" rx="4" fill="#FFFFFF" stroke="{col["line"]}" stroke-width="0.3"/>',
        f'<path d="M{x} {y + 4}a4 4 0 0 1 4 -4h{ST_W - 8}a4 4 0 0 1 4 4v{BAND_H - 4}h{-ST_W}z" fill="{col["band"]}"/>',
        f'<use href="#pLogo" x="{x + 7}" y="{y + (BAND_H - logo_h) / 2:.2f}"/>',
        f'<text x="{gb_x - 4}" y="{y + BAND_H / 2 + 1.4}" font-family="Helvetica, Arial, sans-serif" font-size="4" fill="#FFFFFF" text-anchor="end" opacity="0.8">×</text>',
        f'<use href="#gbMark" x="{gb_x:.2f}" y="{y + (BAND_H - gb_h) / 2:.2f}"/>',
        f'<text x="{x + ST_W / 2}" y="{y + 36}" font-family="Helvetica, Arial, sans-serif" font-weight="700" font-size="6.2" fill="{col["ink"]}" text-anchor="middle">{escape(headline)}</text>',
    ]
    for i, line in enumerate(sub_lines):
        out.append(f'<text x="{x + ST_W / 2}" y="{y + 42.5 + i * 4.2}" font-family="Helvetica, Arial, sans-serif" font-size="3.4" fill="{col["muted"]}" text-anchor="middle">{escape(line)}</text>')
    out += [
        f'<path fill="{col["ink"]}" shape-rendering="crispEdges" d="{_qr_path(matrix, qr_x, y + QR_TOP, QR_MM)}"/>',
        f'<text x="{x + ST_W / 2}" y="{y + QR_TOP + QR_MM + 6}" font-family="Helvetica, Arial, sans-serif" font-weight="700" font-size="3.6" fill="{col["ink"]}" text-anchor="middle">{escape(short_url_display(code["code"]))}</text>',
        f'<line x1="{x + 7}" y1="{y + ST_H - 10}" x2="{x + ST_W - 7}" y2="{y + ST_H - 10}" stroke="{col["line"]}" stroke-width="0.3"/>',
        f'<text x="{x + ST_W / 2}" y="{y + ST_H - 5.6}" font-family="Helvetica, Arial, sans-serif" font-size="2.9" fill="{col["muted"]}" text-anchor="middle">Valued and bought by GreenBay</text>',
        f'<text x="{x + ST_W / 2}" y="{y + ST_H - 2.4}" font-family="Helvetica, Arial, sans-serif" font-size="2.3" fill="{col["muted"]}" text-anchor="middle" opacity="0.85">{escape(tag[:60])}</text>',
        '</g>',
    ]
    return "".join(out)


def _slots(n: int) -> list[tuple[int, float, float]]:
    out = []
    for i in range(n):
        k = i % 4
        out.append((i // 4, GAP_X + (k % 2) * (ST_W + GAP_X), GAP_Y + (k // 2) * (ST_H + GAP_Y)))
    return out


def _defs(cfg: PartnerConfig) -> str:
    """Each logo file embedded once; stickers place it with <use>."""
    logo_uri, logo_ratio = _png_info(_print_logo(cfg))
    gb_uri, gb_ratio = _png_info(GREENBAY_WORDMARK_WHITE)
    logo_w = min(10.0 * logo_ratio, 42.0)
    gb_h = 4.6
    return (
        f'<defs><image id="pLogo" href="{logo_uri}" width="{logo_w:.2f}" height="{logo_w / logo_ratio:.2f}"/>'
        f'<image id="gbMark" href="{gb_uri}" width="{gb_h * gb_ratio:.2f}" height="{gb_h}"/></defs>'
    )


def sheet_svg(cfg: PartnerConfig, codes: list[dict]) -> str:
    """All stickers on A4 pages stacked vertically in one SVG (4 per page)."""
    pages = max(1, (len(codes) + 3) // 4)
    h = PAGE_H * pages
    body = [_defs(cfg)]
    for p in range(pages):
        body.append(f'<rect x="0" y="{p * PAGE_H}" width="{PAGE_W}" height="{PAGE_H}" fill="#FFFFFF"/>')
    for (page, x, y), code in zip(_slots(len(codes)), codes):
        body.append(_sticker_svg(cfg, code, x, y + page * PAGE_H))
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {PAGE_W} {h}" width="{PAGE_W}mm" height="{h}mm">'
        f'<title>{escape(cfg.name)} x GreenBay swap stickers</title>{"".join(body)}</svg>'
    )


# ---------------------------------------------------------------------------
# PDF sheet
# ---------------------------------------------------------------------------
def sheet_pdf(cfg: PartnerConfig, codes: list[dict]) -> bytes:
    from reportlab.lib.colors import HexColor, white
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    col = _colours(cfg)
    headline, sub = sticker_text(cfg)
    logo_path = _asset_path(_print_logo(cfg))
    _, logo_ratio = _png_info(_print_logo(cfg))
    gb_path = _asset_path(GREENBAY_WORDMARK_WHITE)
    _, gb_ratio = _png_info(GREENBAY_WORDMARK_WHITE)
    logo_img, gb_img = ImageReader(str(logo_path)), ImageReader(str(gb_path))

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(PAGE_W * mm, PAGE_H * mm))
    c.setTitle(f"{cfg.name} x GreenBay swap stickers")
    c.setAuthor("GreenBay Second Life Appliances")

    def Y(y_top: float) -> float:  # top-left mm -> PDF points from bottom
        return (PAGE_H - y_top) * mm

    current = 0
    for (page, x, y), code in zip(_slots(len(codes)), codes):
        if page != current:
            c.showPage()
            current = page
        c.setStrokeColor(HexColor(col["line"]))
        c.setLineWidth(0.3 * mm)
        c.setFillColor(white)
        c.roundRect(x * mm, Y(y + ST_H), ST_W * mm, ST_H * mm, 4 * mm, stroke=1, fill=1)
        # band with rounded top corners
        c.setFillColor(HexColor(col["band"]))
        p = c.beginPath()
        r = 4
        p.moveTo(x * mm, Y(y + BAND_H))
        p.lineTo(x * mm, Y(y + r))
        p.arcTo(x * mm, Y(y + 2 * r), (x + 2 * r) * mm, Y(y), startAng=180, extent=-90)
        p.lineTo((x + ST_W - r) * mm, Y(y))
        p.arcTo((x + ST_W - 2 * r) * mm, Y(y + 2 * r), (x + ST_W) * mm, Y(y), startAng=90, extent=-90)
        p.lineTo((x + ST_W) * mm, Y(y + BAND_H))
        p.close()
        c.drawPath(p, stroke=0, fill=1)
        logo_w = min(10.0 * logo_ratio, 42.0)
        logo_h = logo_w / logo_ratio
        c.drawImage(logo_img, (x + 7) * mm, Y(y + (BAND_H + logo_h) / 2), logo_w * mm, logo_h * mm, mask="auto")
        gb_h = 4.6
        gb_w = gb_h * gb_ratio
        c.drawImage(gb_img, (x + ST_W - 7 - gb_w) * mm, Y(y + (BAND_H + gb_h) / 2), gb_w * mm, gb_h * mm, mask="auto")
        c.setFillColor(white)
        c.setFont("Helvetica", 11)
        c.drawRightString((x + ST_W - 7 - gb_w - 2) * mm, Y(y + BAND_H / 2 + 1.4), "×")
        # words
        c.setFillColor(HexColor(col["ink"]))
        c.setFont("Helvetica-Bold", 17.5)
        c.drawCentredString((x + ST_W / 2) * mm, Y(y + 36), headline)
        c.setFillColor(HexColor(col["muted"]))
        c.setFont("Helvetica", 9.6)
        for i, line in enumerate(_wrap(sub, 46)):
            c.drawCentredString((x + ST_W / 2) * mm, Y(y + 42.5 + i * 4.2), line)
        # QR
        matrix = _matrix(short_url(code["code"]))
        n = len(matrix)
        m = QR_MM / n
        qx = x + (ST_W - QR_MM) / 2
        c.setFillColor(HexColor(col["ink"]))
        for ri, row in enumerate(matrix):
            ci = 0
            while ci < n:
                if row[ci]:
                    s = ci
                    while ci < n and row[ci]:
                        ci += 1
                    c.rect((qx + s * m) * mm, Y(y + QR_TOP + (ri + 1) * m), (ci - s) * m * mm, m * mm, stroke=0, fill=1)
                else:
                    ci += 1
        c.setFont("Helvetica-Bold", 10.2)
        c.drawCentredString((x + ST_W / 2) * mm, Y(y + QR_TOP + QR_MM + 6), short_url_display(code["code"]))
        c.setStrokeColor(HexColor(col["line"]))
        c.line((x + 7) * mm, Y(y + ST_H - 10), (x + ST_W - 7) * mm, Y(y + ST_H - 10))
        c.setFillColor(HexColor(col["muted"]))
        c.setFont("Helvetica", 8.2)
        c.drawCentredString((x + ST_W / 2) * mm, Y(y + ST_H - 5.6), "Valued and bought by GreenBay")
        c.setFont("Helvetica", 6.5)
        tag = " · ".join(t for t in (code["code"], code.get("label") or "") if t)
        c.drawCentredString((x + ST_W / 2) * mm, Y(y + ST_H - 2.4), tag[:60])
    c.showPage()
    c.save()
    return buf.getvalue()

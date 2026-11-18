"""Server-side assembly of a partner page.

``frontend/partner.html`` is the shell: co-brand bar, hero, numbered steps,
the credit table, questions and footer. The evaluator wizard itself is NOT
copied into it: the ``<section id="evaluate">`` block is cut out of
``frontend/index.html`` at serve time and dropped in, so there is one wizard
for every portal and partner, and a fix to it reaches all of them.

Every value from a config file is HTML-escaped before it goes in. The page
config (public part only) is embedded as JSON for app.js and partner.js.
"""

from __future__ import annotations

import html
import json
import re
from functools import lru_cache
from pathlib import Path

from greenbay_ai_evaluator.partners.config import PartnerConfig, get_partner, public_config

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
TEMPLATE = FRONTEND_DIR / "partner.html"
INDEX = FRONTEND_DIR / "index.html"

_WIZARD_RE = re.compile(r'<section class="evaluation-section" id="evaluate">.*?</section>', re.S)
_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")
_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{3,8}$")
_FONT_RE = re.compile(r"^[A-Za-z0-9 ,'\-]+$")
_SAFE_LEN_RE = re.compile(r"^[0-9.]+(px|rem|em|%)$")


class PageError(Exception):
    pass


def _e(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def extract_wizard(index_html: str) -> str:
    m = _WIZARD_RE.search(index_html)
    if not m:
        raise PageError("index.html has no evaluation section to reuse")
    return m.group(0)


def _brand_wizard(wizard: str, copy: dict) -> str:
    """Partner wording for the wizard's section header."""
    def sub(cls: str, key: str, text: str) -> str:
        if not copy.get(key):
            return text
        return re.sub(
            rf'(<(?:div|h2|p) class="{cls}"[^>]*>).*?(</(?:div|h2|p)>)',
            lambda m: f"{m.group(1)}{_e(copy[key])}{m.group(2)}", text, count=1, flags=re.S,
        )

    wizard = sub("section-label", "eval_label", wizard)
    wizard = sub("section-title", "eval_title", wizard)
    wizard = sub("section-subtitle", "eval_sub", wizard)
    return wizard


def theme_css(cfg: PartnerConfig) -> str:
    """CSS custom properties for the partner, mapped onto the wizard's own
    tokens so the shared wizard picks up the partner's look. Only colour,
    font and length values that match strict patterns get through."""
    t = cfg.theme or {}
    c = t.get("colors") or {}

    def col(key: str, default: str) -> str:
        v = str(c.get(key) or default)
        return v if _COLOR_RE.match(v) else default

    def font(key: str, default: str) -> str:
        v = str(t.get(key) or default)
        return v if _FONT_RE.match(v) else default

    radius = str(t.get("radius_card") or "14px")
    radius = radius if _SAFE_LEN_RE.match(radius) else "14px"
    ink, paper = col("ink", "#111111"), col("paper", "#F4F2ED")
    band = col("band", ink)
    accent, accent_hover = col("accent", ink), col("accent_hover", "#2B2B2B")
    cosign, cosign_soft = col("cosign", "#0B6E40"), col("cosign_soft", "#E3EFE6")
    line, stone, muted = col("line", "#D6D0C4"), col("stone", "#E8E4DB"), col("muted", "#59554D")
    line_strong = col("line_strong", "#8A8478")
    on_ink, on_ink_muted = col("on_ink", paper), col("on_ink_muted", "#BDB7AB")
    display = font("font_display", "system-ui, sans-serif")
    body = font("font_body", "system-ui, sans-serif")
    serif = font("font_serif", "Georgia, serif")
    sel = f'html[data-country][data-partner="{cfg.slug}"]'
    return (
        f"{sel}{{"
        f"--p-ink:{ink};--p-band:{band};--p-paper:{paper};--p-stone:{stone};--p-line:{line};--p-muted:{muted};"
        f"--p-on-ink:{on_ink};--p-on-ink-muted:{on_ink_muted};--p-accent:{accent};"
        f"--p-accent-hover:{accent_hover};--p-cosign:{cosign};--p-cosign-soft:{cosign_soft};"
        f"--p-font-display:{display};--p-font-body:{body};--p-font-serif:{serif};--p-radius:{radius};"
        # The shared wizard's tokens (styles.css since the Nov 2026 portal
        # redesign): surfaces, text, lines, the dark "coal" bands, the accent
        # that fills buttons and selected chips, fonts, and the flag strips,
        # which become the partner's band colour.
        f"--paper:#FFFFFF;--soft:{stone};--soft-2:{line};"
        f"--ink:{ink};--ink-2:{muted};--ink-3:{muted};--line:{line};--line-strong:{line_strong};"
        f"--coal:{band};--coal-2:{band};--coal-line:{band};--on-coal:{on_ink};--on-coal-2:{on_ink_muted};"
        f"--accent:{accent};--accent-ink:{accent};--accent-soft:{stone};--accent-2:{cosign};"
        f"--ring:color-mix(in srgb,{accent} 28%,transparent);"
        f"--btn-bg:{accent};--btn-bg-hover:{accent_hover};"
        f"--grad:linear-gradient(90deg,{accent} 0%,{accent} 70%,{cosign} 100%);--grad-ink:{accent};"
        f"--flag:{band};--flag-swatch:{band};"
        f"--f-display:{display};--f-body:{body};"
        f"}}"
    )


def _steps(copy: dict) -> str:
    out = []
    for i, step in enumerate(copy.get("steps") or [], 1):
        out.append(
            f'<li class="p-step" data-reveal style="--i:{i}">'
            f'<span class="p-step-num" aria-hidden="true">{i:02d}</span>'
            f'<h3>{_e(step.get("title"))}</h3><p>{_e(step.get("body"))}</p></li>'
        )
    return "".join(out)


def _rows(copy: dict) -> str:
    return "".join(
        f'<tr><th scope="row">{_e(k)}</th><td>{_e(v)}</td></tr>' for k, v in (copy.get("credit_rows") or [])
    )


def _faq(copy: dict) -> str:
    return "".join(
        f'<details class="p-q" data-reveal><summary>{_e(q)}</summary><p>{_e(a)}</p></details>'
        for q, a in (copy.get("faq") or [])
    )


def _config_json(cfg: PartnerConfig) -> str:
    data = public_config(cfg)
    # Nothing in it can close the tag early or be read as markup.
    return (json.dumps(data, ensure_ascii=False)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


# Files whose change must bust the browser cache (the ?v= on every asset).
VERSIONED = ("partner.html", "index.html", "partner.css", "partner.js", "app.js", "styles.css")


def _asset_version() -> str:
    newest = 0.0
    for name in VERSIONED:
        try:
            newest = max(newest, (FRONTEND_DIR / name).stat().st_mtime)
        except OSError:
            pass
    return str(int(newest))


@lru_cache(maxsize=16)
def _render(slug: str, version: str) -> str:
    cfg = get_partner(slug)
    if cfg is None:
        raise PageError(f"unknown partner {slug}")
    copy = cfg.copy_text or {}
    theme = cfg.theme or {}
    colors = theme.get("colors") or {}
    wizard = _brand_wizard(extract_wizard(INDEX.read_text(encoding="utf-8")), copy)
    # The wizard's relative asset links (if any) must point at /app/.
    wizard = re.sub(r'(\s(?:src|href)=")(?!https?:|//|/|#|mailto:|tel:|data:)([^"]+)"',
                    lambda m: f'{m.group(1)}/app/{m.group(2)}"', wizard)

    values = {
        "slug": cfg.slug,
        "name": cfg.name,
        "path": cfg.path,
        "country": cfg.countries[0] if cfg.countries else "KE",
        "expiry_days": cfg.credit.expiry_days,
        "logo": theme.get("logo", ""),
        "logo_on_dark": theme.get("logo_on_dark", theme.get("logo", "")),
        "logo_alt": theme.get("logo_alt", cfg.name),
        "fonts_url": theme.get("fonts_url", ""),
        "asset_version": version,
        "color.band": colors.get("band", colors.get("ink", "#111111")),
    }
    raw_values = {
        "theme_css": theme_css(cfg),
        "config_json": _config_json(cfg),
        "steps_html": _steps(copy),
        "credit_rows_html": _rows(copy),
        "faq_html": _faq(copy),
        "wizard_html": wizard,
    }

    def fill(m: re.Match) -> str:
        key = m.group(1)
        if key in raw_values:
            return raw_values[key]
        if key.startswith("copy."):
            return _e(copy.get(key[5:], ""))
        if key in values:
            return _e(values[key])
        raise PageError(f"partner.html uses unknown placeholder {key}")

    return _PLACEHOLDER.sub(fill, TEMPLATE.read_text(encoding="utf-8"))


def render(slug: str) -> str:
    return _render(slug, _asset_version())

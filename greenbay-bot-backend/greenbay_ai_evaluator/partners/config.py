"""Partner configuration: one JSON file per partner in ``configs/``.

A partner (Mika, Vision Plus, ...) is a supplier whose customers trade in an
old appliance of the partner's own brand for credit they can spend only with
that partner. Everything that differs between partners lives in its config
file, so adding a partner is a new file, not new code:

* identity: slug, name, path, reference and voucher prefixes
* where it runs: countries and currency
* what it accepts: brands (plus spelling aliases) and engine categories
* credit rules: KES floor, nominal value for very old items, expiry
* QR placements and shops, and who is emailed for each event

The files are read once and cached. ``public_config`` is the only shape that
leaves the server for customers: it never carries email addresses.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

CONFIG_DIR = Path(__file__).resolve().parent / "configs"

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{1,23}$")

NOTIFY_EVENTS = ("accepted", "needs_validation", "credit_issued", "credit_used")

# Path segments the partner routes use themselves (/tradein/partners/admin/...)
# or that the site already serves (/app, /q, ...): never a partner slug.
RESERVED_SLUGS = frozenset({"admin", "app", "q", "tradein", "webhook", "dashboard", "health", "api", "monitoring"})


class CreditRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    floor: float = Field(500, ge=0, description="Least credit for an accepted item")
    nominal_age_years: float = Field(12, gt=0, description="At or over this age the item gets nominal_value")
    nominal_value: float = Field(500, ge=0)
    expiry_days: int = Field(90, ge=1, le=3650)
    max_auto_risk_score: float = Field(70, ge=0, le=100, description="Above this, no automatic credit offer")


class Placement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    channel: str
    placement: str
    label: str = ""
    shop: str | None = None

    @field_validator("code")
    @classmethod
    def _code(cls, v: str) -> str:
        v = v.strip().upper()
        if not CODE_RE.match(v):
            raise ValueError(f"bad QR code {v!r}")
        return v


class Shop(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    name: str
    area: str = ""


class BuyCategory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str


class PartnerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    name: str
    legal_name: str = ""
    path: str
    ref_prefix: str
    voucher_prefix: str
    countries: list[str]
    currency: str = "KES"
    brands: list[str]
    brand_aliases: list[str] = Field(default_factory=list)
    categories: list[str]
    bulky_categories: list[str] = Field(default_factory=list)
    bulky_min_size: dict[str, float] = Field(default_factory=dict)
    buy_categories: list[BuyCategory] = Field(default_factory=list)
    credit: CreditRules = Field(default_factory=CreditRules)
    placements: list[Placement] = Field(default_factory=list)
    shops: list[Shop] = Field(default_factory=list)
    notify: dict[str, list[str]] = Field(default_factory=dict)
    # Look and copy for the branded front end (stage 2). Free-form on purpose:
    # the page reads it, the server only passes it through.
    theme: dict[str, Any] = Field(default_factory=dict)
    copy_text: dict[str, Any] = Field(default_factory=dict, alias="copy")

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not SLUG_RE.match(v):
            raise ValueError(f"bad partner slug {v!r}")
        if v in RESERVED_SLUGS:
            raise ValueError(f"partner slug {v!r} is reserved")
        return v

    @field_validator("countries")
    @classmethod
    def _countries(cls, v: list[str]) -> list[str]:
        return [c.strip().upper() for c in v if c.strip()]

    @field_validator("notify")
    @classmethod
    def _notify(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        unknown = set(v) - set(NOTIFY_EVENTS)
        if unknown:
            raise ValueError(f"unknown notify events {sorted(unknown)}")
        return v

    def placement(self, code: str | None) -> Placement | None:
        if not code:
            return None
        code = code.strip().upper()
        return next((p for p in self.placements if p.code == code), None)

    def shop(self, slug: str | None) -> Shop | None:
        if not slug:
            return None
        return next((s for s in self.shops if s.slug == slug), None)


def _load_file(path: Path) -> PartnerConfig:
    with path.open(encoding="utf-8") as fh:
        cfg = PartnerConfig.model_validate(json.load(fh))
    if path.stem != cfg.slug:
        raise ValueError(f"{path.name}: file name must equal its slug {cfg.slug!r}")
    return cfg


@lru_cache(maxsize=1)
def _registry() -> dict[str, PartnerConfig]:
    out: dict[str, PartnerConfig] = {}
    for path in sorted(CONFIG_DIR.glob("*.json")):
        cfg = _load_file(path)
        out[cfg.slug] = cfg
    # Every QR code must be unique across partners: a scan has to name one.
    seen: dict[str, str] = {}
    for cfg in out.values():
        for p in cfg.placements:
            if p.code in seen:
                raise ValueError(f"QR code {p.code} used by {seen[p.code]} and {cfg.slug}")
            seen[p.code] = cfg.slug
    return out


def all_partners() -> list[PartnerConfig]:
    return list(_registry().values())


def get_partner(slug: str | None) -> PartnerConfig | None:
    if not slug:
        return None
    return _registry().get(str(slug).strip().lower())


def partner_for_code(code: str | None) -> tuple[PartnerConfig, Placement] | None:
    """The partner and placement a config-defined QR code belongs to."""
    if not code:
        return None
    for cfg in _registry().values():
        p = cfg.placement(code)
        if p:
            return cfg, p
    return None


def public_config(cfg: PartnerConfig) -> dict[str, Any]:
    """What the customer's browser may see. No emails, no internal rules."""
    return {
        "slug": cfg.slug,
        "name": cfg.name,
        "path": cfg.path,
        "countries": cfg.countries,
        "currency": cfg.currency,
        "brands": cfg.brands,
        "categories": cfg.categories,
        "buy_categories": [b.model_dump() for b in cfg.buy_categories],
        "credit": {
            "floor": cfg.credit.floor,
            "expiry_days": cfg.credit.expiry_days,
        },
        "theme": cfg.theme,
        "copy": cfg.copy_text,
    }

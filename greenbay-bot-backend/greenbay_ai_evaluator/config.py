"""
Evaluator-specific configuration and constants.

Defect deduction lookup tables and helper utilities used by the offer engine.
All monetary values are in KES.
"""

# ---------------------------------------------------------------------------
# Defect deduction table — category → defect_type → KES deduction
# ---------------------------------------------------------------------------
DEFECT_DEDUCTIONS: dict[str, dict[str, float]] = {
    "refrigerator": {
        "broken_seal": 2000,
        "missing_seal": 2000,
        "missing_shelf": 500,
        "missing_tray": 500,
        "no_interior_light": 1000,
        "broken_thermostat": 2500,
        "dented_door": 1500,
        "compressor_noise": 3000,
        "cosmetic_scratch": 300,
        "cosmetic_dent": 500,
        "rust": 1000,
    },
    "washing_machine": {
        "noisy_drum": 2000,
        "missing_hoses": 500,
        "moldy_seal": 1000,
        "broken_door_lock": 1500,
        "water_leak": 2000,
        "broken_control_panel": 2500,
        "cosmetic_scratch": 300,
        "cosmetic_dent": 500,
        "rust": 1000,
    },
    "tv": {
        "dead_pixels": 2000,
        "screen_crack": 5000,
        "no_sound": 2000,
        "broken_stand": 800,
        "missing_remote": 500,
        "backlight_issue": 3000,
        "cosmetic_scratch": 300,
    },
    "cooker": {
        "missing_knob": 300,
        "non_working_burner": 1500,
        "broken_oven": 3000,
        "broken_glass_top": 2500,
        "missing_tray": 400,
        "broken_hinge": 800,
        "cosmetic_scratch": 300,
        "cosmetic_dent": 500,
        "rust": 800,
    },
    "microwave": {
        "no_turntable": 500,
        "broken_door": 1500,
        "display_fault": 1000,
        "magnetron_weak": 3000,
        "cosmetic_scratch": 200,
        "cosmetic_dent": 400,
        "rust": 600,
    },
    "air_conditioner": {
        "low_cooling": 3000,
        "compressor_noise": 3500,
        "missing_remote": 500,
        "refrigerant_leak": 4000,
        "dirty_filter": 300,
        "broken_louver": 800,
        "cosmetic_scratch": 300,
        "cosmetic_dent": 500,
    },
    "water_dispenser": {
        "no_cooling": 2000,
        "no_heating": 1500,
        "water_leak": 1500,
        "broken_tap": 800,
        "cosmetic_scratch": 200,
        "cosmetic_dent": 400,
    },
    "small_kitchen": {
        "motor_fault": 2000,
        "broken_blade": 800,
        "missing_attachment": 500,
        "missing_lid": 300,
        "broken_handle": 400,
        "cracked_jug": 1000,
        "cosmetic_scratch": 200,
        "cosmetic_dent": 300,
        "rust": 500,
    },
    "smartphone": {
        "cracked_screen": 4000,
        "dead_pixels": 2000,
        "battery_degraded": 1500,
        "charging_port_fault": 1000,
        "speaker_fault": 800,
        "camera_fault": 1200,
        "missing_back_cover": 300,
        "cosmetic_scratch": 200,
        "water_damage": 3000,
    },
    # Category aliases — frontend sends these names
    "cooker_oven": {
        "missing_knob": 300,
        "non_working_burner": 1500,
        "broken_oven": 3000,
        "broken_glass_top": 2500,
        "missing_tray": 400,
        "broken_hinge": 800,
        "cosmetic_scratch": 300,
        "cosmetic_dent": 500,
        "rust": 800,
    },
    "tv_monitor": {
        "dead_pixels": 2000,
        "screen_crack": 5000,
        "no_sound": 2000,
        "broken_stand": 800,
        "missing_remote": 500,
        "backlight_issue": 3000,
        "cosmetic_scratch": 300,
    },
}

# Fallback deduction when defect type is not in the lookup
DEFAULT_DEFECT_DEDUCTION: float = 500.0


# ---------------------------------------------------------------------------
# Multi-country currency configuration (v6)
# ---------------------------------------------------------------------------
# round_step is the price granularity in local units. KES 500 is roughly
# UGX 14,000 and NGN 6,000, so the Kenyan step would print UGX prices to the
# shilling; these keep offers to a sensible round figure in each market.
CURRENCY_CONFIG: dict[str, dict[str, str | int]] = {
    "KE": {"code": "KES", "symbol": "KES", "round_step": 500},
    "UG": {"code": "UGX", "symbol": "UGX", "round_step": 10000},
    "NG": {"code": "NGN", "symbol": "NGN", "round_step": 5000},
}

# Approximate KES value of ONE unit of local currency. Used only to carry the
# KES-denominated constants (defect deductions, category default retail
# guesses) into a local-currency evaluation. Never shown to a customer and
# never used to convert a price between markets.
KES_PER_LOCAL_UNIT: dict[str, float] = {"KES": 1.0, "UGX": 0.0357, "NGN": 0.085}

# Markets whose prices come from GreenBay's own Kenyan data (sales stock,
# pricing matrix, tracker sheet, expert feedback, DB comparables, Jiji/Jumia
# Kenya). Every other market is priced from a local new-price lookup plus the
# shared depreciation/condition/acquisition maths, and always goes to a person
# until it has local history of its own.
KES_REFERENCE_MARKETS: frozenset[str] = frozenset({"KE"})

# Highest confidence a market without local reference data may report. Below
# the 80 review gate on purpose: these offers are preliminary and a person
# confirms every one.
LOCAL_PRICING_CONFIDENCE_CAP: float = 60.0


def normalise_country(country: str | None) -> str:
    """Upper-case country code; anything unknown is treated as Kenya."""
    code = (country or "KE").strip().upper()
    return code if code in CURRENCY_CONFIG else "KE"


def uses_kes_reference_data(country: str | None) -> bool:
    """True when the evaluation can be priced from GreenBay's Kenyan data."""
    return normalise_country(country) in KES_REFERENCE_MARKETS


def local_units_per_kes(country: str | None) -> float:
    """How many local currency units one KES is worth (1.0 for Kenya)."""
    code = str(CURRENCY_CONFIG[normalise_country(country)]["code"])
    rate = KES_PER_LOCAL_UNIT.get(code, 1.0)
    return 1.0 / rate if rate else 1.0

PHONE_COUNTRY_PREFIXES: dict[str, str] = {
    "+254": "KE",
    "254": "KE",
    "+256": "UG",
    "256": "UG",
    "+234": "NG",
    "234": "NG",
}


def detect_country_from_phone(phone: str) -> str:
    """Derive country code from phone number prefix. Defaults to KE."""
    if not phone:
        return "KE"
    cleaned = phone.strip().lstrip("0")
    for prefix, country in PHONE_COUNTRY_PREFIXES.items():
        if cleaned.startswith(prefix):
            return country
    return "KE"


def get_currency_config(country: str = "KE") -> dict:
    """Return currency config for a country code."""
    return CURRENCY_CONFIG[normalise_country(country)]


# ---------------------------------------------------------------------------
# Default condition grade mapping (text → letter)
# ---------------------------------------------------------------------------
CONDITION_GRADE_MAP: dict[str, str] = {
    "excellent": "A",
    "good": "B",
    "fair": "C",
    "poor": "D",
    # Already-letter inputs
    "a": "A",
    "b": "B",
    "c": "C",
    "d": "D",
}

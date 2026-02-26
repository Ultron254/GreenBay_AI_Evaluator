#!/usr/bin/env python3
"""
Seed the pricing_policy table with initial category data.

Usage:
    python scripts/seed_pricing_policy.py

Idempotent — uses upsert-on-category logic.
"""

import os
import sys

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database.db import SessionLocal
from greenbay_ai_evaluator.models.evaluator_models import PricingPolicy


# Shared brand premiums
BRAND_PREMIUMS = {
    "Samsung": 1.10,
    "LG": 1.08,
    "Sony": 1.05,
    "Bosch": 1.12,
    "Whirlpool": 1.05,
    "Hisense": 1.00,
    "TCL": 0.98,
    "Hotpoint": 1.00,
    "Ramtons": 0.95,
    "Von": 0.85,
    "Mika": 0.82,
    "Sayona": 0.80,
    "Bruhm": 0.85,
    "Roch": 0.80,
}

# Shared condition multipliers
CONDITION_MULTIPLIERS = {"A": 1.00, "B": 0.85, "C": 0.65, "D": 0.40}


SEED_DATA = [
    {
        "category": "refrigerator",
        "margin_pct": 0.30,
        "max_offer_pct": 0.70,
        "walkaway_pct": 0.35,
        "depreciation_year1": 0.20,
        "depreciation_year2_3": 0.12,
        "depreciation_year4_5": 0.10,
        "depreciation_year6_plus": 0.08,
        "round_step_pct": 0.05,
        "max_negotiation_rounds": 3,
    },
    {
        "category": "washing_machine",
        "margin_pct": 0.30,
        "max_offer_pct": 0.65,
        "walkaway_pct": 0.30,
        "depreciation_year1": 0.25,
        "depreciation_year2_3": 0.15,
        "depreciation_year4_5": 0.10,
        "depreciation_year6_plus": 0.08,
        "round_step_pct": 0.05,
        "max_negotiation_rounds": 3,
    },
    {
        "category": "tv",
        "margin_pct": 0.35,
        "max_offer_pct": 0.60,
        "walkaway_pct": 0.25,
        "depreciation_year1": 0.30,
        "depreciation_year2_3": 0.18,
        "depreciation_year4_5": 0.12,
        "depreciation_year6_plus": 0.08,
        "round_step_pct": 0.05,
        "max_negotiation_rounds": 3,
    },
    {
        "category": "cooker",
        "margin_pct": 0.25,
        "max_offer_pct": 0.70,
        "walkaway_pct": 0.40,
        "depreciation_year1": 0.15,
        "depreciation_year2_3": 0.10,
        "depreciation_year4_5": 0.08,
        "depreciation_year6_plus": 0.06,
        "round_step_pct": 0.05,
        "max_negotiation_rounds": 3,
    },
    {
        "category": "microwave",
        "margin_pct": 0.30,
        "max_offer_pct": 0.60,
        "walkaway_pct": 0.25,
        "depreciation_year1": 0.25,
        "depreciation_year2_3": 0.15,
        "depreciation_year4_5": 0.12,
        "depreciation_year6_plus": 0.10,
        "round_step_pct": 0.05,
        "max_negotiation_rounds": 3,
    },
    {
        "category": "air_conditioner",
        "margin_pct": 0.30,
        "max_offer_pct": 0.65,
        "walkaway_pct": 0.30,
        "depreciation_year1": 0.20,
        "depreciation_year2_3": 0.12,
        "depreciation_year4_5": 0.10,
        "depreciation_year6_plus": 0.08,
        "round_step_pct": 0.05,
        "max_negotiation_rounds": 3,
    },
    {
        "category": "water_dispenser",
        "margin_pct": 0.25,
        "max_offer_pct": 0.65,
        "walkaway_pct": 0.35,
        "depreciation_year1": 0.20,
        "depreciation_year2_3": 0.12,
        "depreciation_year4_5": 0.10,
        "depreciation_year6_plus": 0.08,
        "round_step_pct": 0.05,
        "max_negotiation_rounds": 3,
    },
]


def seed():
    """Upsert pricing policy rows."""
    db = SessionLocal()
    try:
        for row in SEED_DATA:
            existing = (
                db.query(PricingPolicy)
                .filter(PricingPolicy.category == row["category"])
                .first()
            )
            if existing:
                # Update in place
                for key, value in row.items():
                    setattr(existing, key, value)
                existing.brand_premium_json = BRAND_PREMIUMS
                existing.condition_multiplier_json = CONDITION_MULTIPLIERS
                existing.is_active = True
                print(f"  Updated: {row['category']}")
            else:
                policy = PricingPolicy(
                    **row,
                    brand_premium_json=BRAND_PREMIUMS,
                    condition_multiplier_json=CONDITION_MULTIPLIERS,
                    is_active=True,
                )
                db.add(policy)
                print(f"  Created: {row['category']}")

        db.commit()
        print(f"\nSeeded {len(SEED_DATA)} pricing policy records.")
    except Exception as e:
        db.rollback()
        print(f"ERROR: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    print("Seeding pricing_policy table...")
    seed()
    print("Done.")

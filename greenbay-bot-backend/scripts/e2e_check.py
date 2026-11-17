"""End-to-end check of the live evaluator, run by the deploy after every release.

Runs INSIDE the app container (python + requests are there) against
http://localhost:9100, so it exercises the real stack:

  1. the three country portals (/app/ke, /app/ug, /app/ng) serve the wizard;
  2. one ZZSMOKE evaluation per market: priced, in the right currency, and
     Uganda/Nigeria routed to a person;
  3. each evaluation is readable back from Postgres (GET /tradein/{id});
  4. each evaluation reached Airtable (its "Ref: xxxxxxxx" shows up in the most
     recent rows' Notes);
  5. every live service probe: vision, Gemini, Sonar, Sheets, Airtable, S3,
     the email transport actually used (SMTP or SES), and the database.

The email itself is confirmed by the deploy step, which greps the container
log for the "Email: sent" line the lead email writes for these evaluations.

Prints one line per check and a summary. Never prints a key or a phone
number. Exit code 1 when any check fails, so the deploy log shows a warning.

ZZSMOKE rows are removed afterwards by POST /tradein/admin/purge-test-records,
which the deploy step already runs.

Usage (deploy step):
    docker exec greenbay-bot python scripts/e2e_check.py   (the container already has DASHBOARD_KEY)
"""

from __future__ import annotations

import os
import sys
import time

import requests

BASE = os.environ.get("E2E_BASE_URL", "http://localhost:9100")
KEY = os.environ.get("DASHBOARD_KEY", "").strip()

MARKETS = {
    # country: (currency, phone, expect_review)
    "KE": ("KES", "+254700000000", False),
    "UG": ("UGX", "+256700000000", True),
    "NG": ("NGN", "+2348030000000", True),
}

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> bool:
    results.append((bool(ok), label))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}", flush=True)
    return bool(ok)


def admin_headers() -> dict[str, str]:
    return {"X-Admin-Key": KEY} if KEY else {}


def portals() -> None:
    for code in ("ke", "ug", "ng"):
        try:
            r = requests.get(f"{BASE}/app/{code}", timeout=20)
            check(r.status_code == 200 and 'id="wizardPanel"' in r.text,
                  f"portal /app/{code} serves the wizard (HTTP {r.status_code})")
        except Exception as e:  # noqa: BLE001
            check(False, f"portal /app/{code}: {type(e).__name__}")


def evaluate(country: str) -> str | None:
    currency, phone, expect_review = MARKETS[country]
    body = {
        "category": "refrigerator", "brand": "Samsung", "model": f"ZZSMOKE-E2E-{country}",
        "age_years": 2, "condition_grade": "B", "condition_score": 70,
        "defects": [{"type": "cosmetic_dent", "description": "Dents", "severity": "medium"}],
        "retail_price": 65000, "retail_price_source": "category_default",
        "seller_name": f"ZZSMOKE E2E {country}", "seller_phone": phone, "country": country,
    }
    data = None
    for attempt in range(3):
        try:
            r = requests.post(f"{BASE}/tradein/evaluate", json=body, timeout=150)
            if r.status_code == 200:
                data = r.json()
                break
            print(f"  {country} attempt {attempt + 1}: HTTP {r.status_code}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"  {country} attempt {attempt + 1}: {type(e).__name__}", flush=True)
        time.sleep(8)
    if not check(data is not None, f"{country} evaluation returned a result"):
        return None
    sid = str(data.get("session_id") or "")
    check(data.get("currency_code") == currency and data.get("country") == country,
          f"{country} priced in {data.get('currency_code')} (expected {currency})")
    check(float(data.get("opening_offer") or 0) > 0,
          f"{country} offer {data.get('currency_code')} {float(data.get('opening_offer') or 0):,.0f}")
    if expect_review:
        check(data.get("decision") == "review",
              f"{country} routed to a person (decision={data.get('decision')})")
    return sid


def database(country: str, sid: str) -> None:
    try:
        r = requests.get(f"{BASE}/tradein/{sid}", timeout=20)
        check(r.status_code == 200, f"{country} session {sid[:8]} read back from Postgres (HTTP {r.status_code})")
    except Exception as e:  # noqa: BLE001
        check(False, f"{country} session read-back: {type(e).__name__}")


def airtable(refs: dict[str, str]) -> None:
    if not KEY:
        check(False, "Airtable check skipped: DASHBOARD_KEY not set in the container")
        return
    # Writes are fire-and-forget; give them time, then look at the newest rows.
    pending = dict(refs)
    for _ in range(6):
        time.sleep(10)
        try:
            r = requests.get(f"{BASE}/tradein/admin/airtable-debug?n=10",
                             headers=admin_headers(), timeout=60)
            if r.status_code != 200:
                print(f"  airtable-debug HTTP {r.status_code}", flush=True)
                continue
            notes = " ".join(str(x.get("notes") or "") for x in r.json().get("recent", []))
            for country, ref in list(pending.items()):
                if f"Ref: {ref}" in notes:
                    pending.pop(country)
            if not pending:
                break
        except Exception as e:  # noqa: BLE001
            print(f"  airtable-debug: {type(e).__name__}", flush=True)
    for country, ref in refs.items():
        check(country not in pending, f"{country} evaluation {ref} reached Airtable")


def services() -> None:
    if not KEY:
        check(False, "service probes skipped: DASHBOARD_KEY not set in the container")
        return
    try:
        r = requests.get(f"{BASE}/tradein/health/services", headers=admin_headers(), timeout=180)
        if not check(r.status_code == 200, f"service probes answered (HTTP {r.status_code})"):
            return
        for name, res in sorted((r.json().get("services") or {}).items()):
            detail = str(res.get("detail") or "")[:140]
            check(bool(res.get("ok")), f"service {name}: {detail}")
    except Exception as e:  # noqa: BLE001
        check(False, f"service probes: {type(e).__name__}")


def main() -> int:
    print("=== End-to-end check ===", flush=True)
    portals()
    refs: dict[str, str] = {}
    for country in MARKETS:
        sid = evaluate(country)
        if sid:
            database(country, sid)
            refs[country] = sid[:8]
    if refs:
        airtable(refs)
    services()
    failed = [label for ok, label in results if not ok]
    print(f"=== {len(results) - len(failed)}/{len(results)} checks passed ===", flush=True)
    for label in failed:
        print(f"::warning::E2E: {label}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

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
     the email transport actually used (SMTP or SES), and the database;
  6. one ZZSMOKE Mika partner swap, end to end: pages, short links, a credit
     offer, accept, check, issue, part redeem, settlement, and Pulse's read
     routes hiding it (see partner_swap).

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


def partner_swap() -> None:
    """One ZZSMOKE Mika swap through the whole partner flow on the live stack:
    pages, a short link, a priced evaluation with a credit offer, accept with a
    reference, then (admin key) check booked, passed, credit issued, part
    redeemed, settlement, and Pulse's read routes hiding the test row. A real
    QR code is never scanned, so partners' counts stay true. The purge that
    follows removes the swap's partner rows."""
    for path in ("/mika", "/vision-plus"):
        try:
            r = requests.get(f"{BASE}{path}", timeout=30)
            check(r.status_code == 200 and "data-partner=" in r.text, f"partner page {path} served")
        except Exception as e:  # noqa: BLE001
            check(False, f"partner page {path} ({type(e).__name__})")
    try:
        r = requests.get(f"{BASE}/q/ZZSMOKE-NOPE", allow_redirects=False, timeout=30)
        check(r.status_code == 302 and r.headers.get("location") == "/app/", "QR short links route")
    except Exception as e:  # noqa: BLE001
        check(False, f"QR short links ({type(e).__name__})")

    body = {
        "category": "refrigerator", "brand": "Mika", "model": "ZZSMOKE-E2E-PARTNER",
        "age_years": 13, "condition_grade": "C", "condition_score": 45, "defects": [],
        "retail_price": 40000, "retail_price_source": "category_default",
        "seller_name": "ZZSMOKE E2E Partner", "seller_phone": "+254700000000", "country": "KE",
        "partner": {"slug": "mika", "channel": "direct", "buy_category": "cooker_oven"},
    }
    data = None
    for attempt in range(3):
        try:
            r = requests.post(f"{BASE}/tradein/evaluate", json=body, timeout=150)
            if r.status_code == 200:
                data = r.json()
                break
            print(f"  partner attempt {attempt + 1}: HTTP {r.status_code}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"  partner attempt {attempt + 1}: {type(e).__name__}", flush=True)
        time.sleep(8)
    if not check(data is not None and isinstance(data.get("partner"), dict), "Mika swap evaluation priced"):
        return
    offer = data["partner"].get("credit_offer")
    check(offer == 500, f"Mika swap credit offer KES {offer} (a 13-year-old item gets the KES 500 nominal)")
    sid = data.get("session_id")
    try:
        r = requests.post(f"{BASE}/tradein/{sid}/accept-offer", json={"accepted_amount": 500}, timeout=60)
        ref = r.json().get("reference") if r.status_code == 200 else None
    except Exception:  # noqa: BLE001
        ref = None
    if not check(bool(ref) and str(ref).startswith("MK-"), f"Mika swap accepted, reference {ref}"):
        return
    if not KEY:
        check(False, "partner ops steps skipped: DASHBOARD_KEY not set in the container")
        return
    H = admin_headers()
    try:
        found = requests.get(f"{BASE}/tradein/partners/admin/credits?partner=mika",
                             headers={**H, "X-Lookup-Query": ref}, timeout=30).json()["items"]
        cid = found[0]["id"]
        base = f"{BASE}/tradein/partners/admin/credits/{cid}"
        actor = {"actor": "Deploy check"}
        s1 = requests.post(f"{base}/start-validation", headers=H, json={**actor, "method": "drop_greenbay"}, timeout=30)
        s2 = requests.post(f"{base}/validate", headers=H, json=actor, timeout=30)
        s3 = requests.post(f"{base}/issue", headers=H, json=actor, timeout=30)
        code = s3.json().get("voucher_code") if s3.status_code == 200 else None
        check(s1.status_code == s2.status_code == 200 and bool(code) and code.startswith("MIKA-"),
              f"Mika swap checked, passed and credit issued ({code})")
        s4 = requests.post(f"{BASE}/tradein/partners/admin/redeem", headers=H,
                           json={**actor, "partner": "mika", "voucher_code": code, "amount": 200,
                                 "note": "deploy check"}, timeout=30)
        check(s4.status_code == 200 and s4.json().get("balance") == 300, "Mika credit part redeemed (KES 300 left)")
        month = time.strftime("%Y-%m", time.gmtime())
        st = requests.get(f"{BASE}/tradein/partners/admin/settlement?partner=mika&month={month}",
                          headers=H, timeout=30).json()
        check(any(x.get("voucher_code") == code for x in st.get("redemptions", [])),
              "Mika settlement lists the redemption")
        leads = requests.get(f"{BASE}/tradein/partners/mika/portal/leads", headers=H, timeout=30).json()["items"]
        check(not any(ref == x.get("ref") for x in leads), "Pulse's Mika view hides the test swap")
        vp = requests.get(f"{BASE}/tradein/partners/vision-plus/portal/credits", headers=H, timeout=30).json()["items"]
        check(not any(code == x.get("voucher_code") for x in vp), "Vision Plus cannot see Mika's credit")
    except Exception as e:  # noqa: BLE001
        check(False, f"partner ops steps ({type(e).__name__})")


def main() -> int:
    print("=== End-to-end check ===", flush=True)
    portals()
    partner_swap()
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

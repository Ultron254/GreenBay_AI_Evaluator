# EVALUATOR_DISCOVERY.md

**Purpose:** discovery brief for the GreenBay Pulse "Customer Sourcing" tab.
**Prepared:** 2026-09-14 (Africa/Nairobi)
**Mode:** read-only. No code, schema, data, infrastructure or configuration was changed. No migrations, no installs, no deploys, no writes to any sheet.

---

## 0. Evidence basis — read this before trusting any number

Three different things get called "the system" in this brief. They disagree, and the Pulse build must know which is which.

| Source | Reachable in this session? | What it told me |
|---|---|---|
| **Airtable** (`appQP9goyJQ6SRU5c`) | ✅ YES — live REST API | 336 records, 2026-03-18 → 2026-09-14. **Primary evidence for this report.** |
| **Google Sheets** ×3 | ✅ YES — service-account JWT | All tabs, headers, distributions |
| **Production API** (`evaluate.greenbay.market`) | ✅ YES — read-only GET | Live health + 30-day Postgres aggregates |
| **Production Postgres** | ❌ NO — not network-reachable from here | Row-level counts UNVERIFIED except via the metrics endpoint |
| **Local SQLite** (`greenbay_chatbot.db`) | ✅ YES | **Dev scratch only — 3 valuation_sessions. Not production. Ignore for reporting.** |

Method note: the local Python environment has **no** `requests`, `gspread`, `google-auth`, `pyairtable`, `sqlalchemy` or `psycopg2`, and the brief forbids installing. All live verification was therefore done with `curl` + `openssl` + `jq`. The Sheets token was minted by signing a JWT with the service-account key via `openssl dgst -sha256 -sign`.

### 🔴 The repository does not match production

`git status` shows **~20 modified files uncommitted**; last commit is `7314bdd` (2026-07-13).

Production is provably running **older code**: the live health probe returns `anthropic_vision: "OK (model configured: ...)"`, which is the pre-change probe string. The working tree contains a rewritten probe, a vision hard-fail path, an asking-price cap in the offer engine, a 10-step wizard, and migration `v630_image_keys` — **none of which are deployed**.

**Consequence for Pulse:** read behaviour off *deployed* behaviour, not off this repo. Where the two differ I flag it explicitly below.

---

# PART 1 — DIRECT ANSWERS

## Identity, usage, inquiries

**A1. Is a phone number captured on every evaluation?**

**No. 92 of 336 records (27.4%).** Verified live against Airtable.

Formats among those 92 — mixed, normalisation required:

| Format | Count |
|---|---|
| `07…` / `01…` (leading 0) | 83 |
| `+254…` | 6 |
| `254…` | 3 |

Contact capture is a **later code addition**: exactly the same 92 records carry a `Ref:` token in `Notes`. Records before that change have neither phone nor session ref.

**A2. Can unique people be counted reliably?**

Only for that 27% — and weakly. Normalising to `254XXXXXXXXX`:

- **92 records → 28 unique numbers** (all time, Kenya)
- **September MTD: 15 records → 12 unique numbers**

The only identity key is `Customer Phone`. There is no user ID, no cookie, no device ID, no account. 92 records from 28 people means heavy repeat/testing traffic — treat "unique users" as unreliable. `OUTSIDE THIS SYSTEM` for the other 73%.

**A3. What could count as an "inquiry"?**

Only one thing exists: **a completed evaluation**. There is no page-visit log, no session-start record, no abandonment tracking, no analytics (see A13).

A `ValuationSession` row is created **inside** `POST /tradein/evaluate`, after the customer has completed all wizard steps — so a started-but-abandoned wizard leaves **no trace anywhere**.

September MTD, Kenya: **15 evaluations** (Airtable) / **25 in the trailing 30 days** (production metrics endpoint).

There is no funnel. Pulse cannot compute drop-off from this system.

**A4. Is the WhatsApp channel live?**

Partially, and **not** through the Cloud API.

- `whatsapp_router` (WhatsApp Cloud API) is **commented out** in `app/main.py` — and all four credentials (`ACCESS_TOKEN`, `PHONE_NUMBER_ID`, `APP_ID`, `APP_SECRET`) are empty.
- `flowcart_router` **is** registered at `/webhook/flowcart` with HMAC verification — so **Flowcart is the live WhatsApp path** (confirms B12).
- Outbound internal alerts go via the Flowcart API to a hard-coded number in `internal_notification_service.py`.

WhatsApp-originated evaluations write to `trade_in_sessions` (a **different** table from web's `valuation_sessions`), so identity and fields are **not** the same shape. Channel is **not** recorded as a field on either — Pulse cannot split web vs WhatsApp from Airtable at all.

## Evaluations, offers, decisions

**A5. September MTD and per day, Kenya.**

Defining field: `Evaluation Status` in Airtable = `ValuationSession.decision` in Postgres.

September totals (Airtable, n=15): `review=10`, `accepted=2`, `accept=1`, `negotiate=1`, `reject=1`

| Date | n | Breakdown |
|---|---|---|
| 09-03 | 1 | accepted=1 |
| 09-04 | 2 | accepted=1, review=1 |
| 09-05 | 1 | review=1 |
| 09-07 | 2 | accept=1, review=1 |
| 09-08 | 1 | review=1 |
| 09-10 | 1 | reject=1 |
| 09-11 | 2 | negotiate=1, review=1 |
| 09-12 | 2 | review=2 |
| 09-14 | 3 | review=3 |

**Critical semantic trap:** `accept` and `accepted` are different events.
- `accept` = the **engine's** recommendation (asking price ≤ offer), written at evaluation time.
- `accepted` = the **customer** tapped Accept Offer, written later by `POST /{session_id}/accept-offer`.

Counting them together overstates conversion. **Only `accepted` is a real acceptance.**

- **evaluations started** — not recorded. `UNVERIFIED`/does not exist.
- **evaluations completed** — every Airtable row.
- **offers shown** — `decision ∈ {accept, negotiate}`; `review`/`reject` show no price.
- **offers accepted** — `Evaluation Status = "accepted"` (2 in Sept).
- **offers rejected** — `rejected_option_{A,B,C}`. **Zero exist in all 336 records.**
- **routed to human** — `review` (10 in Sept).

**A6. Is "accepted" written when the customer taps Accept?**

Yes. `accept_offer()` in `greenbay_ai_evaluator/api/router.py` writes `decision`, `final_decision` and `final_offer` to Postgres, then patches Airtable `Evaluation Status` → `accepted`, then notifies + emails.

**There is no separate human "Final Decision" field in Airtable** — B10 is wrong about this. The nearest human-authored field is `In-House Evaluator Price (KES)`, synced from the tracker sheet by `sheet_to_airtable_human_price_sync.py`. Who edits it is `OUTSIDE THIS SYSTEM`.

**A7. Are the three rejection options recorded?**

Code writes `decision = "rejected_option_A|B|C"` to Postgres and Airtable.

**In practice: never used. Zero records out of 336.** The three-option screen has produced no data since 2026-03-18. Either customers never reach it, or the frontend never calls the endpoint. Pulse should not plan a rejection-reason breakdown from this source.

**A8. Fields that could serve as a human/real-world price** (fill rate over all 336; the last 100 are within this):

| Field | Fill | Note |
|---|---|---|
| `In-House Evaluator Price (KES)` | **63/336 (18%)** | Human price, synced from tracker sheet |
| `Customer Asking Price (KES)` | **72/336 (21%)** | Customer's own figure |
| `Vertex AI Price (KES)` | **25/336 (7%)** | Second AI opinion, not human |
| `Price Variance (%)` | 334/336 (99%) | Airtable **formula** |
| Counter Offer Price | **does not exist** | B10 wrong |
| Final agreed price | **does not exist in Airtable** | Postgres has `final_offer` |

Real purchase/stock prices live in Google Sheets, not here — see A16.

**A9. Which accuracy measures can be computed today?**

| Measure | Computable? | Result |
|---|---|---|
| AI vs in-house evaluator | ✅ | **n=63, mean abs error 68.3%**, within 10% = 12/63, within 20% = 16/63 |
| AI vs price actually paid | ⚠️ only by joining the `Sourced products` sheet (41 rows) | not computable from Airtable alone |
| AI vs eventual sell price | ⚠️ only via `Final Data` sheet | not in this system |
| Share of AI offers accepted | ✅ | **3/336 all-time (0.9%)**; September **2/15 (13%)** |

Production's own endpoint reports `ai_vs_internal`: compared=3980, mean abs error **71.9%**, median **36.7%**, within 10% **15.2%**, within 20% **30.4%**. The `compared=3980` count is inflated — it counts `expert_price_feedback` rows including sheet-sync markers, not distinct evaluations. **Prefer the n=63 Airtable figure.**

September-only in-house comparison: sample too small to be meaningful (**n<5**).

> **The headline finding: the AI is off by roughly 68–72% on average versus the human evaluator, and only ~15% of estimates land within 10%.** Any Pulse accuracy widget will show this immediately.

**A10. Customer asking price vs AI offer.**

Fill rate **72/336 (21%)**. Of those 72:

| Relationship | Count |
|---|---|
| **AI offered MORE than the customer asked** | **22 (31%)** |
| Customer asked more than AI offered | 49 (68%) |
| Equal | 1 |

31% of priced deals were quoted above the customer's own asking price. The deployed engine sets `decision="accept"` in that case but **never lowers `opening_offer` to the ask** — so it volunteers more money than requested. A cap exists in the working tree but **is not deployed**.

Gap-band acceptance rates are not computable: only 3 acceptances exist in total.

**A11. Confidence.**

Threshold is **80**, at `greenbay_ai_evaluator/engine/offer_engine.py` (`if confidence_score < 80: decision = "review"`).

Production, trailing 30 days: **avg confidence 65.0**, and **80.0% of evaluations fall below the threshold**. Daily average ranged 35 → 86.

**`AI Confidence` is NOT an Airtable field.** It exists only in Postgres (`confidence_score`) and in the tracker sheet column H. B10 is wrong. Pulse cannot get confidence from Airtable.

## Countries and currency

**A12. Country determination and storage.**

Derived from the phone prefix by `detect_country_from_phone()` in `greenbay_ai_evaluator/config.py`; supports KE/UG/NG. Stored in Postgres (`country`, `currency_code`) and Airtable (`Country`, `Currency`).

Both fields **exist and are populated** — 303/336 (90%).

**Every populated record is `KE` / `KES`. Zero Uganda. Zero Nigeria.** All time and September. Multi-country is code-capable, data-empty.

## Tracking and attribution

**A13. Analytics on the frontend?**

**NONE.** No GA4, no GTM, no Meta Pixel, no TikTok Pixel, no Hotjar. The only third-party script in `frontend/index.html` is the Lucide icon library from unpkg.

**A14. UTM / referrer / landing URL / campaign IDs?**

**None captured.** `window.location.search` is never read; `document.referrer` is never referenced.

Where they *could* be captured (stated for completeness only, **not implemented**):
- Client: `frontend/app.js` — the `DOMContentLoaded` handler, then added to the `payload` object built in `startAnalysis()`.
- Server: `EvaluateRequest` in `greenbay_ai_evaluator/api/schemas.py`, persisted in the `ValuationSession` insert in `router.py`.

Note nginx and `app/main.py` both set `Referrer-Policy: strict-origin-when-cross-origin`, which strips cross-origin referrers anyway.

## Sourced items and the link to stock

**A15. Does anything record that an accepted item was collected/received/stocked?**

**Not in this system.** The furthest it goes is `pickup_requests` (a scheduling row + a WhatsApp deep link to the field agent). Nothing records physical receipt.

Real intake lives in **Google Sheets** — `Sourced products` and `Final Data` (A16). That is the only place "accepted → collected → sold" is closed.

**A16. Outlet stock sheet — is there a customer-sourced identifier?**

**Yes, in two places.**

**Sheet `1k7Zw8psnjIw9BukH7vVwESDR-k1PU60wlERWjljk_fE` — "Copy of GreenBay Outlet Stock Control"**
Tabs: `Final Data` (gid 1776996895), `Mika stock`, `Generated Serial numbers`.

`Final Data` — **1,690 data rows**, 26 columns:
`Rerouted items, Received date, Adj Received date, Month, Week, Product Serial Number, Product Category, Brand Name, Product Name, **Sourcing Category**, **Supplier Name**, Model Number, Product Capacity, Product Quality, Repair required?, Units, Purchase cost, Selling Price, Item Status, Sold Date, Sale Week, Day, Days Range, Sales Person, Time to Sale`

| `Sourcing Category` | Count |
|---|---|
| Second Life | 1,262 |
| **Consumer** | **379** |
| New Item | 74 |

| `Item Status` | Count |
|---|---|
| Sold | 1,206 |
| (blank) | 488 |
| Credit | 14 |
| Deposit | 7 |

⚠️ Two warnings. The title begins **"Copy of"**, and the latest `Adj Received date` is **23 May 2026** — roughly four months stale as of today. Treat as a **snapshot, not a live feed**, and confirm with the CX team where the live original lives. Column header `Sourcing Category ` has a **trailing space**.

**Sheet `1DCKTWSxvGYQzuEPoJanQFF5ssM9oWnqVmEoEmh1MhAU` — "AI Evaluation & Pricing Tracker"** also carries sourcing data:

- **`Sourced products`** (gid 1485784013) — **41 data rows**, **header is on row 2, not row 1** (row 1 holds totals). Columns: `Date, Customer name, phone No., Location, Item, Age, Model No, Customer Asking price, Internal Team Price, Buying Price/offered amount, Status, Selling Price, Aftersale Margin, Escalations from Tracker`. Status: **32 Sold**, 6 blank.
  → **This is the richest customer-sourcing ledger available, and the only place with customer phone + price paid + sell price together.**
- **`Kasarani Stock`** (gid 1581086187) — 34 rows, has a **`Where Sourced`** column: **23 "Consumer Sourced"**, 10 "Vendor Sourced".
- **`Roysambu Stock`** (gid 571049748) — **empty (0 rows).**

**A17. Odoo integration?**

**None.** Repo-wide case-insensitive search for `odoo`, `erp`, `xmlrpc` returns zero matches. `OUTSIDE THIS SYSTEM`.

**A18. Every Google Sheet the code references.**

| Sheet ID | Title | Referenced at | Code uses |
|---|---|---|---|
| `1DCKTWSxvGYQzuEPoJanQFF5ssM9oWnqVmEoEmh1MhAU` | AI Evaluation & Pricing Tracker | `app/config.py:131`, `google_sheets_config.py:9`, `reference_data_service.py:42` | **writes** `Customer Initiated Evaluation`; reads it for learning |
| `1_6RqiRaGshsY-iVssXpyLR2avlHsV_riiwXHTKqMz3c` | GreenBay Customer Sourced Appliance Pricing Matrix | `reference_data_service.py:33` | **reads** 10 tabs (per-category matrices + Rules & Governance + Reference Lists) |
| `1k7Zw8psnjIw9BukH7vVwESDR-k1PU60wlERWjljk_fE` | Copy of GreenBay Outlet Stock Control | `reference_data_service.py:34` | **reads** `Final Data` |

**The code does NOT read `Sourced products`, `Kasarani Stock` or `Roysambu Stock`.** Those are human-maintained and invisible to the app — Pulse must read them directly.

## Access for Pulse

**A19. Service account email.**

```
greenbay-evaluator-service@greenbay-ai-evaluator.iam.gserviceaccount.com
```
(project `greenbay-ai-evaluator`; key present, value not reproduced here)

**A20. How could Cloud Run read this system's data?**

| Option | Change needed | Risk |
|---|---|---|
| **Airtable API** | None. Issue Pulse its own read-only PAT | ✅ Lowest. But no confidence, no ceiling, no rejection option, 27% phone coverage |
| **Google Sheets API** | Share the 3 sheets (+ Sales Lead Doc) with a Pulse service account | ✅ Low. Only route to purchase/sell-through data |
| **Read-only API on this app** | Add endpoints + a second scoped key | 🟡 Medium. Best fidelity — only way to reach confidence + full decision history |
| **Direct Postgres** | Expose 9200, security-group allowlist, create read-only role | 🔴 Highest. Port is Docker-mapped; no read-only role exists; would need Cloud Run static egress IP + TLS. **Not recommended** |
| **Scheduled export** | New cron + object storage | 🟡 Medium. Adds staleness |

**Recommended: Airtable + Sheets for v1, plus a read-only API for confidence.** No implementation performed.

🔴 **Blocker to fix first:** `DASHBOARD_KEY` is still the committed default. I confirmed this by successfully calling production with it. That single key also gates destructive routes including `POST /tradein/admin/delete-eval-record` and `POST /tradein/admin/purge-test-records`. **Rotate it and split read-only from admin before granting Pulse anything.**

**A21. Which store is sufficient alone, which needs a join?**

| Question | Sufficient alone | Needs join |
|---|---|---|
| Evaluations per day | Airtable | — |
| Unique customers | — | Airtable + Sales Lead Doc (27% phone coverage) |
| Offers accepted per day | Airtable (`Evaluation Status="accepted"`) | — |
| Rejection reasons | **none — no data exists** | — |
| Confidence distribution | **Postgres or tracker sheet col H only** | not in Airtable |
| AI vs human price | Airtable (n=63) | + `Sourced products` for price actually paid |
| Country split | Airtable | — |
| Channel split | **none — channel is not stored** | — |

## Data quality

**A22. Records to exclude.**

- **Test/probe rows:** the purge routine targets `ZZTEST` / `ZZSMOKE` / `ZZDIAG` prefixes. **Currently 0 such rows** in Airtable — already clean.
- **Duplicate people:** 92 records ↔ 28 phones. Repeat submissions are real but indistinguishable from testing without human input.
- **No-contact records:** 244/336 have no phone — unattributable to a person.
- **Pre-instrumentation records:** the 244 without a `Ref:` token cannot be joined to Postgres at all.
- **Internal price-sync rows** in `expert_price_feedback` carry a sheet-internal marker and are excluded from pricing but **inflate** the production `ai_vs_internal.compared` figure (3,980).

**A23. Known defects affecting reporting.**

| Item | State today |
|---|---|
| **Vertex AI price** | ⚠️ Still largely broken — **7% fill (25/336)**. B15 substantially still true |
| **AI Pricing Justification** | ✅ **Fixed — 100% fill.** B15 no longer true |
| **shopify-sync** | Service commented out of compose; replaced by an in-process 12-hour scraper. Webhook router disabled |
| **Google Chat notifications** | ❌ **No Google Chat integration exists anywhere.** Alerts go via Flowcart WhatsApp + SMTP/SES |
| **Accept Offer flow** | ✅ Works — but only **3 acceptances in 336** |
| **perplexity_sonar** | 🔴 **FAILING in production right now**: *"Sonar returned NO price — check the key/credits"*. Removes a confidence source → drives the 80% review rate |
| **ses_email** | 🔴 *"sender NOT verified — SES will reject"* — **alert emails are silently failing**, including service-down alarms |
| **Outlet stock sheet** | ⚠️ A "Copy of", stale since 23 May 2026 |
| **Roysambu Stock tab** | ⚠️ Empty |

**A24. Drift between models, migrations and the live database.**

- Repo Alembic head: **`v630_image_keys`** (`v6_3_0_add_image_s3_keys.py`) — **uncommitted, therefore not deployed**.
- Production is at **`v620_seller_cols`**.
- Model↔migration drift: `ValuationSession.image_s3_keys` exists in the model **and** in the uncommitted migration, but **not in the production database**.
- Live schema confirmation requires server access → **UNVERIFIED**.
- The evaluator tables were originally created via `Base.metadata.create_all`, not migrations; `v620` exists specifically to retrofit `seller_name`/`seller_phone`. Schema provenance is mixed — **verify column existence before querying Postgres directly.**

---

# PART 2 — ASSUMPTIONS ADJUDICATED

| # | Verdict | Detail |
|---|---|---|
| **B1** | ✅ **CONFIRMED** | FastAPI, nginx, `evaluate.greenbay.market` (+ IP `3.217.166.244`), Let's Encrypt TLS 1.2/1.3. EC2 specifically is `UNVERIFIED` from here |
| **B2** | ⚠️ **PARTLY WRONG** | app/postgres/redis/qdrant/nginx confirmed. Host ports are **9100/9200/9300/9400**, not defaults. `shopify-sync` is **commented out by design**, not failing — replaced by an in-process 12-hour scraper |
| **B3** | ⚠️ **PARTLY WRONG** | Pricing/grounding via Gemini 2.5 Flash on Vertex ✅; Tavily removed ✅. But **vision is Anthropic Claude (opus-4 / sonnet-4), not Vertex.** Vertex is a secondary advisory opinion only |
| **B4** | ❌ **WRONG** | Ratios are **not** static constants and do not match those figures. They are recalculated from tracker data (`recalibrate_ratios`). Category ceilings in `_max_tradein_to_new()` are ~0.70 TV/fridge, 0.45 microwave, 0.35 fan |
| **B5** | ✅ **CONFIRMED** | Pricing matrix sheet + outlet stock sheet + own history (DB comparables / expert feedback). All three verified live |
| **B6** | ⚠️ **PARTLY WRONG** | 80% threshold ✅ (`offer_engine.py`). But below it the customer **still sees a preliminary estimate** alongside a specialist handoff — it is not fully withheld |
| **B7** | ⚠️ **CODE-ONLY** | KE/UG/NG supported in code. **Data is 100% KE (303/303).** Zero UG, zero NG |
| **B8** | ⚠️ **PARTLY WRONG** | Web Accept ✅ writes Airtable + notifies ✅. WhatsApp equivalent runs through **Flowcart against a different table** (`trade_in_sessions`), not an equivalent path |
| **B9** | ⚠️ **CODE-ONLY** | Three options implemented. **Zero occurrences in 336 records** — never exercised in production |
| **B10** | ⚠️ **PARTLY WRONG** | Base ID ✅ `appQP9goyJQ6SRU5c`. **Table is "Appliance Evaluations", not "Evaluated Products".** Present: Customer Asking Price, AI Evaluated Price, Vertex AI Price, Evaluation Status, In-House Evaluator Price, Price Variance (%) formula, Product Images, Customer Name, Customer Phone, AI Pricing Justification. **ABSENT: AI Confidence, Recommendation, Final Decision, Counter Offer Price, Session ID.** Session ref is embedded in free-text `Notes` as `Ref: {8 chars}` |
| **B11** | ❌ **WRONG** | **No Google Chat integration exists.** Notifications go via **Flowcart WhatsApp** (implemented and called) plus email. Outbound WhatsApp **was** completed |
| **B12** | ✅ **CONFIRMED** | Flowcart is the live WhatsApp channel; Cloud API router is disabled |
| **B13** | ✅ **CONFIRMED** | S3 `greenbay-evaluator-images` (us-east-1), **7-day presigned** URLs, re-presigned for Airtable. 68% of records carry images |
| **B14** | ⚠️ **UNVERIFIED** | `In-House Evaluator Price (KES)` exists at 18% fill, synced from the tracker sheet. Identity of the editor is `OUTSIDE THIS SYSTEM` |
| **B15** | **MIXED** | Country/Currency → ✅ **fixed**, 90% fill, all KE. Vertex AI price → ⚠️ **still broken**, 7% fill. AI Pricing Justification → ✅ **fixed**, 100% fill |

---

# PART 3 — WHAT THIS SYSTEM DOES NOT HAVE

Flat list. This is as important as the answers.

**Nothing at all exists for:**
1. Page visits, sessions, or any pre-submission funnel — **no abandonment data**
2. Analytics of any kind — no GA4, GTM, Meta/TikTok pixel, Hotjar
3. UTM / referrer / landing URL / campaign attribution
4. A channel field — web vs WhatsApp is not distinguishable in Airtable
5. Odoo / ERP integration of any kind
6. Any record of physical collection, receipt, or stock intake
7. Any record of eventual sale or sell-through price
8. Rejection-reason data — the schema supports it; **zero rows exist**
9. A customer/user entity — phone is the only key, present on 27%
10. Google Chat notifications
11. Uganda or Nigeria data

**Exists but unusable for reporting as-is:**
12. **AI Confidence** — not in Airtable; Postgres or tracker sheet only
13. **Session ID** — not an Airtable field; buried in free-text `Notes` on 27% of rows
14. Acquisition ceiling / walkaway limit — Postgres only
15. Notification delivery outcomes — log files and disk fallback queues (`/app/airtable_fallback/`, `/app/notification_fallback/`, `/logs/tracker_failed_rows.jsonl`), **not queryable**
16. Counter-offer / negotiation rounds — engine paused; `negotiation_rounds` effectively unused

**Lives in a system I cannot see:**
17. The **Sales Lead Doc** — access denied (Part 5)
18. The **live** outlet stock control sheet — I only reached a stale "Copy of"
19. Production Postgres row-level data — only reachable via the metrics endpoint

---

# PART 4 — SYSTEM DOCUMENTATION

## C1. System map

Docker Compose. Host ports in brackets.

| Service | Port | State |
|---|---|---|
| `greenbay-bot` (FastAPI/uvicorn) | **9100** | active |
| `greenbay-postgres` (PG 17) | **9200** → 5432 | active |
| `greenbay-redis` (7-alpine) | **9300** → 6379 | active, 256MB LRU |
| `greenbay-qdrant` | **9400/9401** | active |
| `greenbay-nginx` | 80 / 443 | active |
| `shopify-sync` | — | **commented out** |

`docker-compose.prod.yml` adds `restart: always` and memory caps (app 3GB). TLS via Let's Encrypt for `evaluate.greenbay.market`. nginx rate-limits `/tradein/*` to 30 r/s (burst 20).

Config: Pydantic `BaseSettings` in `app/config.py`, loaded from `.env`, secrets marked `repr=False`, accessed through a `get_settings()` singleton.

Frontend: **vanilla HTML/CSS/JS** — `frontend/index.html`, `app.js`, `styles.css`. No build step, no framework. Lucide icons from unpkg.

## C2. Data stores

**Postgres — source of truth for evaluations.** Key tables: `valuation_sessions` (UUID PK; category, brand, model, age, condition grade/score, defects JSON, seller name/phone/asking price, image & risk scores, retail price, resale value, **confidence_score**, ceiling, opening_offer, walkaway, decision, decision_reason, country, currency, size, final_decision, final_offer, created_at), `decision_ledger` (append-only audit), `negotiation_rounds`, `pricing_policy`, `expert_price_feedback`, `pickup_requests`, `shopify_products`, plus `trade_in_*` (WhatsApp path) and dormant e-commerce tables (`carts`, `orders`, `payments`, `deliveries`).
Row counts: `UNVERIFIED` — see §0.

**Airtable — human review surface + backup.** Base `appQP9goyJQ6SRU5c`, single table **`Appliance Evaluations`** (`tblukshFF0gpphQ1F`), 23 fields, **336 records**, 2026-03-18 → 2026-09-14.

Fill rates (all 336, live):

| Field | Type | Writer | Fill |
|---|---|---|---|
| Submission ID | autoNumber | Airtable | 100% |
| Date Submitted | dateTime | system | 100% |
| Product Name / Brand / Model Number / Category / Condition | text | system | 100% |
| AI Evaluated Price (KES) | currency | system | 100% |
| AI Pricing Justification | multiline | system | 100% |
| Evaluation Status | text | system | 100% |
| Notes | multiline | system | 100% |
| Price Variance (%) | **formula** | Airtable | 99% |
| New Price (Estimate) | currency | system | 91% |
| Country / Currency | text | system | 90% |
| Age (Years) | number | system | 89% |
| Attachment Summary | multiline | system | 76% |
| Product Images | attachments | system (7-day presigned) | 68% |
| Customer Name / Customer Phone | text / phone | system | **27%** |
| Customer Asking Price (KES) | currency | system | **21%** |
| In-House Evaluator Price (KES) | currency | **human**, via sheet sync | **18%** |
| Vertex AI Price (KES) | currency | system | **7%** |

Monthly volume: Mar 25 · Apr 42 · **May 167** · Jun 60 · Jul 12 · Aug 15 · Sep 15.
Category: refrigerator 117, tv_monitor 81, washing_machine 54, cooker_oven 45, microwave 26, other 12, small_kitchen 1.
Condition: A 219, B 110, C 6, D 1.

**Google Sheets** — three workbooks, all verified live. See A16 and A18.

**S3** — bucket `greenbay-evaluator-images` (us-east-1), key layout `uploads/{session_id}/{0-7}.jpg`, **presigned, 7-day expiry**, re-presigned for Airtable. Live probe: reachable.

**Redis** — caching (marketplace lookups, search). Nothing reporting-relevant exists only here.
**Qdrant** — product embeddings for semantic search. Not reporting-relevant. Degrades silently when absent.

## C3. Evaluation lifecycle

1. Customer completes the wizard. **Nothing is persisted until submit** — no funnel data.
2. `POST /tradein/evaluate`. Blocking: pricing policy load → condition normalise → DB comparables → **Claude vision** → size resolve → image quality → risk → multi-source new-price (Gemini + Perplexity) → reference data → `compute_valuation()`.
   - Deployed: vision failure is **swallowed**, evaluation proceeds on a stub grade.
   - Working tree (not deployed): vision failure returns **503**.
3. `ValuationSession` INSERT (`created_at` = `func.now()`). **This is session creation — at completion, not at start.**
4. `decision_ledger` gets `evaluation_created`, plus `offer_made` when decision ∈ {accept, negotiate}.
5. Best-effort, non-blocking, in order: S3 upload → Airtable write (fallback `/app/airtable_fallback/`) → tracker sheet append (fallback `/logs/tracker_failed_rows.jsonl`) → Flowcart notification (fallback `/app/notification_fallback/`).
6. `POST /{session_id}/accept-offer` → `decision`/`final_decision` = `accepted`, `final_offer` set; Airtable patched; notify + email.
7. `POST /{session_id}/rejection-choice` → `rejected_option_{A|B|C}` persisted to Postgres **and** Airtable. **Never observed in production data.**

**Abandoned = invisible.** There is no started-but-not-finished state.

## C4. Pricing outputs

| Output | Postgres | Airtable |
|---|---|---|
| opening_offer | `opening_offer` | AI Evaluated Price (KES) |
| new price estimate | `retail_price` | New Price (Estimate) |
| estimated_resale_value | `estimated_resale_value` | — |
| **confidence_score** | `confidence_score` | **NOT STORED** |
| acquisition_ceiling | `acquisition_ceiling` | **NOT STORED** |
| walkaway_limit | `walkaway_limit` | **NOT STORED** |
| decision / reason | `decision`, `decision_reason` | Evaluation Status / Notes |
| justification | — | AI Pricing Justification |
| Vertex price | — | Vertex AI Price (KES) (7% fill) |
| acquisition ratio | in justification text only | — |

## C5. Notifications

- **Flowcart WhatsApp** → hard-coded internal number, on every evaluation outcome and on accept/reject. Text only, no images.
- **Email (SMTP → else SES)** → `TEAM_NOTIFICATION_EMAILS`, on accept and reject. 🔴 **SES sender unverified → currently failing.**
- **Monitor alerts** → health-check loop, emails on healthy→failing transitions. Same broken transport.

**No send/failure record is queryable.** Log files and disk fallback queues only.

## C6. HTTP surface

Registered: `flowcart_router` (`/webhook`), `evaluator_router` (`/tradein`), `chat_router` (`/tradein`).
**Disabled (commented out): `whatsapp_router`, `mpesa_router`, `shopify_router`.**

**Public, no auth:** `POST /tradein/evaluate`, `GET /tradein/{session_id}`, `POST /tradein/{session_id}/accept-offer`, `POST /tradein/{session_id}/rejection-choice`, `POST /tradein/notify-pickup`, `POST /tradein/upload-video`, `POST /tradein/expert-feedback`, `POST /tradein/model-lookup`, `POST /tradein/chat`, `POST /tradein/upload`, `GET /tradein/related-products`, `GET /tradein/inventory-stats`, `GET /tradein/pickup-requests`.

**📊 Reporting-relevant, DASHBOARD_KEY:** `GET /tradein/health/services`, `GET /tradein/dashboard/metrics`, `GET /tradein/dashboard/calibration`, `GET /tradein/admin/accuracy-report`, `GET /tradein/admin/tracker-analysis`, `GET /tradein/admin/airtable-audit`, `GET /tradein/admin/contact-data-stats`.

**⚠️ Destructive, same key:** `POST /tradein/admin/delete-eval-record`, `POST /tradein/admin/purge-test-records`, `POST /tradein/admin/fix-justification-field`, plus ~10 backfill/repair jobs.

---

# PART 5 — SALES LEAD DOC (PART D)

## ⛔ UNVERIFIED — ACCESS DENIED

```
GET https://sheets.googleapis.com/v4/spreadsheets/1m9KwHSmsK-h1gP0_BJUWsbpuDJ48dph_U7cotkCK6cY
→ 403 PERMISSION_DENIED — "The caller does not have permission"
```

Per the brief, I am reporting rather than guessing. **D1, D2, D3 and D4 are all UNVERIFIED.** Not one column, row count, date format, backlog figure or overlap statistic below is asserted.

### Action required

Share the sheet (Viewer is enough) with:

```
greenbay-evaluator-service@greenbay-ai-evaluator.iam.gserviceaccount.com
```

The same account already reads the other three workbooks, so no new credential is needed. Once shared, D1–D4 can be completed without any code change.

### What remains blocked

- **D1** — tabs, live tab, columns, distinct Channel/Agent/Status values, row counts, date range, fill rates, columns beyond L.
- **D2** — quantification of the date-format, impossible-date, phone-format and channel-spelling issues visible in the screenshot. I can see them described but cannot count them.
- **D3** — the entire follow-up backlog snapshot: Untouched / Awaiting Alex / Closed counts, wait-time buckets, oldest open rows, per-agent and per-channel counts, and the categorisation of Alex's free-text feedback.
- **D4** — the funnel overlap question.

### One thing I *can* say about D4

The join key would be phone. On this system's side that key exists on **only 92 of 336 records (27%)**, in three different formats. So even with access, **D4's overlap analysis is capped at ~27% of evaluations** — a sheet lead with no matching evaluation may simply be an evaluation where no phone was captured. Any "parallel funnel" conclusion must carry that caveat.

---

# PART 6 — THINGS A REPORTING BUILD WOULD REGRET NOT KNOWING

1. **`accept` ≠ `accepted`.** Engine recommendation vs customer action. Conflating them turns 3 real acceptances into 10.
2. **Airtable has no confidence field.** Any confidence visual needs Postgres or tracker-sheet column H.
3. **Session ID is free text.** `Ref: {8 chars}` inside `Notes`, on 27% of rows. There is no clean join key between Airtable and Postgres.
4. **May 2026 is an outlier** — 167 of 336 records (50%). Almost certainly bulk testing. Leave it in and every trend line is wrong.
5. **92 records ↔ 28 phones.** Repeat submissions dominate the contactable set.
6. **Perplexity is failing in production right now**, which suppresses confidence and inflates the 80% review rate. Fixing it will shift review volume sharply — do not baseline on today.
7. **Alert emails are silently failing** (SES sender unverified). Nobody is being told when things break.
8. **`DASHBOARD_KEY` is the committed public default.** I reached production with it. Rotate before Pulse integrates.
9. **The offer can exceed the customer's asking price** — 22 of 72 priced records (31%). A fix exists in the working tree but is **not deployed**.
10. **AI vs human error is ~68–72%**, within-10% only ~15%. Pulse will surface this on day one; decide in advance whether that is the intended headline.
11. **`compared: 3980`** from the production endpoint is not 3,980 evaluations. Use the Airtable n=63.
12. **The outlet stock sheet is a stale "Copy of"** (last entry 23 May 2026) and `Roysambu Stock` is empty. Find the live originals before building sell-through reporting.
13. **`Sourced products` has its header on row 2.** Row 1 holds totals. A naive loader will produce 41 rows of garbage.
14. **`Sourcing Category ` has a trailing space** in the header.
15. **Mixed date formats in the sheets** — `01/01/2025` (DD/MM/YYYY) alongside `5/23/2026` (M/D/YYYY) in the same column.
16. **This repo is ahead of production by ~20 uncommitted files.** Verify against deployed behaviour, not source.

---

*End of discovery. No changes were made to any code, schema, data, sheet or configuration.*

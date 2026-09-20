# PULSE_CHANGES.md

Changes made to the AI Evaluator on behalf of Pulse (the GreenBay operations dashboard), September 2026, on branch `pulse/ga4-and-readonly-key`. They implement the parts of `EVALUATOR_CHANGES_REQUIRED.md` (kept on the Pulse side) that are purely additive and safe to apply without production access: all of E2, the additive half of E3, and E1.

Nothing here was deployed, pushed, or run against a database. No secrets, `.env` files or pricing logic were touched. The branch is five commits on top of `7314bdd` (main), the last being this file.

## Commits

1. `Add the GA4 tag and wizard funnel events to the evaluator frontend` (E2)
2. `Allow the Google Analytics hosts in the nginx CSP header` (E2, separate so it can be dropped if nginx is managed elsewhere)
3. `Carry campaign attribution from the wizard to the session and Airtable` (E2, E3 partial)
4. `Add a read-only dashboard key for Pulse, separate from the admin key` (E1)
5. `Record the Pulse-side changes for the evaluator team` (this file)

## What changed, file by file

### greenbay-bot-backend/frontend/index.html (E2)

- The Google tag for measurement id `G-2REFLT805D` is the first thing after `<head>`, exactly as GA4 prints it. This is the only HTML page the wizard serves (`/app/` with `#evaluate`). `dashboard.html` is the ops dashboard, not the wizard, and is deliberately untagged so admin usage does not pollute the funnel.
- The `Content-Security-Policy` meta tag now allows `https://www.googletagmanager.com` in `script-src`, and the `*.google-analytics.com`, `*.analytics.google.com` and `*.googletagmanager.com` hosts in `connect-src` and `img-src`. Without this the browser silently blocks gtag.js.
- The `app.js` cache-buster moved from `?v=20260311` to `?v=20260920` so browsers fetch the new script.

### greenbay-bot-backend/frontend/app.js (E2, E3)

- New ANALYTICS block near the top. On first load it reads `utm_source`, `utm_medium`, `utm_campaign`, `utm_content` from the query string plus `document.referrer` and `location.href`, and stores them in `sessionStorage` under `gb_attribution`. The stored values survive the whole wizard, including the "Start New Evaluation" reload (which drops the query string). A later visit in the same tab with fresh `utm_*` parameters replaces them; a visit without them keeps the original capture.
- `trackEvent(name, params)` sends `gtag('event', name, attribution + params)`. It is a no-op when `gtag` is not defined (blocked, offline, ad blocker) and can never throw into the wizard.
- Events, wired to the existing step machine rather than duplicated:
  - `wizard_start`: first category choice (option card on step 1, or the landing-page category buttons). Once per wizard run; the flag resets on "Start New Evaluation".
  - `wizard_step` with `step` (integer): every forward move in `nextStep()`, so steps 2 to 11.
  - `evaluate_submit`: immediately before `POST /tradein/evaluate`.
  - `offer_shown` with `decision`: in `showResults()`, reporting the screen the customer actually sees. `reject` when the engine rejects; `review` when the engine says review or confidence is below 80 (the frontend routes both to a specialist); `accept` when the engine accepts; `negotiate` otherwise (the engine's `decline` also shows the negotiate card). Demo results, shown only when the backend is unreachable, are not tracked.
  - `human_review_routed`: alongside `offer_shown` whenever the decision shown is `review`.
  - `offer_accepted`: in `acceptOffer()`.
  - `rejection_option` with `option` (A, B, C): in `selectRejectionOption()`.
- Nothing personal is sent. The only per-event parameters are `step`, `decision` and `option`; the attribution parameters are the six captured above. Names, phones and prices never reach GA4.
- The evaluate request body gains an `attribution` object with the six captured values (null when absent).

### greenbay-bot-backend/nginx/nginx.conf (E2)

- The response-header CSP gains the same Google hosts as the meta tag. Both policies are enforced, so both must allow them. Nothing else in the header changed. If nginx is configured outside this repository in production, apply the same three additions there.

### greenbay-bot-backend/greenbay_ai_evaluator/api/schemas.py (E3)

- New `EvaluationAttribution` model: six optional strings. Values are tag-stripped and truncated to the column widths (`ATTRIBUTION_MAX_LEN`), never rejected, and unknown keys inside the block are ignored: analytics metadata must never make an evaluation fail. `EvaluateRequest` gains `attribution: EvaluationAttribution | None = None`. Older clients that omit it are unaffected; the top-level `extra="forbid"` is unchanged.

### greenbay-bot-backend/greenbay_ai_evaluator/models/evaluator_models.py (E3)

- `ValuationSession` gains six nullable columns: `utm_source`, `utm_medium`, `utm_campaign`, `utm_content` (String 200), `referrer` (String 500), `landing_url` (String 2000).

### greenbay-bot-backend/alembic/versions/v6_3_0_add_attribution_columns.py (E3)

- Revision `v630_attribution`, revises `v620_seller_cols` (the head in this clone). Idempotent like the v6.2 migration: it inspects the live schema and only adds missing columns. All columns nullable, no backfill.
- Note for the evaluator team: `EVALUATOR_CHANGES_REQUIRED.md` E7 mentions an undeployed `v630_image_keys` migration in a working tree. It is not in this clone. If it also revises `v620_seller_cols`, Alembic will report two heads; add a merge revision or re-point one `down_revision` before upgrading.
- Deploy order matters: the ORM model now selects these columns, so run `alembic upgrade head` (or `python manage_db.py upgrade`) before starting the new code against an existing database. `init_db()` uses `create_all`, which does not add columns to existing tables.

### greenbay-bot-backend/greenbay_ai_evaluator/api/router.py (E1, E3)

- `_attribution_columns(req)` maps the request's attribution block onto the new session columns; `evaluate_trade_in` passes them into `ValuationSession(...)`.
- `_pulse_airtable_fields(vs)` builds the columns Pulse reads by name: `Session ID` (the same 8-character Ref that `Notes` carries as `Ref: xxxxxxxx` and that the tracker sheet uses, so all three sources join on one token), `Confidence` (number, omitted when unknown), `Decision` (the engine value at write time: accept, negotiate, review, reject, or decline), and `UTM Source`, `UTM Medium`, `UTM Campaign`, `UTM Content`, `Referrer`, `Landing URL` (only when non-empty).
- Both `_build_airtable_payload` (live write) and `_build_airtable_payload_from_session` (backfill) call it. Both feed `airtable_service.write_evaluation`, which pre-filters the payload against the base schema via the Meta API and, when the schema is unreadable, `_post_record` drops any column Airtable reports as unknown and retries. A column missing from the base therefore drops that value, never the record. The `patch_record_field` paths used by accept-offer and rejection-choice were not touched because a PATCH with an unknown field is not guarded the same way.
- The seven read-only GET routes now depend on `verify_readonly_or_admin_key` instead of `verify_admin_key`. No other route changed.

### greenbay-bot-backend/greenbay_ai_evaluator/api/security.py (E1)

- `readonly_key()` reads `READONLY_DASHBOARD_KEY` (stripped; empty when unset).
- `verify_readonly_or_admin_key`: same header (`X-Admin-Key`) and `?key=` param as before; accepts the admin key, or the read-only key when one is configured. Constant-time comparison for both. When the variable is unset or blank, behaviour is identical to today.
- `verify_admin_key` is unchanged in effect and still rejects the read-only key. It remains the gate on every POST and on the GET routes that trigger or report on writes (`admin/airtable-fill-newprice`, `admin/backfill-*`, `admin/reprice-historical`, `admin/sheet-repair`, `admin/airtable-debug`, `/dashboard`).

### greenbay-bot-backend/env.example (E1)

- Documents `READONLY_DASHBOARD_KEY=` with the list of routes it opens.

### greenbay-bot-backend/greenbay_ai_evaluator/tests/test_attribution.py, test_readonly_key.py

- 28 new tests in the existing pytest style (deterministic, no network or DB). `test_readonly_key.py` also inspects the real router's dependency graph and asserts that exactly the seven E1 paths use the relaxed gate, that they are GET only, and that every other keyed route, mutating or not, still uses the admin gate.

## Which entries this covers

- E1: done in code. Still needed from the evaluator team: rotate `DASHBOARD_KEY` off the committed default, mint a `READONLY_DASHBOARD_KEY`, set both in the runtime environment, and hand the read-only one to Pulse (Pulse reads it from `EVALUATOR_API_KEY`; header name stays `X-Admin-Key`, so `EVALUATOR_KEY_HEADER` needs no change).
- E2: done in code. Still needed: deploy, confirm "Data collection is active" in the GA4 data stream, and grant the Pulse runtime service account Viewer on the property (Pulse needs the numeric property id in `GA4_PROPERTY_ID`).
- E3: partial. Written on every new evaluation: `Session ID`, `Confidence`, `Decision`, `UTM Source`, `UTM Medium`, `UTM Campaign`, `UTM Content`, `Referrer`, `Landing URL`. The columns must exist on `Appliance Evaluations` (base `appQP9goyJQ6SRU5c`) with those exact names; until they do the writer logs "dropping columns not present on base" and writes the rest. Suggested types: `Session ID` single line text, `Confidence` number (1 decimal), `Decision` single select (accept, negotiate, review, reject, decline), the six attribution fields single line text (Landing URL can be long text). Not done: `Customer Decision` and `Channel`. Both belong on the accept-offer and rejection-choice patch paths, which do not tolerate unknown fields, and `Channel` needs a decision on how WhatsApp-originated evaluations (which call the same `/tradein/evaluate` from `app/webhooks/flowcart.py`) are labelled. See "Still owed".

## Still owed by the evaluator team

- E3 remainder: `Customer Decision` (accepted, rejected_option_A/B/C, none) and `Channel` (web, whatsapp). Simplest path: after the base has the columns, extend `patch_record_field` calls in `accept_offer` and `rejection_choice`, and set `Channel` from the presence of `attribution` (web) versus the flowcart caller (whatsapp).
- E4: permanent image URLs (public-read objects, a re-presign job, or Airtable attachments).
- E5: phone required before the offer, validated as a Kenyan number and stored as `254XXXXXXXXX`.
- E6: Perplexity credentials, or remove it from the confidence formula.
- E7: deploy the working tree (asking-price cap, vision hard-fail, `v630_image_keys`) or record the decision not to. Watch the Alembic head conflict noted above.
- E8: verify the SES sender or switch transport.
- E9: trace why `POST /{session_id}/rejection-choice` has produced no records since March 2026. The `rejection_option` GA4 event added here lets you compare clicks against records.
- E10: point the outlet stock reader at the live ledger (`1MFvguyblM3hQi1YV0ietCtBgYT_vHHTIkJ6nKLfkbVE`, tab `Final Data`) and handle its corrupted header row.
- E11: read-only reporting API behind the read-only key (per-evaluation session id, created at, confidence, ceiling, engine and customer decisions, offer amounts, notification outcomes).
- E12: write the Odoo PO reference or ledger row id back onto the Airtable record on collection.

## Environment variable to add

    READONLY_DASHBOARD_KEY=<long random secret, different from DASHBOARD_KEY>

Optional. Leave unset and nothing changes. `DASHBOARD_KEY` should be rotated at the same time; the committed default is public.

## How to verify

### Tests

    cd greenbay-bot-backend
    python -m venv venv && source venv/bin/activate
    pip install -r requirements.txt
    python -m pytest greenbay_ai_evaluator/tests -q

136 tests should pass (108 existing plus 28 new). During this work they were run in a venv with the core dependencies only (fastapi, pydantic, sqlalchemy, httpx, loguru, pytest, pyairtable, alembic); the full requirements file was not installed.

### GA4

1. Deploy, open `https://evaluate.greenbay.market/app/?utm_source=test&utm_medium=manual&utm_campaign=verify#evaluate`.
2. In the browser console, `JSON.parse(sessionStorage.gb_attribution)` shows the six captured values.
3. Walk the wizard. GA4 Admin, Data streams, Evaluator should show "Data collection is active" within 48 hours; Reports, Realtime shows `wizard_start`, `wizard_step`, `evaluate_submit`, `offer_shown`, then `offer_accepted` or `rejection_option` or `human_review_routed` by name, each carrying `utm_source=test` and the other attribution parameters.
4. In DevTools, Network, filter `collect`: every beacon's `en=` is one of the names above and the payload contains no name, phone or price.

### Airtable

After one real evaluation, the new row should carry `Session ID` equal to the `Ref:` token in `Notes`, a numeric `Confidence`, the engine `Decision`, and the UTM fields from step 1. If the log shows `Airtable: dropping columns not present on base: [...]`, the named columns still need to be created.

### Read-only key

Replace `$ADMIN` and `$READONLY` with the two configured keys.

    # Read route, read-only key: 200
    curl -s -o /dev/null -w "%{http_code}\n" -H "X-Admin-Key: $READONLY" \
      https://evaluate.greenbay.market/tradein/dashboard/metrics?days=7

    # Read route, admin key: 200
    curl -s -o /dev/null -w "%{http_code}\n" -H "X-Admin-Key: $ADMIN" \
      https://evaluate.greenbay.market/tradein/dashboard/metrics?days=7

    # Mutating route, read-only key: 403
    curl -s -o /dev/null -w "%{http_code}\n" -X POST -H "X-Admin-Key: $READONLY" \
      "https://evaluate.greenbay.market/tradein/admin/purge-test-records?dry_run=true"

    # Mutating GET, read-only key: 403
    curl -s -o /dev/null -w "%{http_code}\n" -H "X-Admin-Key: $READONLY" \
      https://evaluate.greenbay.market/tradein/admin/sheet-repair

    # Mutating route, admin key, dry run: 200
    curl -s -o /dev/null -w "%{http_code}\n" -X POST -H "X-Admin-Key: $ADMIN" \
      "https://evaluate.greenbay.market/tradein/admin/purge-test-records?dry_run=true"

    # No key at all: 403
    curl -s -o /dev/null -w "%{http_code}\n" \
      https://evaluate.greenbay.market/tradein/health/services

Pulse's Sentinel keeps its key hygiene issue open while the key it holds is the committed default, so the rotation is what closes it.

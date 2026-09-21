# PULSE_CHANGES.md

Changes made to the AI Evaluator on behalf of Pulse (the GreenBay operations dashboard), September 2026, on branch `pulse/ga4-and-readonly-key`.

There are two rounds on this branch:

- Round 1 (20 Sept, five commits on top of `7314bdd`): the additive parts of `EVALUATOR_CHANGES_REQUIRED.md` (kept on the Pulse side): all of E2, the additive half of E3, and E1. Described from "Commits" down to "Read-only key".
- Round 2 (21 Sept, nine commits on top of round 1): the four critical issues on Pulse's Evaluator Sentinel, D01, D08, D10 and D03. Described in "Round 2" at the end of this file. **Read "Before you merge or deploy" there first: two of the changes need something set in the environment before they reach production.**

Nothing here was deployed, pushed, or run against a database. No secret, key value or `.env` file was read, written or printed. Round 1 touched no pricing logic. Round 2 changes no pricing maths: it changes how one price source's answer is read, what is recorded when a source is down, and adds one opt-in confidence setting that is off by default.

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
- E5: done in round 2 (D08 below).
- E6: the code side is done in round 2 (D01 below); the account check is Allan's. Note what D01 found: Perplexity was never in the confidence formula.
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

136 tests passed at the end of round 1 (108 existing plus 28 new); 394 at the end of round 2. During this work they were run in a venv with the core dependencies only (fastapi, pydantic, sqlalchemy, httpx, loguru, pytest, pyairtable, alembic); the full requirements file was not installed.

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

Pulse's Sentinel keeps its key hygiene issue open while the key it holds is the committed default, so the rotation is what closes it. Since round 2 the committed default opens nothing at all (D03 below), so expect 503, not 200, for the admin-key lines above until a private `DASHBOARD_KEY` is set.

# Round 2 (21 Sept 2026): Sentinel D01, D08, D10, D03

Pulse's Evaluator Sentinel lists four critical issues. For each: what the code actually shows, what changed, what a person still has to do, how to verify, how to roll back. Line numbers marked "at `de00e0e`" are the code as it stood at the end of round 1.

## Before you merge or deploy

1. **Set a private `DASHBOARD_KEY` first.** After the D03 commit, a `DASHBOARD_KEY` that is unset, blank, the old committed default, or the `env.example` placeholder opens nothing: every admin route, the ops dashboard's `/dashboard/*` data routes, and any client still presenting the default get HTTP 503 until a private key is set. Production is on the committed default today (that is what D03 says), so deploying without doing this locks the ops dashboard and Pulse out. The customer wizard and the public API are not affected. Two ways to set it, both without anybody handling the value in a chat or a commit:
   - GitHub, Settings, Secrets and variables, Actions: add `DASHBOARD_KEY` and `READONLY_DASHBOARD_KEY` (two different values, for example from `openssl rand -hex 32` run twice). The deploy workflow now writes both into the server `.env`, the way it already writes `PERPLEXITY_API_KEY`.
   - Or SSH to the server and edit `/opt/greenbay/greenbay-bot-backend/.env`, then `sudo docker compose up -d app`.
   - Then put the read-only value in Pulse's `EVALUATOR_API_KEY`. Hex keys are recommended: docker compose interpolates `$` in `.env` values.
   - If GitHub Actions cannot run (a billing block, or a release done by hand), the workflow's `.env` step does not run either: use the SSH route.
   - Emergency opt-out with no code change: `ALLOW_DEFAULT_DASHBOARD_KEY=true` in the server `.env` restores the old behaviour. It should not stay set.
2. **The deploy test gate needed more packages.** `.github/workflows/deploy.yml` installed only `pytest pytest-asyncio loguru pydantic`. Round 1's `test_readonly_key.py` imports `fastapi`, so the gate failed at collection and would have blocked the first deploy after a merge (reproduced in a clean venv: 1 collection error). The gate now also installs `fastapi`, `httpx`, `python-multipart`, `sqlalchemy` and `pydantic-settings`; with that list all 394 tests pass in a venv built from nothing else. The frontend parity tests run under `node`, which `ubuntu-latest` has; they skip when it is missing.
3. **Migrations:** none in round 2. Round 1's `v630_attribution` is still the single Alembic head and the deploy workflow already runs `alembic upgrade head`.
4. **`seller_phone` is now required on `POST /tradein/evaluate`.** The web wizard and the WhatsApp caller both send one. Any other client that posts without a phone gets HTTP 422.
5. **`GET /tradein/pickup-requests` now needs the admin key.** Nothing in this repository calls it. If an outside tool reads it, give that tool the admin key or revert commit `77d5148` alone.

## Commits (round 2)

1. `f056c58` Install the API test dependencies in the deploy test gate
2. `334c9ec` Say why the Perplexity price source returned no price, and read its answers (D01)
3. `fe05cb8` Name a price-source outage in the justification; opt-in confidence scaling (D01)
4. `a7cc887` Require a callable phone before the offer, stored as 2547XXXXXXXX (D08)
5. `8b6d75d` Keep keys and personal data out of GA4 URLs; add offer_declined (D10)
6. `e68c09c` Stop the service monitor spending Perplexity credits every ten minutes (D01)
7. `9807159` Refuse the committed default admin key; one gate for every ops route (D03)
8. `77d5148` Put the pickup-request list behind the admin key (D03)
9. `7bcf29c` Fix what an adversarial review of this branch found (all four)
10. This file.

Each commit passes the suite. `7bcf29c` touches files from all four issues, so to drop one issue revert `7bcf29c` first, then the issue's own commits, as listed under each issue. Before commit 9 a second agent was asked to break commits 1 to 8 (it fuzzed the phone rules with 80,000 strings, brute-forced 20,160 confidence inputs, and attacked the key guard and the price parser); what it found is fixed in commit 9 and described where it applies below.

## D01. Perplexity price source failing in production

### What the code shows

How Sonar is called (`greenbay_ai_evaluator/services/market_price_service.py`, at `de00e0e`): `POST https://api.perplexity.ai/chat/completions` (line 379), model `"sonar"` hard-coded (385), one user message asking for a JSON object, `temperature` 0.1, `max_tokens` 500, timeout 45 s (390), no retry. The answer is read from `choices[0].message.content` (396) with `_extract_json_from_text`, the price with `float(str(price).replace(",", ""))` (401), then a per-category sanity band.

Four findings, in order of how likely they explain the outage.

1. **Most likely: the prepaid Perplexity balance ran out, and monitoring is what spent it.** `monitor_service._check_once` (`monitor_service.py:52`) calls `run_live_healthcheck()` every `MONITOR_INTERVAL_MIN`, default 10 (`:38`), started from `app/main.py:268`. Two of the health probes are real, billed web searches. Worse, the Gemini probe called `search_internet_price` (`live_healthcheck_service.py:97-103`), which calls Sonar as well. So since 13 July 2026 (commit `57d84ac`) monitoring alone made 288 Perplexity requests a day, about 18,000 by 14 September, plus one more for every Pulse poll of `/tradein/health/services`, all before a single customer was priced. Perplexity's API is prepaid; with auto top-up off the key starts failing when the balance reaches zero. The Sonar request itself has not changed since the day it was added (the only later edits, the same night, are to how citations are recorded), so if it ever priced anything in production, a code change is not what stopped it. That points at the account. It is an inference from the code, not a confirmed fact: the health report will now say which it is.
2. **The failure was undiagnosable.** Every failure path returned the same empty result: non-200 (`:392-394`), null price (`:398-400`), out of band (`:403-408`), and a bare `except` for everything else (`:429-430`). The probe could only say "Sonar returned NO price, check the key/credits" (`live_healthcheck_service.py:126-129`). Nobody could tell a dead key from a parse failure from a timeout.
3. **Real parsing defects.** `float("KES 42,999")` raises, so a quoted price with a currency word was thrown away as "failed". A `[1]` citation mark or a thousands separator inside the JSON (`"new_price": 42,999`) made the JSON invalid and the answer was dropped.
4. **The prompt worked against itself when the size was unknown.** It said "Size/capacity: unknown" and, in the same breath, "The size/capacity MUST match exactly", then offered `{"new_price": null}` as the way out. The health probe sent no size at all (`:118-120`), so the probe itself was the case most likely to get a null.

The model name `sonar` and the request and response shapes match Perplexity's API as far as can be checked from here without calling it. If the model had been retired the API would answer HTTP 400 or 404, which the health report now shows.

**The premise "a dead Sonar lowers confidence and sends 80% of evaluations to review" is not what the code does.** Gemini and Sonar are merged into one price by `search_internet_price` before anything is counted. `reconcile_retail_price` then counts a single source called `internet_lookup` (`offer_engine.py:458-461` at `de00e0e`) whether one provider answered or both. With Gemini alive and Sonar dead, `num_real_sources`, and so the confidence score and the routing, are exactly what they would be with both alive. (The 15-point "two sources agree" boost in `search_internet_price` only touches `internet_data.confidence`, a diagnostic that routing never reads.) What keeps evaluations under the 80% gate is the set of hard caps in `_compute_confidence` (`offer_engine.py:350-360`): a pricing-matrix match with fewer than three comparables is capped at 70, an internet-only valuation at 50. The router never sets a matrix match and a sales-stock match together (`router.py`, reference resolution: the matrix is only consulted when sales stock found nothing), so the 97 cap is unreachable, and a matrix match needs at least three comparables to get past the 70 cap at all. One quirk worth the evaluator team's attention: with a sales-stock match, zero comparables leaves the score uncapped, but one or two comparables trips the 70 cap (`elif has_matrix_match or (1 <= comparables_count <= 2)`), so a little more evidence lowers the ceiling. An average of 65 with about 80% in review is the formula working as written. Fixing Perplexity will improve the new-price estimate (two sources cross-checking each other) but will not move the review rate. If the review rate is the goal, the caps are the lever, and that is a pricing-policy decision for the evaluator team, not something to change from the Pulse side.

### What changed

`greenbay_ai_evaluator/services/market_price_service.py`
- `InternetPriceResult` gains `status`, `status_detail` and `providers`. Status tokens: `ok`, `not_configured`, `auth_failed`, `http_error`, `timeout`, `request_failed`, `bad_response`, `empty_answer`, `parse_failure`, `null_price`, `out_of_band`.
- `sonar_price_research` sets a status on every path. A non-200 reports the HTTP status, Perplexity's own error message, and a hint: 401 and 402 point at the key or the credit balance, 400 and 404 at `PERPLEXITY_MODEL`, 429 at quota. The key is redacted from anything echoed, an HTML error page is never echoed, and exception text is never logged (it can carry the Authorization header); only the exception type is.
- New pure helpers, unit-tested without a network: `_read_price_answer`, `_coerce_price`, `_price_from_loose_text`. They read `42999`, `"42,999"`, `"KES 42,999"`, `"KSh. 42,999.00"`, fenced JSON, `<think>` blocks, and JSON broken by `[1]` or by a thousands separator (only its `new_price` field, and only when the number ends the field, so `42999-45999` is refused). Ranges, negatives, lists, dicts, `inf` and `nan` are refused. `{"new_price": null}` is reported as `null_price`, an answer, not a failure, and it wins over anything said after it.
- **Prose is never read as a price.** The first version of this change did read single-price sentences, and the review broke it: "Samsung UA43T5300 KES 42,999" read as 5,300, "a similar 32 inch model is KES 18,999" priced the wrong product, and an instalment and a discount were accepted. A wrong low reading wins the Gemini cross-check (a conflict takes the lower price), so it was removed. Each of those sentences is now a test that must return no price.
- The size match is demanded only when a size is known.
- The model name comes from `PERPLEXITY_MODEL` (default `sonar`), so a retirement is a config change. The key is stripped of surrounding whitespace.
- `gemini_price_research` records the same status tokens. Its behaviour (prompt, retries, parsing, sanity band, confidence) is unchanged.
- `search_internet_price` records each provider's outcome in `providers`. `internet_lookup_outage` says whether the whole lookup was down: every configured provider failed for an infrastructure reason. A provider answering "no price for this item" is an answer, not an outage.

`greenbay_ai_evaluator/services/live_healthcheck_service.py`
- The Sonar probe's detail now leads with the cause, for example `Sonar returned NO price [http_error] HTTP 401 | <Perplexity's message> | key rejected: invalid or revoked key, or the prepaid credit balance is exhausted`. It stays under the 300 characters Pulse keeps. It never contains the key.
- Both price probes pass the product's size (43 inch), as a real evaluation does.
- The Gemini probe calls Gemini alone. Before, a live Sonar could make a dead Gemini look healthy.
- The two billed probes reuse their last result for `HEALTH_PAID_PROBE_INTERVAL_MIN` minutes, default 360 (four lookups a day each instead of 288). A failure is re-probed within 30 minutes so a top-up shows up quickly. Their entries gain `cached`, `age_s`, `checked_at`; `ok`, `detail` and `latency_ms` are unchanged and no `status` key is added, because Pulse's parser reads one. `GET /tradein/health/services?fresh=true` forces a real lookup, except that a result under a minute old is still reused, so the flag (which the read-only key can pass) cannot be looped to spend credits. The cache lock is never held during a lookup: a grounded search can run past two minutes and nginx gives `/tradein/` 120 seconds. The free probes still run every time.

`greenbay_ai_evaluator/engine/offer_engine.py`, `greenbay_ai_evaluator/api/router.py`
- The evaluate route records sources that were down (infrastructure failure, never "no data found") in `price_verification.unavailable_sources`: `internet_lookup` when every configured provider failed, `marketplace_jiji_jumia` when the scrape raised or timed out. `price_verification.internet_data.providers` carries each provider's status token. Tokens only: this dictionary is returned to the customer's browser, so HTTP statuses and provider messages stay in the logs and the key-gated health report.
- `compute_valuation` takes `unavailable_sources` and names them in the pricing justification (`SOURCE OUTAGE: ...`, which reaches Airtable's "AI Pricing Justification") and in the review reason. By default the offer and the score are exactly what they were; tests pin that.
- New setting `CONFIDENCE_RENORMALISE_UNAVAILABLE_SOURCES`, default `false`, the safer behaviour: fewer answers, lower confidence, more human review. When `true`, the price-verification tiers (4, 3, 2, 1 sources for 30, 25, 20, 12 points) are scaled to the sources that could answer, rounded up. One source down of seven changes nothing (4 x 6/7 = 3.43, which rounds up to 4). Two down lets three answers earn the top tier. The outage count is clamped to three, so the most it can add is 5 points (unclamped, six sources down would have added 18); a dead source is never counted as evidence, and every hard cap still applies. Be clear about what this is: a small, bounded honesty adjustment for the rare evaluation where two sources were down at once. It does not, and should not, move the review rate; see above for what does.

`app/config.py`: `perplexity_model`, `confidence_renormalise_unavailable_sources`. `env.example` documents both plus `HEALTH_PAID_PROBE_INTERVAL_MIN`.

### Still needed from a person (Allan)

1. Open https://www.perplexity.ai/account/api (Settings, API). Check the credit balance, whether auto top-up is on, and on the usage page whether requests run at roughly 290 a day. If the balance is zero: add credit and turn auto top-up on, or accept Gemini-only and clear the key (next point).
2. If the key was revoked or rotated: API keys, Generate. Put the new value in the GitHub secret `PERPLEXITY_API_KEY` (repository Settings, Secrets and variables, Actions) and re-run the "Deploy to AWS EC2" workflow. The workflow writes it to the server `.env`. Do not paste it anywhere else.
3. To run Gemini-only on purpose: the workflow only writes the key when the secret is non-empty, so deleting the secret is not enough. Remove the `PERPLEXITY_API_KEY=` line from `/opt/greenbay/greenbay-bot-backend/.env` and restart the app. The probe then reports `ok` with "not set, running Gemini-only", and Pulse closes D01.
4. After the deploy, read the cause instead of guessing: `curl -s -H "X-Admin-Key: $READONLY" "https://evaluate.greenbay.market/tradein/health/services?fresh=true"` and look at `services.perplexity_sonar.detail`. `[http_error] HTTP 401` or `402` is the account. `[http_error] HTTP 400` or `404` is the model name: set `PERPLEXITY_MODEL`. `[parse_failure]` means the answer format changed: the log line `Sonar price research: no price [parse_failure]` carries the first 200 characters of the answer; send that to whoever maintains the parser. `[timeout]` is the network or Perplexity.
5. Decide whether the review rate should come down. If yes, that is a change to the caps in `_compute_confidence`, to be made and back-tested by the evaluator team (`/tradein/dashboard/calibration` exists for that).

### How Pulse will show it

`service:perplexity_sonar` on the Sentinel turns ok as soon as the probe prices the test product, and until then its detail carries the cause. D01 closes on that probe. The trailing-30-day average confidence and review share in D01's evidence will not move because of this; that line of the evidence describes the caps, not Perplexity.

### Roll back

`git revert 7bcf29c e68c09c fe05cb8 334c9ec` (in that order; the later ones build on `334c9ec`). Without a revert: `HEALTH_PAID_PROBE_INTERVAL_MIN=0` restores a billed lookup on every health-check, and `CONFIDENCE_RENORMALISE_UNAVAILABLE_SOURCES` is already off.

## D08. Phone captured on 27% of evaluations

### What the code shows

- The wizard has required a name and a phone at step 9 since 3 June 2026 (commit `963f054`), so most of the 27% is older records and sessions from before that deploy. The contact step comes before the photos and the offer, which is the right place.
- The API never required it: `seller_phone: str | None = Field(None, ...)` (`schemas.py:143` at `de00e0e`), and the validator only stripped characters (`:169-178`). Any client, a cached older `app.js`, or a direct call could create an evaluation and an offer with no phone.
- The wizard's rule was loose: any 9 to 15 digits (`app.js:459-465`). `0212345678` and `0000000000` passed.
- Nothing was normalised: `seller_phone=req.seller_phone` (`router.py:3005`) stored whatever was typed. That is Pulse's "three formats, resolving to 28 numbers".
- A trap worth knowing: on any non-OK response `callEvaluationAPI` falls through to `generateDemoResults` and shows a demo offer (`app.js:1081`). So a backend that rejects a phone the wizard accepted does not show an error; it shows a customer a made-up price. The two validators have to agree exactly.

### What changed

`greenbay_ai_evaluator/api/schemas.py`
- `normalise_phone(raw, country)`. Kenyan numbers are accepted as `0712345678`, `0112345678`, `712345678`, `+254712345678`, `254712345678`, `00254712345678`, and the common `+254 0712 345 678`, with spaces, dashes, dots or parentheses anywhere, and stored as `254712345678`. Only `07` and `01` ranges, exactly nine digits after the prefix. Uganda (`256`) and Nigeria (`234`) work the same way; a number written locally is read with the request's `country`, which defaults to Kenya. Other countries are accepted in international form: `+`, `00`, or eleven or more digits with no leading zero, which is how WhatsApp sends a sender id. Stored digits only, at most 15, which fits the `String(20)` column. No migration.
- `EvaluateRequest.seller_phone` is required. Missing, null, blank or invalid is HTTP 422 carrying the same sentence the wizard shows inline.

`frontend/app.js`, `frontend/index.html`
- `normalisePhone` mirrors the backend rule for rule; `isValidPhone` uses it, so the inline message and the disabled Next button follow the real rule. The message, the label and the confidentiality line under the field are unchanged.
- Before submitting, the wizard checks the contact step again. A session restored from `localStorage` can hold a phone saved under the old loose rule; it is sent back to step 9 with the message showing.
- A 422 that mentions the phone returns the customer to step 9 instead of falling through to the demo offer.
- The wizard sends what the customer typed; the backend normalises, so there is one source of truth for the stored form.
- Both sides share two more rules found in review: at most 30 characters as typed (the input now has `maxlength="30"`; before, a customer who typed a long dashed number could loop on step 9), and a pasted U+FEFF is dropped (JavaScript's `trim()` removes it, Python's `strip()` does not). The 422 handler looks at the error's `loc` and `msg`, never at the echoed input.
- `app.js` cache-buster moved to `?v=20260921`.

Existing records are untouched: old rows keep their old spellings, the admin views and the ops dashboard show `seller_phone` as stored. `PickupNotifyRequest` was left alone on purpose: it has no country field, and rejecting a pickup is worse than storing its phone as typed.

Tests (`test_seller_phone.py`, 85): the case table, the request model, the 422 body, the deploy smoke-test payload, the WhatsApp phone shapes, and a parity test that runs the real `normalisePhone` from `app.js` under node against the same table. Outside the suite, 60,000 random strings were run through both implementations after the last change: no disagreement, and nothing the wizard accepts is refused by the request model.

### Still needed from a person

- Nothing to configure. After deploy, do one evaluation typing `0712 345 678` and confirm Airtable's `Customer Phone` reads `254712345678`.
- Optional, evaluator team: a one-off normalisation of the 92 existing Airtable phones. `normalise_phone` can be imported by a script; it was not run here because it writes to production data.
- Known and left alone, for the evaluator team to check against real Flowcart payloads: `app/webhooks/flowcart.py` sends the WhatsApp sender id as `seller_phone`. Plain digits and `+` forms work for any country. A missing sender (`"unknown"`), a prefixed id such as `whatsapp:+254...`, or a landline sender would now fail validation and that WhatsApp evaluation would not run, where before it ran with an unusable phone. If Flowcart prefixes ids, strip the prefix in `_call_evaluator` before posting.
- During the deploy, a browser tab still running the old `app.js` can submit a phone the new API refuses; the old script shows a demo offer in that case. It clears on the next page load (the cache-buster changed).

### How Pulse will show it

`airtable_fill:phone` rises towards 100% for new records, and every new value is one format, so the count of distinct numbers stops being inflated. The all-time fill rate climbs slowly because old rows stay; judge it on records created after the deploy.

### Roll back

`git revert 7bcf29c a7cc887`. Stored normalised numbers are valid phone numbers either way; nothing to undo in the data.

## D10. No analytics, UTM or referrer capture on the evaluator frontend

### What was verified in round 1's work

- The tag loads once: one loader and one `gtag('config')` in `index.html`, none in `app.js`, none in `dashboard.html`. The CSP meta tag and the nginx header both allow the Google hosts.
- Events fire at each step (`wizard_step` with `step` in `nextStep`), on the first category choice (`wizard_start`), before the API call (`evaluate_submit`), when the result screen renders (`offer_shown` with `decision`, plus `human_review_routed`), on accept (`offer_accepted`) and on the option chosen after a decline (`rejection_option`). Demo results are not tracked.
- The six attribution fields are captured on first load, survive the wizard in `sessionStorage`, are sent with `POST /tradein/evaluate`, validated by `EvaluationAttribution`, stored by `_attribution_columns` on six nullable columns, and mirrored to Airtable. Migration `v630_attribution` is idempotent and is the single Alembic head.
- No call site passes a name, a phone, a price or a photo.

### Gaps found and fixed

1. **URLs were sent and stored verbatim.** GA4 records `page_location` and `page_referrer` on every hit, and the wizard stored `document.referrer` and `location.href` as they were (`app.js:83-84` at `de00e0e`). The ops dashboard is opened as `dashboard.html?key=<admin key>`. The site's referrer policy sends the full URL on same-origin navigation, so one click from the dashboard to the wizard would have put the admin key in GA4, in `valuation_sessions.referrer` and in Airtable. A support or campaign link carrying `?phone=` or `?name=` would have done the same with personal data. Now `index.html` defines `gbCleanUrl` and passes cleaned values to `gtag('config')` before the first hit: the landing URL keeps the path, the campaign tags (`utm_*`) and the ad-click ids (`gclid`, `gbraid`, `wbraid`, `dclid`, `gad_source`, `gad_campaignid`, `fbclid`, `ttclid`, `msclkid`) and a simple `#fragment`; the referrer keeps the path only. `app.js` captures attribution through the same function and cleans an older stored capture on read. The API cleans again on the way in (`clean_attribution_url` in `schemas.py`), for cached older frontends and other clients. The allow-list lives in two places, `index.html` and `ATTRIBUTION_QUERY_ALLOWLIST`; a test fails if they differ, and another runs the browser cleaner under node against the backend cleaner.
2. **`trackEvent` merged whatever a call site passed.** It now forwards only `step`, `decision` and `option`, so a future call site cannot leak a field by accident.
3. **No event for declining.** `offer_declined` fires when the customer opens "Not Happy With Price?", once per offer. `rejection_option` still reports which of A, B, C they then pick.

Checked in a real browser against a local copy with the Google loader removed: the first `dataLayer` config carried the cleaned URL for a landing URL that had `phone=`, `key=` and `fbclid=` in it (only `utm_*` and `fbclid` survived); `trackEvent('wizard_step', {step: 3, phone: ..., name: ..., price: ...})` pushed only `step`. One stray `page_view` from `127.0.0.1` reached the GA4 property during the first check, before the loader was removed from the test copy. It can be ignored or excluded by hostname.

### Still needed from a person

- Deploy, then GA4 Admin, Data streams: "Data collection is active" within 48 hours. Realtime should list the eight event names.
- GA4 Admin, Custom definitions: register `step`, `decision`, `option` and the six attribution parameters as event-scoped custom dimensions, or they cannot be used in reports and Pulse cannot read them through the Data API.
- Grant Pulse's service account Viewer on the property and set `GA4_PROPERTY_ID` on the Pulse side (unchanged from round 1).
- A decision for marketing and whoever owns privacy: the tag runs without a consent prompt. Kenya's Data Protection Act treats analytics identifiers as personal data. If a consent banner is wanted, Consent Mode can be added in the same inline script; it was not added here because it changes what marketing sees.
- The existing `referrer` and `landing_url` values in the database and Airtable were stored before the cleaning. There are very few (round 1 is not deployed yet), so nothing to clean up unless round 1 went out separately.

### How Pulse will show it

D10 has no probe key; it is closed by hand once GA4 shows data. Pulse's funnel reads the event names above from the GA4 Data API, and campaign attribution from the `UTM *`, `Referrer` and `Landing URL` Airtable columns.

### Roll back

`git revert 7bcf29c 8b6d75d`. `test_attribution.py` in that commit replaced a `?q=` URL with a long path in one test, because a query string that is not a campaign tag is now dropped.

## D03. Dashboard key is the committed default and also gates destructive admin routes

### What the code shows

- `security.py` (at `de00e0e`): `_DEFAULT_KEY = "greenbay-admin-2026"` (line 24) was the fallback for `DASHBOARD_KEY` (`:29`), and using it only logged a warning (`:46-51`). The app started and served every admin route with it.
- `app/main.py:475-483` kept a second copy of the default, read once at import and compared with a plain `!=`, in front of `/dashboard/status`, `/dashboard/resources`, `/dashboard/recent` (customer names and phones), `/dashboard/analytics` and `/dashboard/learning-diagnostic`.
- `env.example` ships the placeholder `change-me-to-a-long-random-secret`. Anybody who copies the file without editing it has a public key too.
- Round 1's read-only key is sound. It reaches exactly seven routes, all GET, all reads: `/tradein/health/services`, `/tradein/dashboard/metrics`, `/tradein/dashboard/calibration`, `/tradein/admin/accuracy-report`, `/tradein/admin/tracker-analysis`, `/tradein/admin/airtable-audit`, `/tradein/admin/contact-data-stats`. None writes to the database, Airtable or the sheets. One caveat: `health/services` makes real calls to paid services, so the read-only key could spend money; the probe reuse added under D01 removes most of that. It is refused by all fourteen `/tradein/admin/*` POST routes (delete-eval-record, purge-test-records, the backfills, repairs, repricing, recalibration), by the GET routes that trigger or report on writes, and by the `/dashboard/*` routes in `app/main.py`.
- Found on the way: `GET /tradein/pickup-requests` had no gate at all (`router.py:3781`). It returns every seller's name, phone and pickup address; nginx proxies `/tradein/` to the internet; `?status=all` lists everything. Not called on production from here.

### What changed

`greenbay_ai_evaluator/api/security.py`
- `PUBLIC_KEY_VALUES`: the legacy default and the `env.example` placeholder. A `DASHBOARD_KEY` that is unset, blank or one of those opens nothing. `verify_admin_key` answers 503 with a sentence naming the fix (503 rather than 403 so the operator sees a configuration fault, not a wrong password). The app still starts: refusing to start would take the customer wizard down over an ops-key problem.
- `verify_readonly_or_admin_key` accepts a sound read-only key even while the admin key is unset or public, so Pulse is not cut off by the guard. A read-only key that is a committed value, or equal to the admin key, is ignored and logged.
- The refusal is logged once at ERROR and afterwards at DEBUG, so requests from the internet cannot fill the log.
- `log_key_hygiene()` prints `[OK]` or `[FAIL] Admin key` in the start-up health check (`app/main.py`). No key value is ever logged; a test asserts it.
- `ALLOW_DEFAULT_DASHBOARD_KEY=true` restores the old behaviour for local work.
- Keys are compared as bytes. `hmac.compare_digest` on a non-ASCII string raised, so a junk key was a 500.

`app/main.py`: the five `/dashboard/*` routes use the shared gate (constant-time, `?key=` or `X-Admin-Key`, admin key only). `frontend/dashboard.html` still works; it sends `?key=`.

`greenbay_ai_evaluator/api/router.py`: `/pickup-requests` requires the admin key (its own commit).

`.github/workflows/deploy.yml`: optional `DASHBOARD_KEY` and `READONLY_DASHBOARD_KEY` secrets are written to the server `.env` exactly as `PERPLEXITY_API_KEY` already is, and skipped when not set. The smoke-test clean-up no longer falls back to the committed default; when the container has no `DASHBOARD_KEY`, or the app refuses the one it has, it prints a warning with the HTTP status and the probe row it could not delete, so `ZZSMOKE` rows cannot pile up unseen.

Tests (`test_admin_key_guard.py`, 32) include the real router's dependency graph: every `/admin` POST is admin only, and a new one cannot be added without being listed in the test.

Not changed, for the evaluator team to decide:
- `POST /tradein/expert-feedback` has no gate, and what it stores feeds `expert_avg`, which anchors the price floor and ceiling guardrail. Anyone can post "expert" prices. The wizard's feedback panel uses it, so gating it needs a product decision.
- `scripts/system_audit.py:358` still falls back to the default when `DASHBOARD_KEY` is unset; it will now get 503 in that case.
- `GET /tradein/{session_id}` and `GET /tradein/expert-feedback/{session_id}` are open by design (the UUID is the secret).

### Still needed from a person

1. Create two values, for example `openssl rand -hex 32` twice, on your own machine.
2. GitHub, repository Settings, Secrets and variables, Actions, New repository secret: `DASHBOARD_KEY`, then `READONLY_DASHBOARD_KEY`.
3. Merge and deploy (or run the deploy workflow by hand). The start-up log should show `[OK]   Admin key: private DASHBOARD_KEY configured`.
4. Put the read-only value in Pulse's `EVALUATOR_API_KEY` (the header name stays `X-Admin-Key`). Give the admin value only to people who run backfills.
5. Check with the curl list under "Read-only key" above, plus: the old default on any admin route now answers 503 before step 2 and 403 after it.

### How Pulse will show it

`key:default` compares a sha256 of the key Pulse holds with the known defaults. It turns ok the moment Pulse holds the read-only key instead of the default, and D03 closes. If the deploy happens before the keys are set, Pulse shows "health endpoint did not answer" with HTTP 503; that is the guard working, and setting the keys clears it.

### Roll back

`git revert 77d5148` for the pickup list alone. `git revert 7bcf29c 9807159` for the guard. Without a revert, `ALLOW_DEFAULT_DASHBOARD_KEY=true` in the server `.env`.

## Environment variables (round 2)

| Variable | Default | Meaning |
| --- | --- | --- |
| `DASHBOARD_KEY` | none usable | Must be private. Unset or a committed value means admin routes answer 503. |
| `READONLY_DASHBOARD_KEY` | unset | Round 1. Now also ignored when it is a committed value or equals `DASHBOARD_KEY`. |
| `ALLOW_DEFAULT_DASHBOARD_KEY` | unset | `true` accepts the committed default. Local development only. |
| `PERPLEXITY_MODEL` | `sonar` | Model name sent to Perplexity. |
| `HEALTH_PAID_PROBE_INTERVAL_MIN` | `360` | How long the two billed health probes reuse a result. `0` is the old behaviour. |
| `CONFIDENCE_RENORMALISE_UNAVAILABLE_SOURCES` | `false` | Opt-in scaling of the verification tiers when sources were down. |

## Tests (round 2)

    cd greenbay-bot-backend
    python -m pytest greenbay_ai_evaluator/tests -q

Before round 2: 136 passed. After: 394 passed (258 new), 0 failed, 0 skipped with node installed; two parity tests skip without node (392 passed, 2 skipped). Run in two venvs: one with the core dependencies, one built from exactly the package list the deploy gate now installs. New files: `test_sonar_source.py` (99), `test_seller_phone.py` (85), `test_admin_key_guard.py` (32), `test_frontend_analytics.py` (29); `test_offer_engine.py` gains 10 and `test_attribution.py` 3. The full `requirements.txt` was not installed and `app.main` was not imported (it needs the chatbot stack), so the `app/main.py` change is covered by static tests of the file and by the shared gate's own tests, not by running the app.

## Things worth a second look before merging

1. The 503 guard against the default key (deploy order, above).
2. `seller_phone` required: any client other than the wizard and the WhatsApp webhook.
3. The Sonar answer reader. It no longer reads prose, and every price still has to pass the category sanity band and the Gemini cross-check. What it newly accepts, compared with `de00e0e`: a quoted price with a currency word or separators (`"KES 42,999"`), and the `new_price` field of JSON broken by a thousands separator or a citation mark. To go back to strict JSON only, make `_price_from_loose_text` return `None`.
4. The Gemini health probe now calls Gemini alone and both price probes are cached for six hours. The monitor's failure emails for those two services can therefore lag by up to six hours for a new failure (30 minutes for a recovery check).
5. `.github/workflows/deploy.yml` was edited in three places (test dependencies, two new optional secrets, the smoke-test clean-up). The YAML parses and the embedded script passes `bash -n`; it has not been run.
6. `frontend/index.html`'s inline Google snippet is no longer byte-for-byte what GA4 prints, because `page_location` and `page_referrer` are overridden. GA4's "tag not detected" checker sometimes wants the stock snippet; the tag itself works the same.

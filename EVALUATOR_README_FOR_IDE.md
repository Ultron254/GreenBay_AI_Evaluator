# EVALUATOR_README_FOR_IDE.md

Written 22 September 2026 for the coding assistant that works inside this repository. It is self-contained: you do not need Pulse, the earlier conversation, or any file outside this repo. Every file path is relative to `greenbay-bot-backend/` unless it starts with `.github/`. Line numbers are as of commit `bf510ea` on branch `pulse/ga4-and-readonly-key`. Where a cause is inferred rather than read from the code, the text says "inferred". The longer record of what was changed and why is `PULSE_CHANGES.md` at the repo root.

## 1. What the evaluator is, and what Pulse reads from it

The evaluator is the FastAPI app in `app/main.py` plus the static wizard in `frontend/`. A customer describes and photographs a used appliance at `https://evaluate.greenbay.market/app/#evaluate`, `POST /tradein/evaluate` prices it (`greenbay_ai_evaluator/api/router.py`, engine in `greenbay_ai_evaluator/engine/offer_engine.py`), and the result is stored in Postgres (`valuation_sessions`), mirrored to Airtable (base `appQP9goyJQ6SRU5c`, table `Appliance Evaluations`) and to a Google Sheet tracker. Pulse is GreenBay's separate operations dashboard. It monitors this app (its "Evaluator Sentinel") and reads from it with `GET` only, sending a key in the `X-Admin-Key` header (never in the URL). The routes Pulse calls, all under the prefix `/tradein` that `app/main.py:386` applies: `/tradein/health/services`, `/tradein/dashboard/metrics`, `/tradein/dashboard/calibration`, `/tradein/admin/accuracy-report`, `/tradein/admin/tracker-analysis`, `/tradein/admin/airtable-audit`, `/tradein/admin/contact-data-stats`, plus the public `/`, `/tradein/inventory-stats` and `GET /tradein/{session_id}`. Pulse also reads the Airtable table and the tracker sheet directly. Pulse holds the read-only key (`READONLY_DASHBOARD_KEY`), never the admin key. Its whole schedule is about 170 requests a day.

## 2. State of this branch

Branch `pulse/ga4-and-readonly-key`, 15 commits on top of `main` (`7314bdd`, 13 July 2026), oldest first:

1. `b02feba` Add the GA4 tag and wizard funnel events to the evaluator frontend
2. `efef5b4` Allow the Google Analytics hosts in the nginx CSP header
3. `bfa4cbb` Carry campaign attribution from the wizard to the session and Airtable
4. `d80643a` Add a read-only dashboard key for Pulse, separate from the admin key
5. `de00e0e` Record the Pulse-side changes for the evaluator team (PULSE_CHANGES.md)
6. `f056c58` Install the API test dependencies in the deploy test gate
7. `334c9ec` Say why the Perplexity price source returned no price, and read its answers
8. `fe05cb8` Name a price-source outage in the justification; opt-in confidence scaling
9. `a7cc887` Require a callable phone before the offer, stored as 2547XXXXXXXX
10. `8b6d75d` Keep keys and personal data out of GA4 URLs; add offer_declined
11. `e68c09c` Stop the service monitor spending Perplexity credits every ten minutes
12. `9807159` Refuse the committed default admin key; one gate for every ops route
13. `77d5148` Put the pickup-request list behind the admin key
14. `7bcf29c` Fix what an adversarial review of this branch found
15. `bf510ea` Record round 2 for the evaluator team

Merge and deploy order. Do these in this order.

1. Create two private values on your own machine, for example `openssl rand -hex 32` run twice. Hex only: docker compose interpolates `$` in `.env` values.
2. Put them in the GitHub repository secrets `DASHBOARD_KEY` and `READONLY_DASHBOARD_KEY`. `.github/workflows/deploy.yml` writes both into the server `.env` on the next deploy, the way it already writes `PERPLEXITY_API_KEY`.
3. If GitHub Actions cannot run (billing block, or a release done by hand), that step does not run. Instead SSH to the server, add the two lines to `/opt/greenbay/greenbay-bot-backend/.env`, and run `sudo docker compose up -d app`.
4. Merge and deploy. The startup log must show `[OK]   Admin key: private DASHBOARD_KEY configured`.
5. Hand the read-only value to whoever runs Pulse. Do not hand out the admin value except to people who run backfills.

Why the order matters. Since commit `9807159`, a `DASHBOARD_KEY` that is unset, blank, the old committed default `greenbay-admin-2026`, or the `env.example` placeholder opens nothing: every `/tradein/admin/*` route, `/tradein/health/services`, `/tradein/dashboard/*`, the five `/dashboard/*` routes in `app/main.py`, and `/tradein/pickup-requests` answer HTTP 503 with the fix in the body (`greenbay_ai_evaluator/api/security.py`, `admin_key_problem` and `_refuse_if_admin_key_is_public`). The customer wizard and `POST /tradein/evaluate` are not affected. A sound `READONLY_DASHBOARD_KEY` keeps the seven read routes open even while the admin key is bad. Emergency opt-out with no code change: `ALLOW_DEFAULT_DASHBOARD_KEY=true` in the server `.env`; do not leave it set.

Migrations. One on this branch: `alembic/versions/v6_3_0_add_attribution_columns.py` (revision `v630_attribution`, revises `v620_seller_cols`). It is the single head. It is idempotent and adds six nullable columns to `valuation_sessions`. The deploy workflow runs `alembic upgrade head` before the health gate. If a `v630_image_keys` migration exists in somebody's working tree (see D05), it must revise `v630_attribution` or be merged with a merge revision; do not leave two heads.

Tests: 394 pass (section 6).

## 3. The Sentinel issues, D01 onward

Each entry: what Pulse observes, the cause in this codebase, the status on this branch, the remaining work as steps, how to verify, and the Pulse probe key that shows it fixed. Probe keys are the identifiers Pulse's monitor attaches to a check; an entry with no probe key is closed by hand once verified.

### D01. Perplexity price source failing in production (E6)

Pulse observes: `health/services` reported `perplexity_sonar` failing with "Sonar returned NO price". Pulse first read this as the cause of the 80% review rate. That was corrected on 22 September: it is not.

Cause in code. `sonar_price_research` in `greenbay_ai_evaluator/services/market_price_service.py` posts to `https://api.perplexity.ai/chat/completions` with model `sonar`. Before this branch every failure (non-200, timeout, null price, out-of-band price, parse error) returned the same empty result, so nobody could tell a dead key from a bad answer. `float("KES 42,999")` raised. The prompt demanded an exact size match even when it said the size was unknown. Separately, `greenbay_ai_evaluator/services/monitor_service.py:52` runs the whole health-check every `MONITOR_INTERVAL_MIN` (default 10), and two of the probes are real billed web searches; the Gemini probe also called Sonar. That was 288 Perplexity requests a day from monitoring alone since 13 July. Inferred, not verified: the prepaid Perplexity balance was spent and the key started returning an error. Also verified: Perplexity is not in the confidence formula. Gemini and Sonar are merged into one `internet_lookup` source in `search_internet_price` before `reconcile_retail_price` counts sources (`offer_engine.py`, the `new_specs` list around line 505), so a dead Sonar with a live Gemini does not change the score. The review rate comes from the hard caps in `_compute_confidence` (section 5).

Status: fixed in code in `334c9ec`, `fe05cb8`, `e68c09c`, `7bcf29c`. Remaining work is the account.

Remaining work:
1. Open the Perplexity API account page. Check the credit balance, auto top-up, and daily usage. If the balance is zero, top up and turn auto top-up on, or decide to run Gemini-only.
2. If the key was rotated, set the GitHub secret `PERPLEXITY_API_KEY` and redeploy. To run Gemini-only on purpose, remove the `PERPLEXITY_API_KEY=` line from the server `.env` (deleting the secret alone is not enough, the workflow only writes non-empty secrets) and restart.
3. After deploy, `curl -s -H "X-Admin-Key: $READONLY" "https://evaluate.greenbay.market/tradein/health/services?fresh=true"` and read `services.perplexity_sonar.detail`. `[http_error] HTTP 401` or `402` is the account. `400` or `404` is the model name: set `PERPLEXITY_MODEL`. `[parse_failure]` is the answer format; the log line `Sonar price research: no price [parse_failure]` carries the first 200 characters of the answer. `[timeout]` is the network.

Verify: the detail starts with `OK` followed by `Sonar grounded price KES ...`. The tests in `greenbay_ai_evaluator/tests/test_sonar_source.py` cover the parser and probe offline.

Pulse shows it fixed: `service:perplexity_sonar` turns ok. The confidence and review figures in D01's evidence will not move; they belong to section 5.

### D02. SES sender unverified; alert and outcome emails silently fail (E8)

Pulse observes: `health/services` `ses_email` detail says "sender NOT verified, SES will reject".

Cause in code. `greenbay_ai_evaluator/services/email_service.py:196` `_send_ses` uses `SES_SENDER_EMAIL` as the From address; SES refuses an unverified sender. `_send_ses` prefers SMTP when `SMTP_HOST` is set (`:202`). Every failure is logged and swallowed by design, so nothing tells anybody. `live_healthcheck_service._probe_ses` reports the verification state.

Status: not started. Configuration, not code.

Remaining work:
1. Either verify the sender address in AWS SES (Verified identities, then click the link SES emails to that address), or set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` in the server `.env` to a Google Workspace app password and leave SES alone.
2. Confirm `TEAM_NOTIFICATION_EMAILS` lists real inboxes (`app/config.py`, default `allanmatano@greenbay.market,kelvin@greenbay.market`).
3. Restart the app. Accept one test offer (a `ZZSMOKE` record) and confirm the accepted-offer email arrives.
4. For queryable outcomes, see D14.

Verify: `health/services` `ses_email` detail contains "sender VERIFIED", or the SMTP path is in use and a test email arrives.

Pulse shows it fixed: `service:ses_email` turns ok.

### D03. Dashboard key is the committed default and also gates destructive admin routes (E1)

Pulse observes: production answered its requests with the committed default key, and the same key gates `admin/delete-eval-record` and `admin/purge-test-records`.

Cause in code (before this branch). `security.py` fell back to `greenbay-admin-2026` and only logged a warning. `app/main.py` kept a second copy of the default in front of `/dashboard/recent`, which returns customer names and phones. `GET /tradein/pickup-requests` had no gate at all and returns names, phones and addresses.

Status: fixed in code in `d80643a`, `9807159`, `77d5148`, `7bcf29c`. The read-only key opens exactly the seven routes in section 1 and is refused by all 14 `/tradein/admin/*` POST routes; `greenbay_ai_evaluator/tests/test_admin_key_guard.py` and `test_readonly_key.py` inspect the real router to enforce that. Remaining work is the rotation in section 2.

Remaining work:
1. Do steps 1 to 5 of the merge order in section 2.
2. After deploy, run: `curl -s -o /dev/null -w "%{http_code}\n" -H "X-Admin-Key: greenbay-admin-2026" https://evaluate.greenbay.market/tradein/health/services` and expect 503 before the keys are set and 403 after.
3. With the read-only key, `GET /tradein/dashboard/metrics?days=7` is 200 and `POST /tradein/admin/purge-test-records?dry_run=true` is 403.

Verify: the three curl results above, and the startup log line `[OK]   Admin key`.

Pulse shows it fixed: `key:default` turns ok once Pulse holds a key whose sha256 is not a known default.

### D04. The offer can exceed the customer's asking price (E7)

Pulse observes: 22 of 72 priced Airtable records carry an AI price above the asking price.

Cause in code. `compute_valuation` in `offer_engine.py` never caps `opening_offer` at `seller_asking_price`; the decision block at lines 934 to 965 only classifies the asking price as accept, negotiate or decline. The wizard hides this from the customer: `frontend/app.js:1240` shows the asking price when the decision is `accept`, but `accept_offer` stores `vs.final_offer = vs.opening_offer` (`router.py:3348`) and Airtable's `AI Evaluated Price (KES)` is `vs.opening_offer` (`router.py:2069`). So the customer sees one number and the records carry a higher one. Pulse's brief says an asking-price cap exists in an uncommitted working tree; it is not in this repository.

Status: not started.

Remaining work:
1. In `compute_valuation`, after STEP 11c and before the decision block, when `seller_asking_price` is not None and `0 < seller_asking_price < opening_offer`, set `opening_offer = _round_price(seller_asking_price, round_step)`, recompute `acquisition_ceiling` and `walkaway_limit` from it as the other guardrails do, append a `GUARDRAIL:` trace line, and set a new `asking_price_cap_applied` flag on `ValuationResult`.
2. Keep the `accept` decision for that case (asking is at or below the offer).
3. Add tests in `greenbay_ai_evaluator/tests/test_offer_engine.py`: offer above asking is capped to asking; offer below asking is unchanged; `None` asking is unchanged.
4. Remove the frontend substitution at `frontend/app.js:1240` once the backend caps, so the two agree.
5. Deploy, then confirm on a `ZZSMOKE` evaluation with a low asking price that `AI Evaluated Price (KES)` equals the asking price.

Verify: the new tests, and the Airtable check in step 5.

Pulse shows it fixed: `semantic:price_above_asking` trends to zero for new records.

### D05. Repository is ahead of production by about 20 uncommitted files (E7)

Pulse observes: last commit on `main` is `7314bdd` (13 July 2026) and production runs an older vision probe string; an asking-price cap, a vision hard-fail path and a `v630_image_keys` migration are said to exist uncommitted on a developer machine.

Cause in code: none of those files is in this repository. This clone was taken from the remote.

Status: not started; cannot be done from this repo alone.

Remaining work:
1. On the machine that holds the working tree, run `git status` and `git diff --stat`.
2. For each change decide: commit it on a branch, or discard it and write one line in `PULSE_CHANGES.md` saying why.
3. If `v630_image_keys` is kept, set its `down_revision` to `v630_attribution` (or add an Alembic merge revision) so `alembic heads` prints one head.
4. Run the tests, open a pull request onto this branch or onto `main` after this branch merges, deploy.

Verify: `alembic heads` prints one revision; `health/services` returns the new vision probe text.

Pulse shows it fixed: no probe key; closed by hand.

### D06. Vertex price populates on 7% of records

Pulse observes: `Vertex AI Price (KES)` is filled on 25 of 336 Airtable records.

Cause in code. `vertex_evaluate` in `greenbay_ai_evaluator/services/vertex_ai_service.py` runs only when `req.image_data` is present (`router.py:2373`), with a 45 second thread timeout (`router.py:2395`) and a 30 second HTTP timeout (`vertex_ai_service.py:171`), on the first three images. Any failure leaves `vertex_result` None and the column is written as `0.0` (`router.py:2026`, then `2071`), so an empty column and a zero look the same. Inferred, not verified: the generation config at `vertex_ai_service.py:236` sets `maxOutputTokens: 512` with no `thinkingConfig`; the model is `gemini-2.5-flash` (`app/config.py`), whose thinking tokens count against that budget. The same cause was found and fixed for the price lookup in `market_price_service.py` (see the comment above its `thinkingConfig` line: thinking "was eating the entire budget, leaving NO visible text"). The vision call never got that fix.

Status: not started.

Remaining work:
1. In `vertex_ai_service.py` `generationConfig`, add `"thinkingConfig": {"thinkingBudget": 0}` and raise `maxOutputTokens` to 1024. Mirror the self-heal in `market_price_service.py`: on a 400 whose text mentions "thinking", remove `thinkingConfig` and retry once.
2. In `_bump_counter` failures, keep the error string; expose the last error in `get_service_status` so `/dashboard/status` shows why the last call failed.
3. In `router.py` `_build_airtable_payload`, write `Vertex AI Price (KES)` only when `vertex_price > 0`, so an absent price is an empty cell, not `0.0`. Check `airtable_service.write_evaluation` tolerates the missing key (it pre-filters against the base schema, so it does).
4. Add a test that fakes the Vertex HTTP response and asserts the price is parsed, and one that asserts a None result writes no `Vertex AI Price (KES)` key.
5. Deploy and watch `EVAL_PRICE_TRACE` log lines: `vertex_secondary_opinion_kes` should be a number on most evaluations with photos.

Verify: after a week, most new Airtable records have a non-empty Vertex price.

Pulse shows it fixed: `airtable_fill:vertex_price` rises.

### D07. Confidence, session id, decision fields and channel are absent from Airtable (E3)

Pulse observes: `Appliance Evaluations` has 23 fields; `Confidence`, `Session ID`, `Decision`, `Customer Decision` and `Channel` are not among them.

Cause in code. Before this branch the payload builders in `router.py` did not send them. `_pulse_airtable_fields` (`router.py:1973`) now sends `Session ID`, `Confidence`, `Decision` and the six attribution fields on every write, but `airtable_service.write_evaluation` drops any column the base does not have and logs `Airtable: dropping columns not present on base`. `Customer Decision` and `Channel` are still not written: the accept and rejection routes patch `Evaluation Status` only (`router.py:3358` and `:3446`) through `patch_record_field`, which does not tolerate unknown fields.

Status: partly done in `bfa4cbb`. Columns must be created; two fields remain.

Remaining work:
1. In Airtable, add to `Appliance Evaluations`: `Session ID` (single line text), `Confidence` (number, 1 decimal), `Decision` (single select: accept, negotiate, review, reject, decline), `Customer Decision` (single select: accepted, rejected_option_A, rejected_option_B, rejected_option_C, none), `Channel` (single select: web, whatsapp), `UTM Source`, `UTM Medium`, `UTM Campaign`, `UTM Content`, `Referrer` (single line text), `Landing URL` (long text). Exact spellings.
2. In `router.py` `accept_offer`, after the existing `patch_record_field(session_id, "Evaluation Status", "accepted")`, add `patch_record_field(session_id, "Customer Decision", "accepted")` in its own try block. In `rejection_choice`, add the same with `f"rejected_option_{option}"`.
3. Add `Channel` to `_pulse_airtable_fields`: `"whatsapp"` when the request's `retail_price_source` is `whatsapp_category_estimate` (that is what `app/webhooks/flowcart.py:610` sends), else `"web"`. Store it on the session too if you want it in Postgres (new nullable column plus a migration that revises `v630_attribution`).
4. Extend `greenbay_ai_evaluator/tests/test_attribution.py` for `Channel` and the two patches.
5. Deploy; do one web evaluation and confirm the row carries all the fields and no `dropping columns` log line.

Verify: the log line disappears; a new row shows `Session ID` equal to the `Ref:` token in `Notes`.

Pulse shows it fixed: `airtable_schema:e3_fields` turns ok.

### D08. Phone captured on 27% of evaluations (E5)

Pulse observes: `Customer Phone` on 92 of 336 records, in three formats, resolving to 28 numbers.

Cause in code (before this branch). `EvaluateRequest.seller_phone` was optional; the wizard rule accepted any 9 to 15 digits; the value was stored as typed.

Status: fixed in code in `a7cc887` and `7bcf29c`. `normalise_phone` in `greenbay_ai_evaluator/api/schemas.py` accepts `07..`, `01..`, bare, `+254..`, `254..`, `00254..` with spaces, dashes, dots or parentheses and stores `254XXXXXXXXX`; Uganda and Nigeria the same way; the field is required and a bad value is a 422 with the sentence the wizard shows inline. `frontend/app.js` `normalisePhone` mirrors it and `greenbay_ai_evaluator/tests/test_seller_phone.py` runs the real JavaScript under node against the same case table. No migration; old rows are untouched.

Remaining work:
1. After deploy, do one evaluation typing `0712 345 678` and confirm Airtable `Customer Phone` reads `254712345678`.
2. Check a real WhatsApp payload: `app/webhooks/flowcart.py:607` sends the sender id as `seller_phone`. Plain digits and `+` forms pass. A missing sender (`"unknown"`) or a prefixed id such as `whatsapp:+254...` now fails with 422; strip such a prefix in `_call_evaluator` if Flowcart sends one.
3. Optional: a one-off script that reads the 92 existing phones and rewrites them with `normalise_phone`. Run it against a `ZZTEST` row first.

Verify: the tests, and step 1.

Pulse shows it fixed: `airtable_fill:phone` rises towards 100% on new records.

### D09. The three-option rejection flow has produced zero records since March (E9)

Pulse observes: 0 of 336 records carry a `rejected_option_A|B|C` status.

Cause in code. The wiring exists: `frontend/app.js:1393` shows "Not Happy With Price?", `showRejectionOptions` renders three cards, `selectRejectionOption` (`app.js:1627`) posts `/tradein/{session_id}/rejection-choice`, and the route patches `Evaluation Status` to `rejected_option_X` (`router.py:3446`). The button is only rendered on the offer card. The review screen (`app.js:1304`, shown when confidence is under 80 or the decision is `review`) has no offer and no rejection button, and about 80% of evaluations go there (section 5). The reject screen and the redirect screen have none either. So most customers never see the button. Not verified from code: whether the remaining 20% click it and the patch fails; nothing in the logs was read.

Status: partly. `rejection_option` and `offer_declined` GA4 events were added in `b02feba` and `8b6d75d` so clicks can be compared with records. The flow itself is unchanged.

Remaining work:
1. In production logs, grep `Airtable rejection-choice patch failed` and `rejection-choice`. If patches fail, fix the cause (usually `Evaluation Status` lacking the `rejected_option_*` options as single-select choices in Airtable; add them).
2. Decide with the product owner whether the review screen should offer the three options too. If yes, render the same cards under the specialist message and let `selectRejectionOption` run with the session id.
3. After GA4 collects a week of data, compare `offer_declined` and `rejection_option` counts with Airtable `rejected_option_*` rows.
4. Once D07 step 2 is done, `Customer Decision` will also carry the option.

Verify: a `ZZSMOKE` evaluation that reaches the offer card, click option B, confirm the Airtable status.

Pulse shows it fixed: `semantic:rejection_records` shows non-zero records.

### D10. No analytics, UTM or referrer capture on the evaluator frontend (E2)

Pulse observes (14 September): no analytics script in `frontend/index.html`; the query string was never read.

Status: fixed in code in `b02feba`, `efef5b4`, `bfa4cbb`, `8b6d75d`. The Google tag for `G-2REFLT805D` loads once in `index.html`; events `wizard_start`, `wizard_step`, `evaluate_submit`, `offer_shown`, `human_review_routed`, `offer_accepted`, `offer_declined`, `rejection_option` fire from `app.js`; `utm_*`, referrer and landing URL are captured on first load, stored on the session and sent to Airtable. URLs are cleaned before GA4 and before storage (`gbCleanUrl` in `index.html`, `clean_attribution_url` in `schemas.py`): the landing URL keeps only campaign and ad-click parameters, the referrer keeps no query string, so the dashboard's `?key=` and any `?phone=` never leave the browser. `trackEvent` forwards only `step`, `decision`, `option`. `greenbay_ai_evaluator/tests/test_frontend_analytics.py` pins all of this.

Remaining work:
1. Deploy. In GA4, Admin, Data streams, Evaluator: "Data collection is active" within 48 hours. Reports, Realtime: the eight event names while somebody walks the wizard.
2. GA4 Admin, Custom definitions: register `step`, `decision`, `option`, `utm_source`, `utm_medium`, `utm_campaign`, `utm_content`, `referrer`, `landing_url` as event-scoped custom dimensions, or reports cannot use them.
3. Grant Pulse's service account Viewer on the property and tell Pulse the numeric property id.
4. Decide with whoever owns privacy whether a consent banner is needed; Consent Mode can be added in the same inline script.

Verify: DevTools, Network, filter `collect`: every beacon's `en=` is one of the names above and no payload contains a name, phone or price.

Pulse shows it fixed: no probe key; closed by hand when GA4 shows data.

### D11. Airtable image URLs expire after 7 days (E4)

Pulse observes: `Product Images` are 7-day presigned S3 links on 68% of records.

Cause in code. `_repressign_images_for_airtable` (`router.py:1874`) presigns each S3 key for `7 * 24 * 3600` seconds and sends `{"url": ...}` dicts as `Product Images` (`router.py:2067`). If that Airtable field is an attachment field, Airtable downloads and hosts the file at write time and the presign expiry does not matter; if it is a text or URL field, the link dies after 7 days. Pulse's observation says the links die, which means the field is not an attachment field, or the writer falls back to text somewhere. Not verified here: the field type in the base.

Status: not started.

Remaining work:
1. Check the field type of `Product Images` in Airtable. If it is not an attachment field, change it to one (or add `Product Images (Attachments)` and write to that). Airtable then hosts the images and the 7-day URL only has to work at write time.
2. If the field must stay text, choose one: make the bucket objects public-read and write `https://<bucket>.s3.<region>.amazonaws.com/<key>` instead of a presigned URL; or add a daily job (`scripts/`, run from the monitor loop or cron) that re-presigns and patches records younger than 90 days.
3. Store the S3 keys on the session (this is what the uncommitted `v630_image_keys` migration in D05 is said to do) so a re-presign job has something to sign.
4. Test with a `ZZSMOKE` record, then check it after 8 days.

Verify: an image on a record older than 7 days still opens.

Pulse shows it fixed: `images:expired_share` trends to zero.

### D12. The outlet stock sheet the evaluator reads is a stale copy (E10)

Pulse observes: the sheet read for reference prices is "Copy of GreenBay Outlet Stock Control", last entry 23 May 2026; `Roysambu Stock` is empty.

Cause in code. `greenbay_ai_evaluator/services/reference_data_service.py:34` hard-codes `SALES_STOCK_SHEET_ID = "1k7Zw8psnjIw9BukH7vVwESDR-k1PU60wlERWjljk_fE"`, tab `Final Data` (`:35`), opened at `:259`. Pulse says the live ledger is `1MFvguyblM3hQi1YV0ietCtBgYT_vHHTIkJ6nKLfkbVE`, tab `Final Data`, and that its header row is corrupted (A1 reads `D0001`, D1 reads `u78`). Not verified here: neither sheet was opened.

Status: not started.

Remaining work:
1. Share the live ledger with the service account the evaluator uses (`GOOGLE_SHEETS_CREDENTIALS_FILE` or the Vertex credentials fallback).
2. Make `SALES_STOCK_SHEET_ID` a setting (`SALES_STOCK_SHEET_ID` in `app/config.py` and `env.example`, default the current id) and set it to the live id in the server `.env`, so the switch is a config change and can be reverted without a deploy.
3. In the loader at `reference_data_service.py:259` onward, read the columns by position or by a header map that tolerates the corrupted header, or ask the sheet owner to repair row 1. Log the header row once at refresh so a future change is visible.
4. Run `GET /tradein/health/services` and check `reference_cache`: sales rows cached should be non-zero after refresh, and `GET /tradein/admin/tracker-analysis` should show recent dates.

Verify: `reference_cache` detail shows rows with a last refresh after the switch.

Pulse shows it fixed: no probe key; closed by hand.

### D13. May 2026 bulk test data contaminates trends

Pulse observes: 167 of 336 records were created in May 2026 during testing. Pulse flags that month and leaves it out by default, so no evaluator change is required.

Status: not started; optional.

Remaining work:
1. If you want the base clean, tag those rows (a `Test` checkbox) or delete them with `POST /tradein/admin/purge-test-records?dry_run=true` first, then `dry_run=false`, which only touches rows with the `ZZTEST`, `ZZSMOKE`, `ZZDIAG` prefixes (`router.py:1474`). Rows without those prefixes need a hand decision.
2. Keep using those prefixes for synthetic records.

Pulse shows it fixed: no probe key.

### D14. Notification delivery outcomes are not queryable (E11)

Pulse observes: whether a customer or the team was told about an outcome lives only in logs and disk queues: `/app/airtable_fallback` (`airtable_service.py:47`), `/app/notification_fallback` (`internal_notification_service.py:25`), `logs/tracker_failed_rows.jsonl` (`reference_data_service.py:882`).

Status: not started.

Remaining work:
1. Add a table `notification_attempts` (session id, channel: email, whatsapp, airtable, sheet; attempted at; ok; error text) with an Alembic revision that revises `v630_attribution`.
2. Write a row from `email_service.send_evaluation_accepted_email`, `internal_notification_service.notify_internal_team`, `airtable_service.write_evaluation` and the tracker append, on success and on failure.
3. Add `GET /tradein/admin/evaluations?since=&limit=&cursor=` behind `verify_readonly_or_admin_key`, returning per evaluation: session id, created at, confidence, ceiling, engine decision, customer decision with timestamp, offer amounts, and the notification attempts. Add it to `READONLY_GET_PATHS` in `greenbay_ai_evaluator/tests/test_readonly_key.py` so the wiring test passes.
4. Tests for the route with an in-memory SQLite session.

Verify: the route answers with the read-only key and lists the attempts for a `ZZSMOKE` accept.

Pulse shows it fixed: no probe key; Pulse switches its confidence source to this route.

### D15. No Odoo integration and no record of physical collection or sale (E12)

Pulse observes: no `odoo`, `erp` or `xmlrpc` reference anywhere in this repository (verified: a grep finds none).

Status: not started.

Remaining work:
1. Add two Airtable fields, `Odoo PO` and `Ledger Row` (single line text).
2. Add `POST /tradein/admin/mark-collected?ref=<8-char ref>&odoo_po=<ref>` behind `verify_admin_key` that patches those fields via `patch_record_field` and records the timestamp on the session.
3. Whoever collects the item calls it, or a later Odoo webhook does.

Verify: the field appears on the record after the call.

Pulse shows it fixed: no probe key; Pulse's reconciliation becomes a join on the field.

## 4. Flagged on this branch and not changed

1. `POST /tradein/expert-feedback` (`router.py:3942`) has no key. What it stores becomes `expert_avg` (`router.py:2638`), which anchors the historical price floor and ceiling guardrail. Anyone on the internet can post "expert" prices that move offers. The wizard's own feedback panel calls it (`frontend/app.js`, `submitExpertFeedback`), so gating it with the admin key breaks that panel. Task: decide whether the panel stays; if it stays, require a short shared token or move the panel behind the ops dashboard; if it goes, add `Depends(verify_admin_key)` and remove the panel.
2. `scripts/system_audit.py:358` still falls back to `greenbay-admin-2026` when `DASHBOARD_KEY` is unset. Since `9807159` that gets a 503, which the script will report as a dead dashboard. Task: read the key from the environment only and print "DASHBOARD_KEY not set" when it is missing.

## 5. Confidence caps: a task for the pricing owner

`_compute_confidence` in `greenbay_ai_evaluator/engine/offer_engine.py`, lines 398 to 407 as of `bf510ea`:

    if has_sales_stock_match and has_matrix_match and comparables_count >= 1:
        score = min(score, 97.0)
    elif has_matrix_match and comparables_count >= 3:
        score = min(score, 85.0)
    elif has_matrix_match or (1 <= comparables_count <= 2):
        score = min(score, 70.0)

Three facts. First, the router never sets `has_sales_stock_match` and `has_matrix_match` together (the matrix is consulted only when sales stock found nothing, `router.py`, the `Priority 2` block at line 2850), so the first branch never runs. Second, a matrix match with fewer than three comparables is capped at 70, and an internet-only price at 50 (the next `elif`), both under the 80 review gate at line 928, which is why about 80% of evaluations route to review with an average near 65. Third, the quirk: with a sales-stock match and zero comparables the score is uncapped, but one or two comparables trip the third branch and cap it at 70, so a little more evidence lowers the ceiling.

Task: the pricing owner decides the intended caps, then change the branches and add cases to `tests/test_offer_engine.py` `TestConfidence`. Back-test with `GET /tradein/dashboard/calibration` before and after. Do not change this from the Pulse side.

## 6. Running the tests

1. Create a venv outside the repository, for example `python3.12 -m venv ../evalvenv`.
2. Install exactly what the deploy gate installs (`.github/workflows/deploy.yml`, job `test`): `pip install pytest pytest-asyncio loguru pydantic "fastapi>=0.115.0,<1.0" "httpx>=0.27.0,<1.0" python-multipart "sqlalchemy>=2.0.0,<3.0" "pydantic-settings>=2.7.0,<3.0"`. The full `requirements.txt` is not needed; nothing in the tests calls a network or a database.
3. From `greenbay-bot-backend/`: `python -m pytest greenbay_ai_evaluator/tests -q`. Expect 394 passed with node installed, or 392 passed and 2 skipped without it.
4. The two node tests (`test_seller_phone.py::TestFrontendParity::test_frontend_and_backend_agree_on_every_case` and `test_frontend_analytics.py::TestUrlCleanerParity::test_browser_cleaner_agrees_with_the_backend`) cut the real function out of `frontend/app.js` or `frontend/index.html` and run it with `node -e` against the Python implementation. `ubuntu-latest` has node; install it locally to run them.
5. `node --check frontend/app.js` after any frontend edit.
6. The deploy gate blocks the deploy when any test fails.

## 7. Do not

1. Do not print, log, echo, commit, or paste any key or secret value: `DASHBOARD_KEY`, `READONLY_DASHBOARD_KEY`, `PERPLEXITY_API_KEY`, AWS, Airtable, Anthropic. The tests assert that no key value is logged; keep it that way. Never echo a secret in `deploy.yml`.
2. Do not change the phone rules in one place. `normalise_phone` in `greenbay_ai_evaluator/api/schemas.py` and `normalisePhone` in `frontend/app.js` must agree exactly, because on a non-422 error the wizard shows a demo offer, and on a 422 it sends the customer back to step 9. Change both, update `PHONE_CASES` in `test_seller_phone.py`, and run the node parity test.
3. Do not send names, phones, prices, photos or raw URLs to GA4. Only `step`, `decision`, `option` and the six cleaned attribution fields may go; `trackEvent` drops anything else and `test_frontend_analytics.py` checks the call sites. Keep the query allow-list in `index.html` equal to `ATTRIBUTION_QUERY_ALLOWLIST` in `schemas.py`.
4. Do not reorder or re-point the Alembic chain. New revisions revise `v630_attribution` (or whatever `alembic heads` prints); never edit an existing revision's `down_revision`; never leave two heads.
5. Do not call `POST /tradein/admin/*` routes against production while working, except `?dry_run=true`. Synthetic records use the `ZZTEST`, `ZZSMOKE`, `ZZDIAG` prefixes so the purge routine and Pulse both skip them.
6. Do not loosen `verify_admin_key` or move a POST route to `verify_readonly_or_admin_key`; `test_admin_key_guard.py` and `test_readonly_key.py` will fail, and they are right.
7. Do not set `ALLOW_DEFAULT_DASHBOARD_KEY=true` on a server.

## 8. Glossary of Pulse terms

1. Lead: an accepted offer with a phone number that can be called. Pulse's first target is leads per day.
2. Offer shown: the customer reached the offer card (engine decision accept or negotiate, confidence 80 or more). The review, reject and redirect screens are not offers shown.
3. Accepted: the customer pressed Accept; `Evaluation Status` becomes `accepted`. Distinct from the engine decision `accept`, which means the asking price was at or below the offer. Pulse never merges the two.
4. Review: the evaluation was routed to a human specialist; in the engine, decision `review`, set when confidence is under 80 or risk is over 70.
5. Confidence: `confidence_score` from `_compute_confidence`, 0 to 100, stored on the session and, since this branch, written to Airtable as `Confidence`.
6. Probe: one check Pulse runs on a schedule and records under a key such as `service:perplexity_sonar` or `key:default`; an issue closes when its probe keys report ok.
7. Sentinel: Pulse's monitor of this app; its issue register is D01 onward, each pointing at an E-entry in Pulse's `EVALUATOR_CHANGES_REQUIRED.md`.
8. Ref: the first 8 characters of the session id, written into Airtable `Notes` as `Ref: xxxxxxxx` and, since this branch, as `Session ID`; the tracker sheet uses the same token.
9. Read-only key: `READONLY_DASHBOARD_KEY`, opens the seven GET routes in section 1 only. Admin key: `DASHBOARD_KEY`, opens everything keyed.

# Partner trade-in platforms: handover (18 Nov 2026)

GreenBay x Mika and GreenBay x Vision Plus swap pages, built on the existing evaluator engine and backend. The spec is `PARTNER_PLATFORMS_SPEC.md`.

## Where everything is
| What | Where |
|---|---|
| Evaluator work | Branch `partners/platforms` (5 commits, `bbc8ad4` to `f20f744`), worktree `/Users/allan_matano/Downloads/PROJECTS/GREENBAY_PARTNERS_WT`. Not pushed. |
| Pulse "Trade-ins" screen | Branch `partners-tradein` (commit `e9acafe`, from `release-33`), worktree `UPDATED_GREENBAY_DASHBOARD/.claude/worktrees/partners-tradein`. Not pushed. |
| Partner pages | `/mika`, `/vision-plus` (`frontend/partner.html`, `partner.css`, `partner.js`; the wizard itself is cut from `index.html` at serve time) |
| QR short links | `/q/<CODE>`: logs the scan, opens the partner page with the code |
| GreenBay ops page | `/app/partners-ops.html` (admin key, asked once per tab, sent as a header) |
| Code | `greenbay_ai_evaluator/partners/` (config, rules, ledger, service, pages, api, data_api, reports, qr, notify), migration `alembic/versions/v6_4_0_add_partner_tables.py`, tests `tests/test_partners.py` (751 tests pass in total, with main's) |

## Before it goes live (in this order)
1. **Main's portal redesign is merged in** (`8c4227a`, merged on 18 Nov). The partner hooks were re-applied in `app.js`, and the partner theme now maps onto the redesign's tokens (`--ink`, `--paper`, `--soft`, `--coal`, `--accent-ink`, `--btn-bg`, `--f-display`). `/mika`, `/vision-plus` and `/app/` were checked in the browser. If `main` moves again before deploy, merge it in again and re-run the tests.
2. **Merge to `main`.** A push to `main` deploys to production, the deploy runs `alembic upgrade head` (adds `v640_partners`), and the end-to-end check runs. No new secrets are needed: emails use the existing SMTP settings, and Pulse uses its existing read-only key.
3. **Airtable** (`Appliance Evaluations`): add `Partner`, `Partner Source`, `Partner Shop`, `QR Code`, `Buy Intent` (single line text) and `Partner Credit Offer` (number). Until they exist, those values are dropped and the rest of the row is written.
4. **One live swap**: run a ZZSMOKE Mika evaluation through `/q/MK-WEB1`, accept, book, pass and issue it on the ops page, then redeem it. Confirm the emails arrive and the settlement shows it, then cancel or purge it.
5. **Pulse**:
   - Merge `partners-tradein` into the next release and ship it with `pulse_setup/deploy.sh`.
   - On the staff page, tick "Trade-ins" for Mika: a saved module list overrides the new default.
   - Create the Vision Plus tenant and its users.
   - Decide whether the data note should mention trade-in customer data. Changing it makes every user accept it again.
6. **Give the partners their website links**: Mika gets `MK-WEB1` (banner) and `MK-WEB2` (product pages); Vision Plus gets `VP-WEB1` and `VP-WEB2`.

## Adding a partner
1. Get the partner's official logo files (a white version for the band, a dark one for light backgrounds, and a PNG of the white one for print). Put them in `frontend/partners/<slug>/`.
2. Copy `partners/configs/mika.json` to `<slug>.json` and edit:
   - slug, name, path, the two prefixes
   - countries, brands and their spellings, categories, the buy list
   - credit rules
   - placements, which are its website QR codes and must be unique across partners
   - who gets each email
   - theme: colours, fonts, logo paths
   - copy, including the sticker lines
3. Run the tests. They check that the page has no empty placeholders, that its colours pass WCAG AA (text on the band too), that its copy has no em dashes and no instant or reply-time promises, that the logo files exist, and that partners stay apart.
4. Deploy. The page is at `/<slug>`, and the partner appears on the ops page and in its read routes for Pulse.
5. In Pulse, create the tenant with the same slug and tick "Trade-ins".

## Printing QR codes
1. Open the ops page, go to "QR codes", and pick the partner.
2. Make a code: where it goes (shop sticker, website, social, flyer or poster, sales staff, event), the shop name and the placement (for example "Roysambu" and "counter"). It becomes `MK-ROY1`, and your name is saved with it.
3. Tick the codes you want, or none for all active codes. Choose "Stickers PDF" for the printer (A4, four 90 x 125 mm stickers a page) or "Stickers SVG" for a designer. "QR SVG" on a row gives one bare code for a website.
4. Each code's row shows its scans, finished evaluations, accepted swaps and credit used. To retire a code, use "Turn off"; scans of it then land on the main evaluator page.

## How a credit moves (each step is logged with who did it)
1. **Offered**: the customer finishes the partner wizard and the engine prices the item. The credit equals the offer, at least KES 500, and items 12 years old or more get a flat KES 500. If the engine routes to review, no figure is shown and the lead appears under "Needs a price".
2. **Accepted**: the customer taps Accept and sees a reference (MK-7K3Q). We send the email and the WhatsApp alert.
3. **Awaiting inspection**: on the ops page, "Book the check" (a technician for fridges, washing machines and TVs of 50 inches and up; otherwise a drop-off). This sends the "check booked" email.
4. **Inspected**: "It passed", "Passed at a lower value" (agreed with the customer, never higher), or "It failed" (cancelled).
5. **Credit live**: "Issue credit" creates the voucher code (MIKA-XXXX-XX) and the 90-day expiry. CX calls the customer with the code, and we send the "credit issued" email.
6. **Used**: when the partner tells CX a customer spent credit, CX enters it under "Record a redemption" with the partner's receipt number. It can be used in parts until it runs out. We send the "credit used" email.
7. **Settlement**: "Settlement", then partner, then month, gives the amount due to the partner (credit used), issued, lapsed and held at month end, with a CSV.
8. **Expired**: unused credit lapses after 90 days. Credit is never paid out as cash.

## Open items
- **Not yet tested together.** The live engine has never priced an item on a partner page. Previews used stubbed answers, and the tests run the real handlers offline. Step 4 above covers this.
- **Copy to confirm.** Mika's buy list includes TVs and water dispensers, and Vision Plus's includes soundbars and speakers. Partners should also sign off on their page and stickers.
- **Not tagged by partner yet.** Kay's chat messages and the Google Sheet tracker do not carry the partner fields; Postgres, Airtable, emails and the ledger do.
- **Demand board.** Trade-in buy intents show on the Pulse Trade-ins screen, but are not yet merged into Pulse's demand board.
- **Two accept emails.** On a partner accept, the team gets the existing evaluator email as well as the new partner email.
- **No expiry job.** Expiry is applied whenever credits are read; there is no nightly job.
- **Not built yet:**
  - shop lists for shop stickers (make the codes on the QR page as shops are confirmed)
  - named recipients beyond the defaults
  - SAP (the ledger has a clean record for it later)
- **Demo data.** `python scripts/seed_partner_demo.py` (SQLite only) fills demo leads, credits and QR stats. Every demo row is marked ZZTEST-DEMO, which the purge and Pulse skip.

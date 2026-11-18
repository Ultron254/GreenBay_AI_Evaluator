# GreenBay x Partner trade-in platforms: spec (draft for approval, 17 Nov 2026)

**Goal.** Mika and Vision-Plus each get a branded trade-in site running on the same evaluator engine and backend. A customer trades in an old item of that partner's brand, and once GreenBay has validated it, they receive credit they can spend only with that partner. Both partners go live end to end on **1 December 2026, Kenya only**.

## Decisions (from the question rounds)
| Topic | Decision |
|---|---|
| URL | Paths on the evaluator: `evaluate.greenbay.market/mika`, `/vision-plus`. QR short links `/q/<code>` redirect there and log the scan. No new DNS or certificates. |
| Accepted items | Partner brand only (old Mika on the Mika site, old Vision-Plus on the Vision-Plus site). Every category the engine prices that the brand makes: fridges/freezers, cookers, microwaves, washing machines, small kitchen, plus TVs for Vision-Plus. Audio and IT are out. |
| Credit value | Equal to the accepted cash offer from the unchanged engine. Floor of KES 500. Items over 12 years old get a flat KES 500. Floor and age live in partner config. |
| Credit form | Voucher code tied to the customer's phone, e.g. `MIKA-7K3Q-92`. Expires 90 days after issue. Partial use allowed, not transferable, no minimum spend, never paid out as cash. |
| Funding | GreenBay pays the partner for credit actually redeemed, per period, from the settlement report. |
| Validation | Bulky items (fridges, freezers, washers, large TVs) get a GreenBay technician visit. Small items are dropped at a GreenBay or partner shop. If the grade differs, the team settles it with the customer offline (usually a reject). The system records the outcome: validated at the agreed value (staff may enter a lower agreed value), or rejected. |
| Customer after accepting | Sees a reference only (e.g. `MK-7K3Q`) and "our team will call you". CX gives out the voucher code. No status page, no customer SMS. |
| Redemption | Partner tells GreenBay CX when credit is used. CX records the amount in the internal view. GreenBay keeps the ledger. SAP is out of scope; the ledger exposes a clean record to hook into later. |
| Internal view | New pages on the evaluator ops dashboard behind the admin key. Every action asks for the operator's name, and that name is written to the audit trail. |
| Partner view | Pulse partner portal, owner and sales roles only. Read-only: own trade-in leads, buy intents fed into the demand board, credits (code, balance, status), QR performance. Pulse reads new partner-scoped GET routes on the evaluator with its read-only key. The tenant always comes from the Pulse session. |
| QR | Admin page creates codes per partner, shop and placement, and produces SVG and PDF sticker sheets. Launch with website placements only; shops are added later from the same page. |
| Notifications | Email per event (accepted, needs validation, credit issued, credit used), configurable per partner. Default recipients: allanmatano@, alex@, info@greenbay.market. The existing Flowcart WhatsApp alert goes to the ops line for new accepted partner offers. |
| Look | Partner-led with a GreenBay co-sign: a "GreenBay x Mika" lock-up and the line "Evaluated and bought by GreenBay". Mika: its black and white wordmark, calm and geometric. Vision-Plus: its purple `#472f8f`. Font pairs are chosen per partner and shown at stage 2. Official logos come from each partner's own website (already downloaded). |
| Demo | `?demo=1` runs the full flow without the live engine or Airtable/Sheet writes. Demo records carry the `ZZDEMO` prefix. A seed script provides local sample leads, credits and QR stats. |

## How it fits the existing code
- **Partner layer (backend).** One config file per partner in `greenbay_ai_evaluator/partners/` holds: slug, name, country, brand tokens, logo files, copy, accepted brands and categories, credit rules, placements and shops, and email routing. It is served at `GET /tradein/partners/{slug}/config`. The page routes `/mika` and `/vision-plus` and the `/q/<code>` redirect are registered like `_country_portal`.
- **Attribution.** `EvaluateRequest.attribution` gains `partner_id`, `channel` and `shop` (plus a `qr_code`), the same additive way utm_* was added. The same three fields are stored on `valuation_sessions`, the Airtable row, the tracker, emails and chat. An event table counts scan, start, completion, offer, accept, validation and redemption per QR code.
- **Credit ledger.** New tables `partner_credits` and `partner_credit_events` (append-only, with actor and timestamp). States run `offered` > `accepted` > `pending_validation` > `validated` > `issued` > `partially_redeemed` / `redeemed`, with `expired` and `cancelled` reachable as exits. Every transition goes through one service function that enforces the allowed moves, the 90-day expiry, the balance check and the partner match.
- **Engine untouched.** `/tradein/evaluate` and `offer_engine.py` stay as they are. The partner layer validates the brand before pricing and applies the floor and age rule after. A partner evaluation routed to `review` shows no offer; the team can later set the agreed amount from the internal view, which creates the credit.
- **Wizard.** Same 4 steps, re-skinned, with the brand step locked to the partner. One step is added: "What would you like to buy from Mika?" (category plus an optional model). A line explains that the payout is Mika credit, not cash. Copy is plain and warm, with no em dashes and no promises of instant payouts or reply times. Mobile first, tested at 360px and 1440px, WCAG AA.
- **Migrations.** One Alembic revision on the current head.
- **Tests.** Attribution end to end, partner isolation (evaluator routes and Pulse), the ledger state machine and expiry, the floor and age rule, and brand gating. The existing 599 tests stay green.

## Stages (screenshots at 360px and 1440px after each)
1. Shared partner layer and attribution, plus the ledger schema and migration.
2. Mika front end.
3. Vision-Plus front end, through config only.
4. Credit ledger, internal ops pages, and Pulse partner screens (in the Pulse repo, release worktree).
5. QR generation, short links, sticker sheets and notifications.
6. Handover note.

Work happens on a branch. Nothing is pushed to `main` (which deploys to production) or to Pulse without your go-ahead. I'll pull first and keep clear of the country-contacts session's areas (`COUNTRY_CONFIG`, `email_service.py`, `reference_data_service.py`); notifications plug into the email service only through a new partner function.

## Still open (not blocking the build)
- Named email recipients per event, beyond the defaults above.
- Mika and Vision-Plus shop lists, for shop stickers after launch.
- Each partner's sign-off on the co-branded look.
- The SAP hook, later.

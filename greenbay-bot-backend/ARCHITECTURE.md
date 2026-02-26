## GreenBay Market Chatbot – Architecture & Developer Guide

This document is a deeper, implementation‑focused overview of the GreenBay Market WhatsApp e‑commerce chatbot.  
It complements `README.md` by explaining how the main components fit together and what to watch out for when extending or operating the system.

---

### 1. High‑Level System Overview

- **Entry point**: WhatsApp Business Cloud API webhooks hit the FastAPI app.
- **Conversation engine**: A LangGraph agent orchestrates tools to perform shopping, trade‑in, payment and fulfilment tasks.
- **Commerce data**: Product catalog comes from Shopify, is embedded and stored in Qdrant, and optionally mirrored to a Meta Product Catalog.
- **Transactional state**: Orders, payments, deliveries, users, trade‑ins, and conversations are stored in PostgreSQL.
- **Runtime glue**: Redis is used for deduplication and message processing guarantees; S3 stores images and PDF receipts; nginx optionally terminates TLS.

The stack is designed so that each responsibility is isolated:

- **Web layer** – FastAPI routers (`app/webhooks/*`, monitoring endpoints).
- **Domain services** – `app/services/*` provide reusable business logic.
- **Agent layer** – `app/agent/*` holds state machines, tools and workflows.
- **Persistence** – `app/database/*` and `alembic/` manage relational state.

---

### 2. Application Lifecycle & Configuration

#### 2.1 Lifespan and startup

`app/main.py`:

- Builds a `Settings` singleton via `get_settings()` from `app/config.py`.
- Initializes LangSmith tracing (if enabled via env).
- Calls `init_db()` from `app/database/db.py` to ensure schema availability.
- Starts the `payment_polling_service` in the background.

On shutdown, the payment polling loop is cancelled cleanly.

#### 2.2 Settings and environment

`app/config.py` (`Settings` class) aggregates all configuration:

- **OpenAI / LLM**: `OPENAI_API_KEY`, `OPENAI_MODEL`
- **WhatsApp**: token, phone number ID, verify token, catalog/app IDs
- **Qdrant**: URL, API key, collection name, embedding config, rerank options
- **M‑Pesa**: consumer key/secret, shortcode, passkey, environment, callback URL
- **Database**: `DATABASE_URL`, plus individual Postgres host/db/user/pass if needed
- **Redis**: host, port, db, password
- **AWS S3**: access key, secret, region, bucket
- **App metadata & security**: app name/version, secret key, JWT details
- **Observability**: LangSmith API key / endpoint / project

The `.env` file is loaded automatically via Pydantic settings (see `model_config` in `Settings`).  
This means **any new configuration** should be added here so that:

- It is centrally validated.
- It can be overridden via environment or `.env` without code changes.

---

### 3. Core Components

#### 3.1 Webhooks (FastAPI routers)

- `app/webhooks/whatsapp.py`
  - Verifies WhatsApp webhook (`GET /webhook/whatsapp`).
  - Handles incoming messages (`POST /webhook/whatsapp`).
  - Uses `WhatsAppService` to send messages/receipts and manage basic UX.
  - Parses inbound payloads, deduplicates by `message_id` in Redis, and forwards a unified message object to the agent (`greenbay_agent.process_message`).
  - Special handling for image uploads to support trade‑in flows (S3 upload + DB session association).

- `app/webhooks/mpesa.py`
  - Processes M‑Pesa STK push callbacks (`/webhook/mpesa/callback`) and query results.
  - Maps payment results to `Order`/`Payment` records and updates statuses.
  - Generates PDF receipts via `receipt_service`, uploads them to S3, and sends them back to the customer via WhatsApp.

- `app/webhooks/shopify.py`
  - Validates Shopify signatures (if `SHOPIFY_WEBHOOK_SECRET` is set).
  - Persists key info about:
    - Product creation (`/products/create`)
    - Product deletion (`/products/delete`)
    - Collection updates (`/collections/update`)
  - Writes a rolling log to `scripts/shopify_webhook_events.json` for debugging and replay.

All routers are registered in `create_app()` in `app/main.py`.

#### 3.2 Agent & tools

The agent lives under `app/agent/`:

- `graph.py` – defines the LangGraph graph: nodes, edges, and error handling behaviour.
- `state.py` – models the conversation/session state used across nodes.
- `nodes.py` – node implementations; each node performs one logical step (e.g. interpret intent, call a tool, decide next step).
- `compressed_prompt.py` – holds prompt engineering / system instructions to keep tokens low.
- `tools/` – functional units the agent can call:
  - `product_tools.py` – query Qdrant, select relevant products, enrich with Shopify metadata.
  - `cart_tools.py` – add/remove items, handle quantities and pricing logic.
  - `checkout_tools.py` – orchestrate delivery, installation, and M‑Pesa initiation.
  - `trade_in_tools.py` – manage trade‑in sessions, valuations, and offer lifecycle.
  - `image_tools.py` – trade‑in image validation and association with sessions.
  - `order_tools.py`, `user_tools.py`, `button_tools.py`, `common.py` – support workhorse operations.

The **agent boundary** is:

- Input: `(user_phone, message, thread_id)`
- Output: a structured response dict with:
  - `success: bool`
  - `response: str` (human‑readable message)
  - Optional metadata (e.g., which tools ran, durations).

The WhatsApp webhook then sends the `response` back to the user via `WhatsAppService`.

#### 3.3 Services

`app/services/` encapsulates reusable business logic:

- `cart_service.py` – cart model and operations (create, update, summarise).
- `order_service.py` – order creation, status transitions, total calculation.
- `delivery_service.py` – pricing and timelines based on zones/package sizes.
- `mpesa_service.py` – STK push initiation and status polling helper.
- `payment_polling_service.py` – background periodic polling of pending payments.
- `qdrant_service.py` – Qdrant client abstraction and vector search.
- `retail_price_service.py` – optional external price intelligence, via Tavily and other sources.
- `s3_service.py` – S3 uploads (images and receipts) and key generation.
- `receipt_service.py` – PDF generation with `reportlab`/`weasyprint`.
- `search_cache.py` – caching around expensive search operations.
- `conversation_log.py` – durable storage of conversation messages.

Most services receive configuration via `get_settings()` to avoid tight coupling.

#### 3.4 Persistence layer

- `app/database/models.py` – SQLAlchemy ORM models:
  - `User`, `Conversation`, `Order`, `OrderItem`, `Delivery`, `Payment`,
    `TradeInSession`, `TradeInImage`, and others for tracking trade‑in lifecycle.
- `app/database/db.py` – engine/session creation and async helpers:
  - `get_db()` / `get_db_session()` for dependency injection.
  - `init_db()` used at startup to ensure schema exists.
- `alembic/` – migration scripts for schema evolution.
  - Use `manage_db.py` to interact with Alembic safely (see below).

---

### 4. Background Jobs & Sync Services

#### 4.1 Payment polling

- Implemented in `app/services/payment_polling_service.py`.
- Started from the FastAPI lifespan in `app/main.py`.
- Periodically queries M‑Pesa for payments that were initiated but did not yet receive a callback.
- On success, updates `Payment` and `Order` statuses and triggers downstream tasks (e.g. receipt generation).

#### 4.2 Shopify product sync

Two layers:

- `scripts/sync_shopify_products.py`
  - Connects to Shopify REST API.
  - Fetches all in‑stock products (paginated).
  - Cleans HTML descriptions, builds a `searchable_text` field.
  - Generates 768‑dim embeddings via `all-mpnet-base-v2` and upserts them into Qdrant.
  - Optionally pushes variants to Meta Product Catalog when `ACCESS_TOKEN` and `CATALOG_ID` are set.

- `scripts/auto_sync_service.py`
  - Long‑running scheduler that:
    - Runs an initial sync.
    - Schedules sync every 6 hours and daily at 02:00.
    - Logs history (success/failure, duration) to `scripts/sync_history.json`.

In Docker, the `shopify-sync` service runs `auto_sync_service.py` continuously.

---

### 5. Database & Migrations

#### 5.1 Development DB options

- **SQLite (quick start / local)**:
  - Use `DATABASE_URL=sqlite:///./greenbay_chatbot.db` in `.env`.
  - Call `init_db()` (see README) to create tables from models.

- **PostgreSQL (recommended / production‑like)**:
  - Use `DATABASE_URL=postgresql://<user>:<password>@<host>/<db>` in `.env`
    or provide the individual `POSTGRES_*` variables for Docker.
  - Run migrations with Alembic via `manage_db.py`.

#### 5.2 `manage_db.py` usage

From the project root (with venv active):

- `python manage_db.py init` – initialise DB (create initial migration if none, then upgrade).
- `python manage_db.py upgrade` – apply all pending migrations.
- `python manage_db.py downgrade` – roll back one migration.
- `python manage_db.py revision "message"` – autogenerate a new migration.
- `python manage_db.py current` – show current head.
- `python manage_db.py history` – show migration history.
- `python manage_db.py reset` – **dangerous** – drop all tables and recreate them.
- `python manage_db.py status` – summarise DB URL, current revision, and table list.

When adding new models or altering fields:

1. Update `app/database/models.py`.
2. Run `python manage_db.py revision "describe-change"`.
3. Inspect the generated migration.
4. Run `python manage_db.py upgrade`.

---

### 6. Error Handling, Logging & Monitoring

#### 6.1 Logging

- Uses `loguru` across the app (`logger.info`, `logger.error`, `logger.debug`).
- In Docker, `logs/` is volume‑mounted for persistent logs from app and sync services.
- WhatsApp and M‑Pesa webhooks log raw payloads (with care not to log secrets).

Best practices when extending:

- Log:
  - External API calls and non‑200 responses.
  - All irreversible changes (e.g. trade‑in decisions, order confirmation).
  - Unexpected branches or fallback code paths.
- Avoid logging:
  - Full access tokens, passkeys, or card/banking details.

#### 6.2 Global exception handling

`app/main.py` defines a global `@app.exception_handler(Exception)` that:

- Logs the exception with `logger.error`.
- Returns a generic 500 JSON response without leaking internal details.

You can define **more specific handlers** (e.g. for `HTTPException`) at module or app level if certain error types should be surfaced differently.

#### 6.3 LangSmith monitoring

`app/monitoring/dashboard.py` exposes aggregated metrics via:

- `GET /monitoring/metrics?hours=<n>`
- `GET /monitoring/tools?hours=<n>`
- `GET /monitoring/conversations?hours=<n>`
- `GET /monitoring/health`

To enable:

- Set `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT`, and `LANGSMITH_ENDPOINT` in `.env`.
- Ensure `LANGSMITH_TRACING_V2=true` (already default in `env.example`).

---

### 7. Security & Secrets Management

Key principles followed in this repo (and to be maintained going forward):

- **No real secrets in version control**
  - `docker-compose.yml` uses placeholders (`CHANGE_ME` and `${VAR_NAME}`).
  - TLS certificates/keys (`nginx/ssl/server.crt`/`server.key`) are **not** in the repo.
  - `.env` is the single source of truth for local/dev secrets and must not be committed.

- **Where secrets live at runtime**
  - Local dev: `.env` loaded by `pydantic-settings`.
  - Docker: environment variables provided via compose (`env_file: .env` plus overrides).
  - Production: ideally from a secret manager (AWS Secrets Manager, Vault, etc.), not from flat files.

- **What to rotate if the repo was ever shared**
  - WhatsApp access token and app secret.
  - Shopify access token and webhook secret.
  - M‑Pesa consumer key/secret, shortcode, passkey.
  - AWS user access key and secret.
  - OpenAI API key and LangSmith keys.
  - Any Postgres / Redis passwords.

---

### 8. Extending the System Safely

When adding new features, keep these patterns:

- **New business capability**:
  - Add a service under `app/services/` if it has reusable logic.
  - Add one or more tools under `app/agent/tools/` to expose that service to the agent.
  - Wire tools into the graph in `app/agent/graph.py` / `nodes.py`.

- **New DB entity**:
  - Define model(s) in `app/database/models.py`.
  - Create and apply a migration via `manage_db.py`.
  - Create a dedicated service module if logic is non‑trivial.

- **New webhook or HTTP endpoint**:
  - Create a router module under `app/webhooks/` or another package.
  - Register it in `create_app()` (in `app/main.py`).
  - Add basic logging and error handling.

- **Changes to external integrations**:
  - Keep integration‑specific logic in service modules (`mpesa_service`, `qdrant_service`, etc.).
  - Do not scatter direct HTTP calls throughout webhooks or tools; call the service instead.

---

### 9. Handover Summary for New Developers

If you are inheriting this system, prioritise the following:

1. **Configuration**
   - Copy `env.example` to `.env` and fill in real values.
   - Double‑check `DATABASE_URL`, Qdrant, Redis, and external API endpoints.

2. **Environment & data**
   - Decide on dev DB (SQLite vs Postgres).
   - Run `init_db()` or Alembic migrations via `manage_db.py`.
   - Run a Shopify sync at least once to populate Qdrant.

3. **Integration wiring**
   - Configure webhooks for WhatsApp, M‑Pesa, and Shopify to hit your environment.
   - Confirm they are reaching `/webhook/whatsapp`, `/webhook/mpesa/*`, `/shopify/webhooks/*`.

4. **Security checks**
   - Ensure `.env`, `logs/`, `receipts/`, and any temp directories are excluded from version control.
   - Verify that no real certificates or keys are present in the codebase you distribute.

5. **Monitoring**
   - (Optional but recommended) set up LangSmith and scrape health endpoints to your monitoring stack.

With these in place, you should be able to operate, debug, and extend the chatbot with confidence.


## GreenBay Market WhatsApp E‑commerce Chatbot

A production‑ready WhatsApp e‑commerce chatbot for GreenBay Market built on FastAPI and LangGraph, integrating product search, cart and order management, Shopify, Qdrant, M‑Pesa, and WhatsApp Business API.

---

### 🌟 High‑level Capabilities

- **Product discovery**: Semantic product search over Shopify catalog via **Qdrant** + sentence‑transformers
- **Shopping cart & orders**: Full cart, checkout, orders, delivery and installation flows
- **M‑Pesa payments**: STK push, callbacks, polling, and receipt generation
- **WhatsApp experience**: Rich conversational UX, image support, trade‑in flows, and PDF receipts
- **Shopify sync**: Scheduled service to keep Qdrant and Meta Catalog in sync with the store
- **Monitoring & analytics**: LangSmith‑based monitoring endpoints and structured logging

---

### 🏗️ Tech Stack & Architecture

- **Backend**: FastAPI (`app/main.py`)
- **Agent**: LangGraph / LangChain agent (`app/agent/graph.py`, `nodes.py`, `state.py`, `agent/tools/*.py`)
- **Databases**:
  - **PostgreSQL** (primary, via SQLAlchemy + Alembic)
  - **Qdrant** for vector search (`app/services/qdrant_service.py`)
  - **Redis** for deduplication / caching (`whatsapp` webhook)
- **Messaging**: WhatsApp Business Cloud API (`app/webhooks/whatsapp.py`)
- **Payments**: Safaricom M‑Pesa Daraja API (`app/services/mpesa_service.py`, `app/webhooks/mpesa.py`)
- **E‑commerce backend**: Shopify webhooks + sync scripts
- **Storage**: AWS S3 for images and receipts (`app/services/s3_service.py`, `receipt_service.py`)
- **Monitoring**: LangSmith analytics (`app/monitoring/*`)

Key entrypoints:

- **FastAPI app**: `app/main.py` → `create_app()` and `lifespan` hook
- **WhatsApp webhooks**: `app/webhooks/whatsapp.py`
- **M‑Pesa webhooks**: `app/webhooks/mpesa.py`
- **Shopify webhooks**: `app/webhooks/shopify.py`
- **DB models & migrations**: `app/database/models.py`, `alembic/`
- **Shopify sync**: `scripts/auto_sync_service.py`, `scripts/sync_shopify_products.py`

---

### 📂 Project Structure (important folders)

```text
app/
  main.py              # FastAPI app, routers, monitoring, lifespan
  config.py            # Centralised settings (Pydantic BaseSettings)
  database/
    db.py              # Engine/session helpers + init_db
    models.py          # All ORM models (users, orders, trade-ins, etc.)
  agent/
    graph.py           # LangGraph graph wiring
    nodes.py           # Node implementations
    state.py           # Conversation / workflow state
    compressed_prompt.py
    tools/             # Agent tool implementations
      cart_tools.py
      checkout_tools.py
      trade_in_tools.py
      ...
  services/
    cart_service.py
    order_service.py
    delivery_service.py
    mpesa_service.py
    receipt_service.py
    qdrant_service.py
    retail_price_service.py
    s3_service.py
    search_cache.py
    payment_polling_service.py
  webhooks/
    whatsapp.py        # WhatsApp inbound + test send endpoints
    mpesa.py           # M‑Pesa callbacks & status
    shopify.py         # Shopify product/collection webhooks
  monitoring/
    dashboard.py       # LangSmith metrics surface
    evaluators.py

alembic/               # Alembic migrations
scripts/
  auto_sync_service.py     # Long‑running Shopify sync scheduler
  sync_shopify_products.py # One‑off / batch sync Shopify → Qdrant/Meta
  init-db.sql              # DB bootstrap
image_analyzer.py      # Standalone image‑analysis tool (OpenAI vision)
smolvlm_analyzer.py    # Alternative image analysis using local VLM
manage_db.py           # Alembic helper CLI
docker-compose.yml     # Full stack (app + Postgres + Redis + Qdrant + nginx + sync service)
Dockerfile             # App container
env.example            # Example environment configuration
```

---

### 📋 Prerequisites

- **Python**: 3.10+ (recommended; Dockerfile uses Python 3.13‑slim)
- **Docker & Docker Compose** (strongly recommended for a full stack)
- **Accounts / credentials**:
  - OpenAI API key (for the agent and `image_analyzer.py`)
  - WhatsApp Business Cloud API credentials
  - Safaricom M‑Pesa Daraja credentials
  - Shopify private app access token
  - AWS S3 credentials
  - Optional: LangSmith API key, Tavily API key

> **Note**: All secrets and passwords are expected to come from environment variables / `.env`.
> The `docker-compose.yml` file now uses placeholders like `CHANGE_ME` and `${VAR_NAME}` –
> you **must** provide real values in your own `.env` or compose environment before running.

---

### 🚀 Option A: Run everything with Docker (recommended)

This is the easiest way for a new developer to get the full system running (app + DB + Redis + Qdrant + Shopify sync + nginx).

1. **Clone and enter the project**

```bash
git clone <repository-url>
cd gb-bot-new-main
```

2. **Create your environment file**

```bash
cp env.example .env
```

Fill in at least:

- **OpenAI**: `OPENAI_API_KEY`
- **WhatsApp**: `WHATSAPP_API_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN`
- **M‑Pesa**: `MPESA_CONSUMER_KEY`, `MPESA_CONSUMER_SECRET`, `MPESA_SHORTCODE`, `MPESA_PASSKEY`, `MPESA_ENVIRONMENT`, `MPESA_CALLBACK_URL`
- **Shopify**: `SHOPIFY_SHOP_DOMAIN`, `SHOPIFY_ACCESS_TOKEN`, `SHOPIFY_API_VERSION`, `SHOPIFY_WEBHOOK_SECRET`
- **AWS S3**: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_S3_BUCKET`
- **LangSmith (optional)**: `LANGSMITH_API_KEY`, etc.

3. **Start the stack**

```bash
docker compose up --build
```

This will start:

- `greenbay-bot` (FastAPI app on port **8888** in container, exposed as `localhost:8888`)
- `greenbay-postgres` (PostgreSQL 17, DB = `greenbay_market`)
- `greenbay-redis` (Redis 7)
- `greenbay-qdrant` (Qdrant vector DB)
- `greenbay-nginx` (reverse proxy on ports 80/443; uses `nginx/nginx.conf`)
- `greenbay-shopify-sync` (Shopify auto sync service)

> **TLS in Docker setup**
>
> The nginx container expects a certificate and key mounted at:
> - `/etc/nginx/ssl/server.crt`
> - `/etc/nginx/ssl/server.key`
>
> The repo intentionally does **not** include real TLS files. For production, mount your own
> cert/key into `nginx/ssl/` on the host (or configure another termination layer such as a cloud load balancer).

4. **Verify health**

```bash
curl http://localhost:8888/health
curl http://localhost:8888/monitoring/health
curl http://localhost:8888/shopify/webhooks/health
```

---

### 🚀 Option B: Local Python dev environment (without Docker)

1. **Create venv & install dependencies**

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
```

2. **Environment configuration**

```bash
cp env.example .env
```

Adjust at minimum:

- Database: `DATABASE_URL` (defaults to local SQLite)
- Qdrant: `QDRANT_URL` (e.g. `http://localhost:6333` if you run Qdrant locally or via Docker)
- Redis: `REDIS_HOST`, `REDIS_PORT` if you want duplicate‑message protection to work fully

3. **Initialize the database (development)**

- **Simple SQLite dev DB**:

```bash
python -c "from app.database.db import init_db; import asyncio; asyncio.run(init_db())"
```

- **Using Postgres + Alembic (closer to production)**:

```bash
python manage_db.py init          # or: python manage_db.py upgrade
python manage_db.py status        # see current migration + tables
```

4. **Run the app**

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8888
```

The app will:

- Run startup lifespan (init DB, start payment polling loop)
- Expose:
  - `/` – root service info
  - `/health` – health check
  - `/webhook/whatsapp`, `/webhook/mpesa/*`, `/shopify/webhooks/*`

---

### 🌐 Webhook Configuration

**WhatsApp Business Cloud API**

- **Verify endpoint**:
  - URL: `GET https://<public-host>/webhook/whatsapp`
  - Query params: `hub.mode`, `hub.verify_token`, `hub.challenge`
  - `VERIFY_TOKEN` must match `WHATSAPP_VERIFY_TOKEN` in `.env`
- **Incoming messages**:
  - URL: `POST https://<public-host>/webhook/whatsapp`

**M‑Pesa Daraja**

- **Callback URL** (configured in Daraja portal):
  - `POST https://<public-host>/webhook/mpesa/callback`
- **Status query**:
  - `POST /webhook/mpesa/query-status?checkout_request_id=<id>`
- **Health**:
  - `GET /webhook/mpesa/health`

**Shopify webhooks**

Configured in Shopify admin to point to:

- `POST /shopify/webhooks/products/create`
- `POST /shopify/webhooks/products/delete`
- `POST /shopify/webhooks/collections/update`
- Shared secret must match `SHOPIFY_WEBHOOK_SECRET` in `.env`.

For local development, use a tunnelling tool (e.g. `ngrok`) to expose your FastAPI port:

```bash
ngrok http 8888
```

Then configure the ngrok URL (`https://<id>.ngrok.io/...`) in WhatsApp, M‑Pesa, and Shopify.

---

### 🧠 Conversation & Shopping Flow (simplified)

1. **User sends WhatsApp message** → `POST /webhook/whatsapp`
2. `WhatsAppService` parses the webhook, deduplicates via Redis, keeps a per‑user thread id
3. Message is logged and passed to `greenbay_agent` (LangGraph)
4. Agent calls tools for:
   - Product search via Qdrant
   - Cart & order building
   - Delivery & installation cost calculation
   - M‑Pesa STK push
   - Trade‑in flows and image validation
5. On successful payment, `mpesa` webhook triggers receipt generation and S3 upload
6. Receipt is sent back to the user as a PDF via WhatsApp.

Trade‑in images are automatically validated and attached to the relevant `TradeInSession` using tools in `app/agent/tools/image_tools.py` and `trade_in_tools.py`.

---

### 🛠️ Key API Endpoints (FastAPI)

- **Root & health**
  - `GET /` – service info
  - `GET /health` – app health

- **WhatsApp**
  - `GET /webhook/whatsapp` – verify webhook
  - `POST /webhook/whatsapp` – incoming messages
  - `POST /webhook/whatsapp/send` – send test message
  - `POST /webhook/whatsapp/send-receipt` – send a receipt PDF

- **M‑Pesa**
  - `POST /webhook/mpesa/callback` – STK callbacks
  - `POST /webhook/mpesa/query-status` – query STK push status
  - `GET  /webhook/mpesa/health` – M‑Pesa integration health

- **Shopify**
  - `POST /shopify/webhooks/products/create`
  - `POST /shopify/webhooks/products/delete`
  - `POST /shopify/webhooks/collections/update`
  - `GET  /shopify/webhooks/health`

- **Monitoring (LangSmith)**
  - `GET /monitoring/metrics?hours=24`
  - `GET /monitoring/tools?hours=24`
  - `GET /monitoring/conversations?hours=24`
  - `GET /monitoring/health`

---

### 🧪 Testing & Manual Checks

There is no dedicated `tests/` package yet, but you can manually exercise core flows:

- **WhatsApp webhook smoke test**

```bash
curl -X POST "http://localhost:8888/webhook/whatsapp" \
  -H "Content-Type: application/json" \
  -d '{"entry":[{"changes":[{"field":"messages","value":{"messages":[{"from":"254700000000","timestamp":"'$(date +%s)'","type":"text","text":{"body":"Hello"}}]}}]}]}'
```

- **M‑Pesa callback smoke test**

```bash
curl -X POST "http://localhost:8888/webhook/mpesa/callback" \
  -H "Content-Type: application/json" \
  -d '{"Body":{"stkCallback":{"ResultCode":0,"ResultDesc":"Success","CallbackMetadata":{"Item":[{"Name":"Amount","Value":500},{"Name":"MpesaReceiptNumber","Value":"TEST123"},{"Name":"PhoneNumber","Value":"254700000000"}]}}}}'
```

You can also use `pytest` for new tests (pytest and pytest‑asyncio are already in `requirements.txt`).

---

### 🧾 Shopify + Qdrant + Meta Catalog Sync

The sync is designed to be run either:

- Continuously (inside Docker) via `scripts/auto_sync_service.py`, or
- On demand via `scripts/sync_shopify_products.py`.

**Manual one‑off sync (from host, with venv):**

```bash
source venv/bin/activate
export SHOPIFY_SHOP_DOMAIN=your-store.myshopify.com
export SHOPIFY_ACCESS_TOKEN=...
export SHOPIFY_API_VERSION=2025-10
export QDRANT_URL=http://localhost:6333
python scripts/sync_shopify_products.py
```

This will:

- Fetch in‑stock products from Shopify
- Ensure Qdrant collection exists and upsert embeddings
- Optionally push products to Meta Catalog if `ACCESS_TOKEN` and `CATALOG_ID` are set.

The auto‑sync service (`scripts/auto_sync_service.py`) wraps that script and records history in `scripts/sync_history.json`.

---

### 👨‍💻 Working on the Agent & Tools

- **Add a new tool**:
  - Create a new module in `app/agent/tools/` (e.g. `inventory_tools.py`)
  - Wire it into the graph in `app/agent/graph.py` / `nodes.py`
- **Add a new service**:
  - Implement it in `app/services/` (e.g. `loyalty_service.py`)
  - Use it from agent tools or webhooks
- **Add a new DB entity**:
  - Add models in `app/database/models.py`
  - Create a new Alembic revision via `python manage_db.py revision "<message>"` and `python manage_db.py upgrade`

---

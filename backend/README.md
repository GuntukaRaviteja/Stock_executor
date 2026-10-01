# Stock Intelligence API — Phase 2

Production-structured FastAPI backend for the Stock Intelligence & Analysis Platform.  
**Phase 2 only:** Backend Foundation + MySQL Integration + Data Ingestion.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Architecture](#2-architecture)
3. [Requirements](#3-requirements)
4. [Installation](#4-installation)
5. [Environment Configuration](#5-environment-configuration)
6. [Database Setup](#6-database-setup)
7. [Alembic Migrations](#7-alembic-migrations)
8. [Running the Backend](#8-running-the-backend)
9. [Running Tests](#9-running-tests)
10. [Seeding the Database](#10-seeding-the-database)
11. [Running Ingestion](#11-running-ingestion)
12. [API Endpoints](#12-api-endpoints)
13. [Example API Requests](#13-example-api-requests)
14. [Troubleshooting](#14-troubleshooting)
15. [Phase 2 Completion Checklist](#15-phase-2-completion-checklist)

---

## 1. Project Overview

Stock Intelligence API is a RESTful backend service that:

- Connects to a MySQL database (created in Phase 1)
- Manages stock master data, historical prices, and corporate events
- Provides a market-data ingestion pipeline with mock and real providers
- Exposes a fully documented REST API (Swagger / ReDoc)
- Is structured for extensibility into Phase 3 (ML / predictions)

**Phase 3 features (ML, predictions, recommendations, frontend) have NOT been implemented.**

---

## 2. Architecture

```
backend/
├── app/
│   ├── main.py               # FastAPI application entry-point
│   ├── core/
│   │   ├── config.py         # Pydantic settings (reads .env)
│   │   ├── database.py       # SQLAlchemy engine + session + health
│   │   ├── logging.py        # Structured logger
│   │   └── exceptions.py     # Custom exception hierarchy
│   ├── models/               # SQLAlchemy ORM models
│   │   ├── stock.py          # stocks table
│   │   ├── price.py          # stock_prices table
│   │   ├── event.py          # stock_events table
│   │   ├── company.py        # companies table
│   │   └── raw_ai_data.py    # raw_ai_data table
│   ├── schemas/              # Pydantic request / response schemas
│   ├── repositories/         # Data access layer (DB queries only)
│   ├── services/             # Business logic layer
│   │   └── ingestion_service.py  # Orchestrates ingestion pipeline
│   ├── api/v1/               # FastAPI route handlers
│   └── providers/            # Pluggable market data + news providers
├── scripts/
│   ├── seed_stocks.py        # Insert seed stocks + company profiles
│   └── ingest_market_data.py # CLI ingestion runner
├── tests/                    # pytest test suite (24 tests)
├── alembic/                  # Database migration scripts
├── Makefile                  # Single-command shortcuts
├── Dockerfile                # Container image
├── docker-compose.yml        # Local container orchestration
└── requirements.txt
```

**Layer flow:**  
`API Route → Service → Repository → SQLAlchemy Model → MySQL`

---

## 3. Requirements

| Tool | Version |
|------|---------|
| Python | 3.12+ |
| MySQL | 8.0+ (from Phase 1) |
| pip | Latest |

Python packages (see `requirements.txt`):

- fastapi
- uvicorn[standard]
- sqlalchemy 2.x
- pydantic 2.x
- pydantic-settings
- alembic
- pymysql + cryptography
- python-dotenv
- httpx
- pytest + pytest-asyncio

---

## 4. Installation

> **Important:** Always use the project virtual environment. Do NOT use the system Python.

```bash
# Navigate to the backend directory
cd /Users/guntukaraviteja/project/backend

# Option A — Using Makefile (recommended)
make install

# Option B — Manual
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

The virtual environment is created at `backend/.venv/`.

---

## 5. Environment Configuration

Copy the example file and fill in your values:

```bash
cp .env.example .env
```

Edit `.env`:

```ini
APP_NAME=Stock Intelligence API
APP_ENV=development
DEBUG=true

# Your existing MySQL database from Phase 1
DATABASE_URL=mysql+pymysql://root:@127.0.0.1:3306/stock_ml_db

# Provider: mock (default, no API key needed) | alphavantage | yfinance
MARKET_DATA_PROVIDER=mock
MARKET_DATA_API_KEY=

NEWS_DATA_PROVIDER=mock
NEWS_DATA_API_KEY=
```

> **Never commit `.env` to Git.** Only `.env.example` is tracked.

---

## 6. Database Setup

Phase 1 already created the MySQL database `stock_ml_db`.  
Phase 2 **does not drop or recreate** existing tables.

Verify your database is running:

```bash
mysql -u root stock_ml_db -e "SHOW TABLES;"
```

Expected tables:

```
alembic_version
companies
financial_metrics
market_sentiment
model_runs
predictions
raw_ai_data
stock_events
stock_prices
stocks
users
```

---

## 7. Alembic Migrations

```bash
# Apply all pending migrations to the existing DB
make migrate
# or
.venv/bin/alembic upgrade head

# Check current migration state
.venv/bin/alembic current

# Create a new migration (after changing ORM models)
.venv/bin/alembic revision --autogenerate -m "describe change"

# Rollback one step
.venv/bin/alembic downgrade -1
```

> Migrations are stored in `alembic/versions/`. The Phase 2 migration is `e40ea9e35d92_phase_2_schema.py`.

---

## 8. Running the Backend

### Using Makefile (recommended)

```bash
# Development mode (auto-reload on file changes)
make dev

# Production mode (no reload)
make run
```

### Manual commands

```bash
cd /Users/guntukaraviteja/project/backend

# Development
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Production
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Verify startup

```
GET http://localhost:8000/health
→ {"status": "ok", "database": "connected"}

GET http://localhost:8000/
→ {"app": "Stock Intelligence API", "docs": "/docs", ...}

Swagger UI:  http://localhost:8000/docs
ReDoc:       http://localhost:8000/redoc
OpenAPI:     http://localhost:8000/openapi.json
```

---

## 9. Running Tests

```bash
# Using Makefile
make test

# Manual
.venv/bin/pytest tests/ -v
```

Expected output — **24 tests pass**:

```
tests/test_database.py::test_database_connection          PASSED
tests/test_database.py::test_check_database_health_function PASSED
tests/test_health.py::test_health_endpoint_success        PASSED
tests/test_health.py::test_health_endpoint_db_failure     PASSED
tests/test_stocks.py::test_create_stock                   PASSED
tests/test_stocks.py::test_get_stock_by_id                PASSED
tests/test_stocks.py::test_get_stock_by_symbol            PASSED
tests/test_stocks.py::test_duplicate_stock_handling       PASSED
tests/test_stocks.py::test_stock_not_found                PASSED
tests/test_stocks.py::test_invalid_input_create_stock     PASSED
tests/test_stocks.py::test_update_stock                   PASSED
tests/test_stocks.py::test_delete_stock                   PASSED
tests/test_prices.py::test_price_insertion                PASSED
tests/test_prices.py::test_duplicate_price_handling       PASSED
tests/test_prices.py::test_price_history_retrieval        PASSED
tests/test_prices.py::test_latest_price_retrieval         PASSED
tests/test_prices.py::test_invalid_price_data             PASSED
tests/test_events.py::test_event_creation                 PASSED
tests/test_events.py::test_event_retrieval                PASSED
tests/test_events.py::test_duplicate_event_rejection      PASSED
tests/test_companies.py::test_company_profile_crud        PASSED
tests/test_ingestion.py::test_mock_market_provider        PASSED
tests/test_ingestion.py::test_provider_failure_handling   PASSED
tests/test_ingestion.py::test_ingestion_pipeline_run      PASSED
========================= 24 passed =========================
```

> Tests use an **in-memory SQLite database** — they never touch your development MySQL data.

---

## 10. Seeding the Database

```bash
make seed
# or
.venv/bin/python scripts/seed_stocks.py
```

Seeds 6 stocks with company profiles (idempotent — safe to run multiple times):

| Symbol | Company | Exchange |
|--------|---------|----------|
| AAPL | Apple Inc. | NASDAQ |
| MSFT | Microsoft Corporation | NASDAQ |
| GOOGL | Alphabet Inc. | NASDAQ |
| AMZN | Amazon.com Inc. | NASDAQ |
| NVDA | NVIDIA Corporation | NASDAQ |
| TSLA | Tesla, Inc. | NASDAQ |

---

## 11. Running Ingestion

```bash
# Ingest all seeded stocks (last 30 days)
make ingest
# or
.venv/bin/python scripts/ingest_market_data.py

# Ingest specific symbols
.venv/bin/python scripts/ingest_market_data.py --symbols AAPL MSFT

# Ingest with custom date range (days back from today)
.venv/bin/python scripts/ingest_market_data.py --days 7

# Disable event ingestion
.venv/bin/python scripts/ingest_market_data.py --no-events
```

Example output:

```
Starting ingestion...

AAPL:
  fetched: 30
  inserted: 25
  skipped: 5

MSFT:
  fetched: 30
  inserted: 30
  skipped: 0

Ingestion completed.
```

> Uses **mock provider** by default (no API key required). Duplicate records are automatically skipped.

---

## 12. API Endpoints

### Health

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | App + DB health check |

### Stocks

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/stocks` | List all stocks |
| POST | `/api/v1/stocks` | Create a new stock |
| GET | `/api/v1/stocks/{stock_id}` | Get stock by ID |
| GET | `/api/v1/stocks/symbol/{symbol}` | Get stock by ticker |
| PUT | `/api/v1/stocks/{stock_id}` | Update stock |
| DELETE | `/api/v1/stocks/{stock_id}` | Delete stock |

### Prices

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/stocks/{stock_id}/prices` | Price history (filterable) |
| GET | `/api/v1/stocks/{stock_id}/prices/latest` | Latest price |
| POST | `/api/v1/stocks/{stock_id}/prices` | Add single price |
| POST | `/api/v1/stocks/{stock_id}/prices/bulk` | Bulk insert prices |

### Events

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/stocks/{stock_id}/events` | Events for a stock |
| POST | `/api/v1/stocks/{stock_id}/events` | Create event |
| GET | `/api/v1/events/recent` | Recent events (all stocks) |

### Companies

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/companies` | List company profiles |
| GET | `/api/v1/companies/{stock_id}` | Company by stock ID |
| GET | `/api/v1/companies/symbol/{symbol}` | Company by ticker |
| PUT | `/api/v1/companies/{stock_id}` | Update company profile |

---

## 13. Example API Requests

```bash
# Health check
curl http://localhost:8000/health

# List all stocks
curl http://localhost:8000/api/v1/stocks

# Get stock by symbol
curl http://localhost:8000/api/v1/stocks/symbol/AAPL

# Create a new stock
curl -X POST http://localhost:8000/api/v1/stocks \
  -H "Content-Type: application/json" \
  -d '{"symbol": "INFY", "company_name": "Infosys Ltd", "exchange": "NSE"}'

# Get price history with date filter
curl "http://localhost:8000/api/v1/stocks/1/prices?start_date=2026-09-01&limit=10"

# Get latest price
curl http://localhost:8000/api/v1/stocks/1/prices/latest

# Create event
curl -X POST http://localhost:8000/api/v1/stocks/1/events \
  -H "Content-Type: application/json" \
  -d '{
    "event_type": "EARNINGS",
    "event_title": "Q3 Earnings Beat",
    "event_date": "2026-10-01",
    "impact": "POSITIVE"
  }'

# Recent events across all stocks
curl "http://localhost:8000/api/v1/events/recent?limit=5"

# Company profile
curl http://localhost:8000/api/v1/companies/symbol/AAPL
```

---

## 14. Troubleshooting

### `zsh: command not found: uvicorn`

You are using the system Python. Always prefix commands with the venv path:

```bash
# Wrong ❌
uvicorn app.main:app --reload

# Correct ✅
.venv/bin/uvicorn app.main:app --reload

# Or use Makefile (handles this automatically)
make dev
```

### `No module named pytest`

Same issue — system Python has no project packages:

```bash
# Wrong ❌
python -m pytest -v

# Correct ✅
.venv/bin/pytest tests/ -v

# Or
make test
```

### Activate the venv once per terminal session

```bash
source .venv/bin/activate
# Then you can use plain: pytest, uvicorn, python, alembic
deactivate   # to exit
```

### `Access denied for user 'root'@'localhost'`

Your MySQL root user may require a password. Update `.env`:

```ini
DATABASE_URL=mysql+pymysql://root:YOUR_PASSWORD@127.0.0.1:3306/stock_ml_db
```

### `Can't connect to MySQL server`

Start MySQL:

```bash
brew services start mysql
# or
mysql.server start
```

### Alembic `Target database is not up to date`

```bash
.venv/bin/alembic upgrade head
```

---

## 15. Phase 2 Completion Checklist

| Item | Status |
|------|--------|
| Backend starts successfully | ✅ |
| MySQL connection works | ✅ |
| `/health` endpoint works | ✅ |
| SQLAlchemy models work | ✅ |
| Pydantic schemas work | ✅ |
| Stock CRUD works | ✅ |
| Price APIs work | ✅ |
| Event APIs work | ✅ |
| Company APIs work | ✅ |
| Market provider abstraction exists | ✅ |
| News provider abstraction exists | ✅ |
| Mock providers work without API keys | ✅ |
| Ingestion pipeline works | ✅ |
| Duplicate protection works | ✅ |
| Alembic configured and working | ✅ |
| Structured logging works | ✅ |
| Error handling works | ✅ |
| pytest passes (24/24) | ✅ |
| Seed script works (idempotent) | ✅ |
| API docs work (/docs, /redoc) | ✅ |
| Docker support exists | ✅ |
| README is complete | ✅ |
| Existing Phase 1 data intact | ✅ |
| **Phase 3 NOT implemented** | ✅ |

---

## Docker (Optional)

> Docker is provided for convenience. Your existing local MySQL from Phase 1 is **not affected** by Docker.

```bash
# Build and start backend only (connects to host MySQL)
docker build -t stock-api .
docker run -p 8000:8000 --env-file .env stock-api

# Full stack with Docker MySQL (isolated from Phase 1 DB)
docker-compose up --build
```

> The `docker-compose.yml` creates a **separate** MySQL container. It does not modify your existing `stock_ml_db`.

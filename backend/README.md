# Stock Intelligence API — Phase 10

Production-structured FastAPI backend for the Stock Intelligence & Analysis Platform.  
**Phases 1–10 complete:** Backend Foundation + MySQL Integration + Data Ingestion + Market Data & Stock Intelligence Layer + Stock Analytics + Technical Signal Engine & Screener + Backtesting + Portfolio & Paper-Trading Engine + Risk Management & Portfolio Analytics.

> ⚠️ **Phase 7 signals are not financial advice and do not guarantee outcomes.**
> A `bullish_alignment` classification means several technical indicators currently
> agree in direction. It is not a prediction, not a probability, and not a buy/sell
> recommendation. See [`docs/phase-7.md`](docs/phase-7.md).

> ⚠️ **Phases 9 and 10 are paper trading / simulation only.**
> Portfolios, orders and trades are virtual. No brokerage is contacted, no credentials
> are stored, and no real order is ever placed. See [`docs/phase-9.md`](docs/phase-9.md)
> and [`docs/phase-10.md`](docs/phase-10.md).

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
15. [Phase 9 Completion Checklist](#15-phase-9-completion-checklist)
16. [Phase 10 Completion Checklist](#16-phase-10-completion-checklist)
17. [Phase 11 Completion Checklist](#17-phase-11-completion-checklist)
18. [Phase 12 Completion Checklist](#18-phase-12-completion-checklist)
19. [Phase 13 Completion Checklist](#19-phase-13-completion-checklist)
20. [Phase 15 Completion Checklist](#20-phase-15-completion-checklist)

---

## 1. Project Overview

Stock Intelligence API is a RESTful backend service that:

- Connects to a MySQL database (created in Phase 1)
- Manages stock master data, historical prices, and corporate events
- Provides a market-data ingestion pipeline with mock and real providers (Alpha Vantage, Yahoo Finance)
- Exposes a fully documented REST API (Swagger / ReDoc)
- **Phase 4:** Market Data & Stock Intelligence Layer — sync prices from external providers, historical data retrieval, latest price, idempotent upsert
- **Phase 6:** Stock Analytics — SMA, EMA, RSI, MACD, volatility, returns, support/resistance, Bollinger Bands, volume averages
- **Phase 7:** Technical Signal Engine & Screener — deterministic, explainable signal rules, a −100…+100 composite alignment score, and a filterable technical screener
- **Phase 8:** Backtesting — historical strategy simulation, trade generation, P&L and metrics
- **Phase 9:** Portfolio & Paper Trading — virtual portfolios, simulated orders and fills, positions, cash accounting, realised/unrealised P&L, deterministic risk limits, equity snapshots and portfolio performance
- Is structured for extensibility into later phases

**ML models and price prediction have NOT been implemented and are explicitly out of scope.**
**Real-money trading has NOT been implemented and is out of scope: Phase 9 is a local simulation with no brokerage integration.**

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
│   │   ├── raw_ai_data.py    # raw_ai_data table
│   │   ├── backtest.py       # Phase 8 backtest tables
│   │   └── portfolio.py      # Phase 9 portfolios, positions, orders, trades, snapshots
│   ├── schemas/              # Pydantic request / response schemas
│   │   ├── analytics.py      # SMA / EMA / RSI / MACD / Bollinger / volume responses
│   │   ├── signals.py        # Phase 7 signal enums + response models
│   │   └── screeners.py      # Phase 7 screener request / response models
│   ├── repositories/         # Data access layer (DB queries only)
│   ├── services/             # Business logic layer
│   │   ├── ingestion_service.py    # Orchestrates ingestion pipeline
│   │   ├── analytics_service.py    # Phase 6 indicator calculations
│   │   ├── signal_service.py       # Phase 7 SignalEngine (rules) + SignalService
│   │   └── screener_service.py     # Phase 7 screener filtering
│   ├── api/v1/               # FastAPI route handlers
│   │   ├── analytics.py      # Phase 6 analytics endpoints
│   │   ├── signals.py        # Phase 7 signal endpoints
│   │   └── screeners.py      # Phase 7 screener endpoint
│   └── providers/            # Pluggable market data + news providers
├── scripts/
│   ├── seed_stocks.py        # Insert seed stocks + company profiles
│   └── ingest_market_data.py # CLI ingestion runner
├── docs/
│   └── phase-7.md            # Phase 7 architecture, rules, scoring, screener
├── tests/                    # pytest test suite (199 tests)
├── alembic/                  # Database migration scripts
├── Makefile                  # Single-command shortcuts
├── Dockerfile                # Container image
├── docker-compose.yml        # Local container orchestration
└── requirements.txt
```

**Layer flow:**  
`API Route → Service → Repository → SQLAlchemy Model → MySQL`

Routers never query the database and never compute indicators — Phase 7 signal rules
run in a stateless `SignalEngine` inside the service layer.

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

# Phase 10 risk defaults (empty disables the rule; per-portfolio values win)
PAPER_TRADING_MAX_EXPOSURE_PCT=
PAPER_TRADING_MAX_TRADE_ALLOCATION_PCT=
PAPER_TRADING_MIN_CASH_RESERVE_PCT=
PAPER_TRADING_MAX_POSITIONS=
# OPEN = signal at bar N's close, fill at bar N+1's open | CLOSE = fill at bar N's close
PAPER_TRADING_EXECUTION_PRICE_POLICY=OPEN
PAPER_TRADING_RISK_FREE_RATE_PCT=0
```

> **Never commit `.env` to Git.** Only `.env.example` is tracked.

> **The Phase 10 migration must be applied before using the new endpoints.** An unmigrated
> database rejects orders with an SQL error about a missing `signal_as_of` column. Run
> `make migrate` (or `alembic upgrade head`) after pulling these changes.

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

Expected output — **199 tests pass**:

```
tests/test_database.py        # database engine, session, health
tests/test_health.py          # /health success + DB-failure paths
tests/test_stocks.py          # stock CRUD, duplicates, not-found, validation
tests/test_prices.py          # insertion, history, latest, duplicates, validation
tests/test_events.py          # event creation, retrieval, duplicates
tests/test_companies.py       # company profile CRUD
tests/test_ingestion.py       # provider mocks and pipeline
tests/test_market_data.py     # validator, normalizer, fetcher, service
tests/test_analytics.py       # Phase 6 indicators + Bollinger/volume endpoints
tests/test_signals.py         # Phase 7 signal rules, scoring, classification, API
tests/test_screeners.py       # Phase 7 screener validation, filters, API
=========================== 199 passed ===========================
```

> Tests use an **in-memory SQLite database** — they never touch your development MySQL data.
> Phase 7 tests use fixed `Decimal` price series: no randomness, no wall clock, and no
> external API calls, so results are fully deterministic.

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
| POST | `/api/v1/stocks/{stock_id}/prices/sync` | Sync prices from provider (idempotent) |

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

### Analytics (Phase 6)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/stocks/{stock_id}/analytics` | Consolidated analytics snapshot |
| GET | `/api/v1/stocks/{stock_id}/analytics/sma` | Simple moving average |
| GET | `/api/v1/stocks/{stock_id}/analytics/ema` | Exponential moving average |
| GET | `/api/v1/stocks/{stock_id}/analytics/rsi` | Relative Strength Index |
| GET | `/api/v1/stocks/{stock_id}/analytics/macd` | MACD line / signal / histogram |
| GET | `/api/v1/stocks/{stock_id}/analytics/volatility` | Historical volatility |
| GET | `/api/v1/stocks/{stock_id}/analytics/returns` | Period returns |
| GET | `/api/v1/stocks/{stock_id}/analytics/support-resistance` | Support & resistance levels |
| GET | `/api/v1/stocks/{stock_id}/analytics/bollinger-bands` | Bollinger Bands (+ %B, bandwidth) |
| GET | `/api/v1/stocks/{stock_id}/analytics/volume` | Latest vs average volume |

### Technical Signals (Phase 7)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/stocks/{stock_id}/signals` | Full signal summary + composite score |
| GET | `/api/v1/stocks/{stock_id}/signals/latest` | Strongest individual signal |
| GET | `/api/v1/stocks/{stock_id}/signals/classification` | Alignment label only |

### Technical Screener (Phase 7)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/screeners/technical` | Filter the universe by signal/indicator criteria |

### Backtesting (Phase 8)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/backtests/strategies` | Registered strategy identifiers |
| POST | `/api/v1/backtests` | Run a historical backtest (stored) |
| GET | `/api/v1/backtests` | List stored backtests |
| GET | `/api/v1/backtests/{id}` | Backtest with metrics, trades, equity curve |
| GET | `/api/v1/backtests/{id}/trades` | Round-trip trades |
| GET | `/api/v1/backtests/{id}/equity` | Equity curve points |

### Paper Trading (Phase 9) — simulated only

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/portfolios` | Create a virtual portfolio |
| GET | `/api/v1/portfolios` | List portfolios |
| GET | `/api/v1/portfolios/{id}` | Cash, equity, realised/unrealised P&L, return % |
| PATCH | `/api/v1/portfolios/{id}/status` | ACTIVE / PAUSED / CLOSED (CLOSED is terminal) |
| GET | `/api/v1/portfolios/{id}/positions` | Open positions and allocation |
| POST | `/api/v1/portfolios/{id}/orders` | Place a simulated market order |
| GET | `/api/v1/portfolios/{id}/orders` | Order history, including rejections |
| GET | `/api/v1/portfolios/{id}/trades` | Trade history |
| POST | `/api/v1/portfolios/{id}/valuation` | Mark to market (no trades, no cash movement) |
| POST | `/api/v1/portfolios/{id}/snapshots` | Capture an equity-curve point |
| GET | `/api/v1/portfolios/{id}/snapshots` | Equity curve |
| GET | `/api/v1/portfolios/{id}/performance` | Performance, fees, counts, drawdown |
| POST | `/api/v1/portfolios/{id}/strategy-orders` | Phase 7/8 strategy decision as a paper order |

### Risk Management and Portfolio Analytics (Phase 10) — simulated only

| Method | Endpoint | Description |
|--------|----------|-------------|
| PATCH | `/api/v1/portfolios/{id}` | Update name / description / status / risk limits |
| GET | `/api/v1/portfolios/{id}/positions/{stock_id}` | One position with allocation |
| GET | `/api/v1/portfolios/{id}/orders/{order_id}` | One order, including a rejection reason |
| POST | `/api/v1/portfolios/{id}/orders/{order_id}/execute` | Fill an order recorded with `defer_execution` |
| POST | `/api/v1/portfolios/{id}/risk/check` | Would this trade be accepted? (changes nothing) |
| GET | `/api/v1/portfolios/{id}/risk` | Limits in force, plus any rule already breached |
| POST | `/api/v1/portfolios/{id}/position-size` | Derive a quantity (fixed / percentage / risk) |
| GET | `/api/v1/portfolios/{id}/equity-curve` | Recorded curve, optionally date-bounded |
| GET | `/api/v1/portfolios/{id}/drawdown` | Peak, current and maximum drawdown + curve |
| POST | `/api/v1/portfolios/{id}/simulate` | Replay stored history against the portfolio |

#### Risk rules

| Limit | Blocks | Default |
|-------|--------|---------|
| `max_drawdown_pct` | BUY once equity is this far below peak equity | disabled |
| `max_positions` | BUY that would open a position beyond this count | disabled |
| `max_position_quantity` | BUY beyond this many shares in one stock | disabled |
| `max_position_value` | BUY beyond this market value in one stock | disabled |
| `max_position_allocation_pct` | BUY beyond this share of equity in one stock | disabled |
| `max_exposure_pct` | BUY beyond this share of equity invested overall | disabled |
| `max_trade_allocation_pct` | BUY committing more than this share of equity | disabled |
| `min_cash_reserve_pct` | BUY leaving less cash than this share of equity | disabled |

Every limit is optional per portfolio and falls back to the `PAPER_TRADING_*` application
defaults. A limit is breached **strictly** above its threshold, so a value exactly at the
limit is accepted.

#### Rejection codes

Every refusal carries a stable, machine-readable `code` and a `details.rule` naming the
limit that fired:

`PORTFOLIO_INACTIVE`, `INVALID_QUANTITY`, `INVALID_PRICE`, `INSUFFICIENT_CASH`,
`INSUFFICIENT_POSITION`, `ORDER_NOT_EXECUTABLE`, `MAX_DRAWDOWN_EXCEEDED`,
`MAX_POSITIONS_EXCEEDED`, `MAX_POSITION_QUANTITY_EXCEEDED`, `MAX_POSITION_SIZE_EXCEEDED`,
`MAX_POSITION_ALLOCATION_EXCEEDED`, `MAX_EXPOSURE_EXCEEDED`,
`MAX_TRADE_ALLOCATION_EXCEEDED`, `MIN_CASH_RESERVE_BREACHED`, `RISK_LIMIT_EXCEEDED`.

A rejected order is still recorded with its code and reason, so a refusal is auditable.
No cash, position or P&L changes when an order is refused.

### Portfolio Construction & Risk Management (Phase 11) — simulated only

The layer that decides *what the portfolio should hold*, consuming the Phase 7 screener and the
Phase 9/10 order path. Full documentation: [`docs/phase-11.md`](docs/phase-11.md).

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/portfolios/{id}/decisions` | BUY / SELL / HOLD / REDUCE per stock, each explained; `apply` submits them |
| POST | `/api/v1/portfolios/{id}/rebalance` | Plan drift against target weights; `apply` submits the plan |
| GET | `/api/v1/portfolios/{id}/risk/report` | Exposure, concentration, violations and risk recommendations |
| POST | `/api/v1/portfolios/{id}/deposit` | Add capital |
| POST | `/api/v1/portfolios/{id}/withdraw` | Remove capital |
| GET | `/api/v1/portfolios/{id}/transactions` | Append-only cash ledger, filterable by type and stock |
| GET | `/api/v1/portfolios/{id}/transactions/summary` | Counts per transaction type |

```bash
# What should this portfolio hold? Nothing is written without "apply": true.
curl -X POST localhost:8000/api/v1/portfolios/1/decisions \
  -H 'content-type: application/json' \
  -d '{"candidates":[{"stock_id":101,"price":"1000"}],"apply":false}'
```

Every decision carries `reasons` — stable codes such as `underweight`, `equal_weight_allocation`,
`cash_reserve_reached` — and one sentence of `explanation` per code. There is no opaque score
anywhere in the engine.

Sizing supports three formulas, all floored to whole shares and all bounded by investable cash:
`EQUAL_WEIGHT`, `RISK_BASED` (a risk budget over the configured stop) and `MAX_POSITION_CAP`.

A crossed stop-loss or take-profit is a **recommendation**, not a violation: it is a level to act
at, not a limit the portfolio is outside of. Nothing is executed until `apply: true`.

### Daily Loss, Trade Sizing & Lifecycle (Phase 12) — simulated only

The gaps Phase 9–11 left in the portfolio layer. Full documentation:
[`docs/phase-12.md`](docs/phase-12.md).

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/portfolios/{id}/valuation` | Mark positions at supplied prices; optionally persist a snapshot |
| GET | `/api/v1/portfolios/{id}/valuation` | Read back the stored marks and marked equity |
| POST | `/api/v1/portfolios/{id}/pause` | Stop executing orders; resumable |
| POST | `/api/v1/portfolios/{id}/close` | Retire the portfolio permanently |

Three new settings, all disabled by default so an existing portfolio behaves exactly as it did:

| Setting | Default | Meaning |
|---------|---------|---------|
| `PAPER_TRADING_MAX_DAILY_LOSS_PCT` | unset (off) | Share of the day's *opening* equity that may be lost before the day closes to new BUYs |
| `PAPER_TRADING_MIN_TRADE_VALUE` | unset (off) | Smallest accepted order, in the portfolio's currency |
| `PAPER_TRADING_DEFAULT_CURRENCY` | `INR` | ISO 4217 code stamped on every portfolio, read at creation time |

```bash
# Would this order be refused, and why? Every breached rule is reported, not just the first.
curl -X POST localhost:8000/api/v1/portfolios/1/risk/check \
  -H 'content-type: application/json' \
  -d '{"stock_id":101,"side":"BUY","quantity":400,"requested_price":"1000"}'
```

The daily-loss limit is measured against the day's **opening** equity — the last snapshot before
today, else the earliest snapshot today, else initial capital plus external flows — so a deposit
never counts as a gain and never resets the allowance. It refuses **BUYs only**: a loss limit that
could block a SELL would trap the very position it exists to cut.

`min_trade_value` applies to both sides, because a dust SELL is a rounding artefact that still pays
a commission, and it is measured on the slippage-adjusted gross value.

> **The Phase 12 migration must be applied before using the new endpoints.** An unmigrated
> database has no `portfolios.currency` column.

### Portfolio & Risk Management Engine (Phase 13) — simulated only

Multi-candidate allocation, stop-aware position sizing, structured risk flags and portfolio-level
backtesting. Full documentation: [`docs/phase-13.md`](docs/phase-13.md).

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/portfolios/{id}/allocate` | Allocate across one or more candidate signals, ranked and sized against each other |
| GET | `/api/v1/portfolios/{id}/risk/state` | Current exposure, weights, P&L, drawdown and structured risk flags |
| POST | `/api/v1/portfolios/{id}/backtest` | Replay several stocks against one portfolio, chronologically |
| POST | `/api/v1/portfolios/{id}/risk/position-size` | Size one trade, returning every intermediate figure |

The sizing model, in full:

```
quantity = floor(min(
    floor(risk_budget / (entry_price - stop_price)),   # risk: loss at the stop
    floor(max_position_value / entry_price),           # capital: single-name cap
    quantity_by_cash))                                # cash: price + fees must fit
```

Both bounds are floored, never rounded up — a position one share larger than the risk budget allows
would put more than the configured risk at risk. The response names **which** bound applied, so the
size can be re-derived from the response alone.

```bash
# Every candidate gets a decision, and every refusal names every rule it broke.
curl -X POST localhost:8000/api/v1/portfolios/1/allocate \
  -H 'content-type: application/json' \
  -d '{"candidates":[{"stock_id":101,"entry_price":"1000","stop_price":"950","composite_score":72}],
       "apply": false}'
```

Three settings, all disabled by default:

| Setting | Default | Meaning |
|---------|---------|---------|
| `PORTFOLIO_MAX_DRAWDOWN_PCT` | unset (off) | Decline from peak equity at which the drawdown flag is raised |
| `PORTFOLIO_MAX_TOTAL_RISK_PCT` | unset (off) | Share of equity that may sit at risk across all open stops |
| `PORTFOLIO_MIN_TRADE_VALUE` | unset (off) | Smallest trade the engine will propose |

Candidates are ranked by `composite_score` descending then `stock_id` ascending — the existing
Phase 11 key, so the two engines can never disagree about which candidate is better. Allocation is
**sequential**: candidates compete for one pool of cash, so each is sized against what the previous
one actually left behind. `apply` defaults to `false`; an allocation is a recommendation until it is
asked for, and when applied every order still travels through the ordinary Phase 10 order path.

No migration is required: Phase 13 adds no columns and no tables.

### Portfolio Risk Analysis & Exposure Control (Phase 15) — simulated only

The layer above allocation: given the portfolio Phase 13/14 produced, is it acceptable? Full
documentation: [`docs/phase-15.md`](docs/phase-15.md).

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/portfolios/risk/analyze` | Evaluate a submitted portfolio against portfolio-level risk limits |
| POST | `/api/v1/portfolios/risk/adjust` | Run the deterministic reduction loop; returns before and after |
| POST | `/api/v1/portfolios/{id}/risk/analyze` | Evaluate a persisted paper portfolio from stored state |

```bash
curl -X POST localhost:8000/api/v1/portfolios/risk/analyze \
  -H 'content-type: application/json' \
  -d '{"portfolio_value":"20000.00","cash":"4000.00",
       "positions":[{"symbol":"AAA","quantity":50,"price":"100","sector":"Technology","beta":"1.2"}],
       "asset_returns":{"AAA":["0.011","0.004"]},
       "risk_config":{"max_single_asset_weight":"25"}}'
```

The report holds portfolio exposure (positions, sectors, strategies and asset classes, with a symbol
held by several strategies aggregated into one exposure), concentration metrics, annualised
volatility, beta, maximum drawdown, historical VaR and Expected Shortfall, per-position risk
contributions, correlation clusters, seven deterministic stress scenarios, a liquidity assessment,
every limit breach, the deterministic reductions applied, and one of four decisions:
`APPROVED`, `ADJUSTED`, `REJECTED` or `DATA_INSUFFICIENT`.

**Missing data is never reported as zero.** A metric that cannot be computed comes back as
`"value": null` with `"status": "DATA_INSUFFICIENT"` and an explanation naming the missing input —
because a zero volatility would claim the portfolio cannot move, and a zero participation ratio
would claim it is liquid. A scenario that had nothing to act on is `NOT_APPLICABLE`, which is a
different statement from a scenario that could not run.

Adjustments only ever **reduce**: whole shares are sold and the proceeds stay in cash, so equity and
total leverage are unchanged. Each action is re-evaluated against the whole measurement stack before
the next is chosen, in a fixed priority order. Limits in `PORTFOLIO_RISK_MANDATORY_LIMITS` are never
adjusted — a breach of one is reported and the portfolio is rejected, because a limit an operator
declared mandatory is one the engine should not quietly fix on its own.

```bash
PORTFOLIO_RISK_MAX_SINGLE_ASSET_WEIGHT=20
PORTFOLIO_RISK_MAX_SECTOR_WEIGHT=35
PORTFOLIO_RISK_MAX_STRESS_LOSS=20
PORTFOLIO_RISK_MIN_OBSERVATIONS=30
```

Every limit is a percentage of equity, is overridable per request through `risk_config`, and is
**disabled** by an empty value rather than set to zero. No migration is required: Phase 15 adds no
columns and no tables.

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

# Sync prices from provider (idempotent)
curl -X POST http://localhost:8000/api/v1/stocks/1/prices/sync \
  -H "Content-Type: application/json" \
  -d '{"start_date": "2026-09-01", "end_date": "2026-09-30"}'

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

### Phase 7 — Signals & Screener

```bash
# Full technical signal summary (composite score + every signal + indicator snapshot)
curl http://localhost:8000/api/v1/stocks/85/signals

# Strongest individual signal
curl http://localhost:8000/api/v1/stocks/85/signals/latest

# Alignment classification only
curl http://localhost:8000/api/v1/stocks/85/signals/classification
# → "bullish_alignment"

# Bullish screen: score of at least 20
curl -X POST http://localhost:8000/api/v1/screeners/technical \
  -H "Content-Type: application/json" \
  -d '{"direction": "bullish", "min_score": 20, "limit": 5}'

# Bearish screen
curl -X POST http://localhost:8000/api/v1/screeners/technical \
  -H "Content-Type: application/json" \
  -d '{"direction": "bearish", "max_score": -20}'

# Oversold, below the 50-period average, bearish MACD, liquid
curl -X POST http://localhost:8000/api/v1/screeners/technical \
  -H "Content-Type: application/json" \
  -d '{
    "min_rsi": 25,
    "max_rsi": 45,
    "price_below_sma": 50,
    "macd_direction": "bearish",
    "min_volume": 500000
  }'

# Bollinger Bands and volume averages (the Phase 6 inputs the engine consumes)
curl "http://localhost:8000/api/v1/stocks/85/analytics/bollinger-bands?period=20"
curl "http://localhost:8000/api/v1/stocks/85/analytics/volume?period=20"
```

Full reference: [`docs/phase-7.md`](docs/phase-7.md).

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

## 15. Phase 9 Completion Checklist

### Phases 1–4 (Foundation, MySQL, Schema, Market Data)

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
| Seed script works (idempotent) | ✅ |
| API docs work (/docs, /redoc) | ✅ |
| Docker support exists | ✅ |
| README is complete | ✅ |
| Existing Phase 1 data intact | ✅ |
| Sync endpoint (`/stocks/{id}/prices/sync`) works | ✅ |
| Idempotent upsert (no duplicates) | ✅ |
| Provider abstraction (mock/alphavantage/yfinance) | ✅ |
| Historical price retrieval works | ✅ |
| Latest price retrieval works | ✅ |
| Provider errors handled gracefully | ✅ |

### Phase 6 — Stock Analytics

| Item | Status |
|------|--------|
| SMA / EMA calculations | ✅ |
| RSI (Wilder smoothing) | ✅ |
| MACD (line, signal, histogram) | ✅ |
| Volatility and returns | ✅ |
| Support / resistance levels | ✅ |
| Bollinger Bands (+ %B, bandwidth) | ✅ |
| Volume average | ✅ |
| Consolidated analytics snapshot | ✅ |
| Indicators reused by Phase 7 (not duplicated) | ✅ |

### Phase 7 — Technical Signal Engine & Screener

| Item | Status |
|------|--------|
| Deterministic, explainable signal rules | ✅ |
| RSI, MA, MA crossover, MACD, Bollinger, volume, momentum signals | ✅ |
| `strength` 0–100 with weak/moderate/strong bands | ✅ |
| Composite score −100…+100 | ✅ |
| Alignment classifications (no BUY/SELL labels) | ✅ |
| Family-capped aggregation prevents double-counting | ✅ |
| Explanations describe conditions, never forecasts | ✅ |
| Screener filters (score, direction, RSI, SMA, MACD, volume) | ✅ |
| Full screener validation (ranges, ordering, mutual exclusion) | ✅ |
| Insufficient data never crashes, never fabricates | ✅ |
| `no_price_data` / `insufficient_data` distinguished | ✅ |
| Signal endpoint (`/stocks/{id}/signals`) | ✅ |
| Latest signal endpoint | ✅ |
| Classification endpoint | ✅ |
| Screener endpoint (`/screeners/technical`) | ✅ |
| Endpoints documented in `/docs` with schemas + status codes | ✅ |
| Deterministic fixtures, no external API calls in tests | ✅ |
| pytest passes (199/199) | ✅ |
| No regressions in Phases 1–6 (64 pre-existing tests green) | ✅ |
| `docs/phase-7.md` written | ✅ |
| Not-guaranteed-outcomes disclaimer | ✅ |
| **ML / price prediction NOT implemented** | ✅ |
| **Real-money trading / brokerage execution NOT implemented (paper trading only)** | ✅ |

### Phase 9 — Portfolio & Paper-Trading Engine

| Item | Status |
|------|--------|
| Virtual portfolio creation (cash = equity = initial capital) | ✅ |
| Cash accounting (`cash ± gross ∓ fees`), never negative | ✅ |
| Long-only positions, weighted average entry price | ✅ |
| Partial and full exits; position row removed at zero | ✅ |
| Realised P&L net of exit and allocated entry fees | ✅ |
| Unrealised P&L (gross mark to market) and valuation without trades | ✅ |
| Portfolio equity, total P&L, return percentage | ✅ |
| Portfolio snapshots and equity curve (on request, no scheduler) | ✅ |
| Simulated market orders with configurable fees and slippage | ✅ |
| Trades immutable after execution; one trade per filled order | ✅ |
| Order/trade history with statuses and pagination | ✅ |
| Rejection records: insufficient cash, oversell, risk, inactive, invalid input | ✅ |
| Risk limits: position quantity, position value, allocation, drawdown | ✅ |
| Portfolio states: ACTIVE / PAUSED / CLOSED, CLOSED terminal | ✅ |
| Idempotent orders and strategy-event duplicate protection | ✅ |
| Atomic, transactional order execution with rollback | ✅ |
| Decimal money arithmetic, shared with Phase 8 quantisation | ✅ |
| Phase 7 signals + Phase 8 strategy rules reused, never duplicated | ✅ |
| All money columns `DECIMAL` (no `FLOAT`), FKs and indexes | ✅ |
| **Paper trading only: no brokerage, no credentials, no real orders** | ✅ |
| **Phase 1–8 tests unchanged and passing (250 → 379 total)** | ✅ |

---

## 16. Phase 10 Completion Checklist

### Risk management

| Item | Status |
|------|--------|
| Deterministic pre-trade check that changes nothing | ✅ |
| All breached rules reported, not only the first | ✅ |
| Configurable per-portfolio limits, persisted and re-read | ✅ |
| Rules: exposure, trade allocation, cash reserve, position count | ✅ |
| Rules: position quantity, value, allocation, drawdown | ✅ |
| Growth limits never block a SELL | ✅ |
| `GET /risk` reports limits in force plus current breaches | ✅ |
| Machine-readable rejection code per rule | ✅ |

### Position sizing

| Item | Status |
|------|--------|
| `FIXED_CAPITAL` → `floor(capital / unit_cost)` | ✅ |
| `PERCENTAGE` → `floor(equity × pct / unit_cost)` | ✅ |
| `RISK_BASED` → `floor(equity × risk_pct / \|entry − stop\|)`, cash-capped | ✅ |
| Quantities floored; fees included in the unit cost | ✅ |
| Arithmetic reported back, not just the quantity | ✅ |
| No order placed by the sizing endpoint | ✅ |

### Valuation and performance

| Item | Status |
|------|--------|
| Equity, invested capital, exposure %, position weights | ✅ |
| Realised / unrealised / total P&L and return % | ✅ |
| Drawdown curve with peak, current and maximum drawdown | ✅ |
| Volatility and Sharpe from daily portfolio returns | ✅ |
| Win rate, average win, average loss, profit factor | ✅ |
| `null` — never `0` — for unmeasurable metrics | ✅ |
| Equity curve and drawdown read the same persisted columns | ✅ |

### Execution and replay

| Item | Status |
|------|--------|
| `defer_execution` records a `PENDING` order; explicit execute fills it | ✅ |
| Deferred fill re-validated against current state | ✅ |
| Sequential historical replay with `OPEN` / `CLOSE` policies | ✅ |
| **No look-ahead: point-in-time indicators and later-bar fills** | ✅ |
| `signal_date` and `executed_at` recorded on every simulated order | ✅ |
| Simulated fills use the manual order path — same rules, no duplicates | ✅ |

### Persistence, contract and safety

| Item | Status |
|------|--------|
| Additive migration; upgrade, downgrade and legacy rows verified | ✅ |
| Atomic, transactional execution; a refusal moves no money | ✅ |
| Refusals recorded with code and reason for audit | ✅ |
| Decimal money; consistent 2dp money / 4dp percentage output | ✅ |
| Existing endpoints unchanged; `POST /orders` still fills immediately | ✅ |
| **Phase 1–9 tests unchanged and passing (379 → 582 total)** | ✅ |
| **Paper trading only: no brokerage, no credentials, no real orders** | ✅ |

See [`PHASE_10_COMPLETION_REPORT.md`](PHASE_10_COMPLETION_REPORT.md) for the full report,
including 15 defects found and fixed during testing.

---

## 17. Phase 11 Completion Checklist

### Construction, allocation and sizing

| Item | Status |
|------|--------|
| Allocation config stored per portfolio, separate from risk limits | ✅ |
| Equal-weight, risk-based and max-position-cap sizing | ✅ |
| Every formula floored to whole shares and bounded by investable cash | ✅ |
| Cash reserve sizes a position down rather than refusing the buy | ✅ |
| Targets sorted signal DESC then stock_id ASC | ✅ |

### Decisions and explainability

| Item | Status |
|------|--------|
| BUY / SELL / HOLD / REDUCE / REBALANCE decisions | ✅ |
| Machine-readable `reasons` + one sentence of `explanation` each | ✅ |
| Skipped candidates reported with their own reasons | ✅ |
| Blocked decisions returned but not applied | ✅ |
| Identical input produces identical decisions | ✅ |

### Risk, rebalancing and cash

| Item | Status |
|------|--------|
| Portfolio risk report: exposure, concentration, cash, drawdown | ✅ |
| Stop-loss / take-profit as recommendations, never auto-executed | ✅ |
| Rebalance threshold suppresses rounding noise | ✅ |
| Holds reported, not dropped, from the rebalance plan | ✅ |
| Sells prefunded before buys so a plan funds its own trades | ✅ |
| Append-only cash ledger; deposits are not reported as performance | ✅ |
| Weighted-average cost, never mixed with another method | ✅ |

### Contract, persistence and compatibility

| Item | Status |
|------|--------|
| Additive migration; upgrade and downgrade verified against the ORM | ✅ |
| `null` + an explicit `insufficient_data` list for unmeasurable metrics | ✅ |
| Money 2dp, percentages 4dp, quantised as their own kind | ✅ |
| **Phase 1–10 tests unchanged and passing (582 → 757 total)** | ✅ |
| **Paper trading only: no brokerage, no credentials, no real orders** | ✅ |

See [`PHASE_11_COMPLETION_REPORT.md`](PHASE_11_COMPLETION_REPORT.md) for the full report,
including the defects found and fixed while verifying the phase.

---

## 18. Phase 12 Completion Checklist

### Limits

| Item | Status |
|------|--------|
| Daily loss limit measured against the day's opening equity | ✅ |
| Day-open resolution: snapshot before today → earliest today → invested basis | ✅ |
| Deposits never count as a gain and never reset the allowance | ✅ |
| Daily loss refuses BUYs only, never a SELL | ✅ |
| Minimum trade value on both BUY and SELL, on the slippage-adjusted gross | ✅ |
| Both new limits disabled by default; per-portfolio value wins | ✅ |
| Each new rule reports its own `rejection_code`, not the generic one | ✅ |
| One refused order reports every breached rule with its numbers | ✅ |
| Long-only stop below entry enforced in position sizing | ✅ |

### Valuation, currency and lifecycle

| Item | Status |
|------|--------|
| `POST /valuation` marks at supplied prices | ✅ |
| `GET /valuation` reads the stored marks back | ✅ |
| Portfolio currency stored, updatable and carried on every money field | ✅ |
| Existing rows backfilled with the default currency | ✅ |
| `POST /pause` halts execution and is resumable | ✅ |
| `POST /close` is terminal, idempotent and retains all history | ✅ |
| Cash movements allowed when paused, refused when closed | ✅ |
| Row lock taken before cash is read on a mutating request | ✅ |
| `as_of` accepted by risk checks and the performance report | ✅ |

### Contract, persistence and compatibility

| Item | Status |
|------|--------|
| Additive migration `d3e8f1a25b74`; up, down and up verified | ✅ |
| No existing endpoint changed shape | ✅ |
| Identical input produces identical money, counts and percentages | ✅ |
| **Phase 9–11 tests unchanged and passing (757 → 905 total)** | ✅ |
| Documentation: `docs/phase-12.md`, README, `.env.example` | ✅ |
| 64-item acceptance matrix complete (1–64) | ✅ |
| End-to-end lifecycle walkthrough over HTTP, run twice for determinism | ✅ |
| **Paper trading only: no brokerage, no credentials, no real orders** | ✅ |

See [`PHASE_12_COMPLETION_REPORT.md`](PHASE_12_COMPLETION_REPORT.md) for the full report,
including the defects found and fixed while verifying the phase.

---

## 19. Phase 13 Completion Checklist

### Position sizing

| Item | Status |
|------|--------|
| `min(risk bound, weight bound, cash bound)`, the whole-share floor | ✅ |
| Risk bound `floor(equity * risk_pct / (entry − stop))` | ✅ |
| Capital bound `floor(equity * weight_pct / entry)` | ✅ |
| Cash bound includes fees, so a sized order can always be funded | ✅ |
| Both bounds floored, never rounded up | ✅ |
| Risk at the stop never exceeds the budget (checked across stops) | ✅ |
| Position value never exceeds the weight cap | ✅ |
| `MAX_POSITION_CAP` capital-only model claims no risk bound | ✅ |
| Missing / inverted / zero stop refused, never assumed | ✅ |
| Non-positive entry, equity or cash refused explicitly | ✅ |
| Below `min_trade_value` opens **no** position, and risks nothing | ✅ |
| Existing position reported without changing the marginal size | ✅ |
| Every figure in the decision re-derivable from the explanation | ✅ |

### Allocation

| Item | Status |
|------|--------|
| Ranked by `composite_score` desc, `stock_id` asc — the Phase 11 key | ✅ |
| Input order cannot change the outcome | ✅ |
| Sequential sizing against the state the previous allocation left | ✅ |
| Adding to a held name does not consume a position slot | ✅ |
| Weight limit measured on the resulting total, not the increment | ✅ |
| `MAX_POSITION_WEIGHT_EXCEEDED` | ✅ |
| `MAX_PORTFOLIO_EXPOSURE_EXCEEDED` | ✅ |
| `MAX_POSITIONS_REACHED` | ✅ |
| `MAX_RISK_PER_TRADE_EXCEEDED` | ✅ |
| `MAX_SECTOR_WEIGHT_EXCEEDED` | ✅ |
| `MAX_TOTAL_RISK_EXCEEDED`, including risk carried by existing positions | ✅ |
| `MIN_CASH_RESERVE_BREACHED`, with an inclusive boundary | ✅ |
| `INSUFFICIENT_CAPITAL`, `INVALID_STOP_PRICE`, `INVALID_PRICE`, `INVALID_PORTFOLIO_STATE`, `UNSUPPORTED_SIDE` | ✅ |
| Every breach reported, never only the first, in a fixed order | ✅ |
| A candidate never reached is distinguished from one refused | ✅ |
| A refusal mutates nothing | ✅ |
| `apply=false` by default; `apply=true` goes through the Phase 10 order path | ✅ |
| A stock that does not exist cannot be allocated | ✅ |

### Risk state

| Item | Status |
|------|--------|
| Exposure, cash weight, invested weight, per-position weight | ✅ |
| Realised, unrealised and total P&L; return on initial capital | ✅ |
| Current drawdown from peak; worst drawdown over the equity curve | ✅ |
| Peak includes initial capital and the history, never today's equity alone | ✅ |
| Total risk to the stops of every open position | ✅ |
| Seven structured flags in a fixed evaluation order | ✅ |
| Each flag paired with rule key, limit, observed value and offending stock/sector | ✅ |
| Concentration/exposure/cash boundaries **exclusive**; drawdown boundary **inclusive** | ✅ |
| Unknown sectors excluded from sector totals rather than bucketed | ✅ |
| Non-positive equity yields `null` weights, not `0` | ✅ |
| Read-only: no mutation | ✅ |

### Portfolio backtest

| Item | Status |
|------|--------|
| Several stocks competing for one cash, one exposure ceiling, one position count | ✅ |
| Chronological: fill at open → stop check → mark → decide | ✅ |
| Sizing from last-known closes only | ✅ |
| `no_look_ahead` verified from the run's own fills, not asserted | ✅ |
| Two-path look-ahead proof: identical up to the decision, divergent after | ✅ |
| Stops from the policy, capped at the stop price rather than the day's low | ✅ |
| Open positions liquidated at the end of the window | ✅ |
| Reuses Phase 8 strategy registry, validator, cost model and metrics | ✅ |
| Writes nothing: no portfolio row, position, order, trade or snapshot | ✅ |
| Two identical calls return identical results | ✅ |

### Contract, compatibility and determinism

| Item | Status |
|------|--------|
| No migration: no column or table added | ✅ |
| No existing endpoint changed shape | ✅ |
| Every monetary figure is a `Decimal` string across HTTP, never a float | ✅ |
| Bad policy values refused by field constraints (422), not clamped | ✅ |
| `/allocate` requires at least one candidate | ✅ |
| Every engine pure: no clock, no database, no randomness | ✅ |
| **Phase 9–12 tests unchanged and passing (894 → 1113 total)** | ✅ |
| 208 new tests across five files | ✅ |
| Documentation: `docs/phase-13.md`, README, `.env.example` | ✅ |
| **Paper trading only: no brokerage, no credentials, no real orders** | ✅ |

See [`PHASE_13_COMPLETION_REPORT.md`](PHASE_13_COMPLETION_REPORT.md) for the full report,
including the defects the tests exposed and fixed.

---

## 20. Phase 15 Completion Checklist

### Exposure & concentration

| Item | Status |
|------|--------|
| Lots of one symbol aggregated into one portfolio-level exposure | ✅ |
| `total_positions` and `total_lots` both reported, so the difference is visible | ✅ |
| Every weight measured against **equity**, not invested value | ✅ |
| Equity reconciled against `cash + Σ(quantity × price)` before anything is computed | ✅ |
| Sector, strategy and asset-class exposure, with top-3/5/N ladders | ✅ |
| Unclassified sector reported with its weight, never dropped | ✅ |
| A limit with no configured value is not evaluated at all | ✅ |

### Covariance & risk metrics

| Item | Status |
|------|--------|
| Sample correlation and covariance matrices, always in sorted symbol order | ✅ |
| `w' Σ w` portfolio variance, daily volatility and annualisation | ✅ |
| Fewer than `MIN_OBSERVATIONS`, or fewer than two symbols → `DATA_INSUFFICIENT` | ✅ |
| A flat series is excluded and named, never treated as uncorrelated | ✅ |
| Non-finite observation refused rather than propagated | ✅ |
| Incomplete, asymmetric or indefinite matrix refused, not repaired | ✅ |
| `ρ' = (1 − m)ρ + m` keeps the spiked matrix PSD by construction | ✅ |
| Marginal and component risk contributions, sorted, reconciling to total volatility | ✅ |
| Reconciliation residual reported, not assumed | ✅ |
| Beta from position betas, else estimated from returns; computed even with one position | ✅ |
| Flat benchmark → beta `null`, never an arbitrary number | ✅ |
| Max drawdown kept distinct from the largest single-day loss | ✅ |
| Interpolated historical VaR; `√horizon` scaling beyond one day, stated as an approximation | ✅ |
| Expected Shortfall = worst `⌈α·n⌉` returns, not "everything below the interpolated threshold" | ✅ |

### Stress testing & liquidity

| Item | Status |
|------|--------|
| Seven scenarios, in a fixed catalogue order, each stating its assumptions | ✅ |
| Target selection deterministic (configured sector if held, else heaviest; ties alphabetical) | ✅ |
| Targeting an unheld sector → `DATA_INSUFFICIENT`, never a zero loss | ✅ |
| `NOT_APPLICABLE` when a scenario had nothing to act on, distinct from `DATA_INSUFFICIENT` | ✅ |
| Per-position contributions sum to the portfolio return | ✅ |
| Participation ratio, liquidity score and liquidation estimate from traded value | ✅ |
| No volume data → liquidity unavailable, never assumed liquid | ✅ |

### Controls, decision & adjustment

| Item | Status |
|------|--------|
| Violations ordered deterministically, each with a sentence of explanation | ✅ |
| Breaches and coverage gaps counted separately in the report | ✅ |
| `DATA_INSUFFICIENT` decision checked first: an unmeasured mandatory metric rejects | ✅ |
| A non-mandatory gap is an `EVALUATION_GAP` detail, kept in `decision_details` | ✅ |
| Reduction priority fixed: position → sector → strategy → asset class → top-N → cluster → contribution → stress → participation | ✅ |
| Each action re-evaluated against the whole measurement stack | ✅ |
| Whole shares only, rounded up so no residual breach survives | ✅ |
| Proceeds stay in cash: equity and leverage unchanged | ✅ |
| Mandatory limits never adjusted; a surviving breach rejects | ✅ |
| Non-adjustable breaches (drawdown, VaR, ES, beta without betas, conflicting counts) reported, not faked | ✅ |
| Reduction budget honoured and the stopping reason stated | ✅ |

### API, determinism & documentation

| Item | Status |
|------|--------|
| Three routes registered **before** `/{portfolio_id}`, so `"risk"` is never parsed as an id | ✅ |
| Sixteen 422 cases: unreconciled equity, negative values, unknown limits and fields | ✅ |
| Per-request `risk_config`: named limits override, omitted limits untouched, explicit `null` disables | ✅ |
| Stored route's body carries only `risk_config`; a caller cannot declare its own `portfolio_value` | ✅ |
| Persisted path: volume from stored bars, sector from the profile, no assumption when absent | ✅ |
| Every engine pure: no clock, no database, no randomness | ✅ |
| Every decimal a `Decimal` string across HTTP, never a float | ✅ |
| Analysis writes nothing back | ✅ |
| No migration: no column or table added | ✅ |
| **Phase 9–14 tests unchanged and passing (1233 → 1445 total)** | ✅ |
| 212 new tests across seven files | ✅ |
| Documentation: `docs/phase-15.md`, README, `.env.example` | ✅ |
| **Paper trading only: no brokerage, no credentials, no real orders** | ✅ |

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

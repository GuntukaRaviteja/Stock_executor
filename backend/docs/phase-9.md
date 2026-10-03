# Phase 9 — Portfolio & Paper-Trading Engine

**Paper trading / simulation only.** Virtual portfolios, virtual cash and simulated orders.
No brokerage is contacted, no credentials are stored, and no order ever leaves this process.
Every portfolio and order is labelled `trading_mode: "PAPER"` in the database and in the API
responses, and every response carries a paper-trading disclaimer.

Nothing in Phase 9 changes Phase 1–8 behaviour: the signal engine, the analytics
calculations, the screener, the backtest simulator and every existing endpoint are untouched.

---

## 1. What it does

| Capability | Where |
| --- | --- |
| Create virtual portfolios with initial capital | `services/portfolio/service.py` |
| Track cash, positions, realised/unrealised P&L and equity | `services/portfolio/` |
| Simulate market orders with fees and cash accounting | `services/portfolio/executor.py` |
| Deterministic risk controls | `services/portfolio/risk.py` |
| Mark a portfolio to market, take snapshots, report performance | `services/portfolio/service.py` |
| Turn Phase 7 signals + Phase 8 rules into paper orders | `services/portfolio/strategy_bridge.py` |

## 2. Layering (unchanged from Phase 1–8)

```
Router  (app/api/v1/portfolios.py)     HTTP only, no business logic
  ↓
Service (app/services/portfolio/)      business rules, transactions, no SQL
  ↓
Repositories (app/repositories/*_repository.py)   persistence only
  ↓
Models (app/models/portfolio.py)       SQLAlchemy, DECIMAL money columns
```

The money rules live in pure functions (`calculator.py`) and a pure executor (`executor.py`)
with no FastAPI and no database, so every calculation is unit-testable on its own.

## 3. Money model

Precision rules:

* every monetary value is a `Decimal`; a float never enters a calculation;
* rounding is delegated to the Phase 8 helpers (`quantize_money`, `quantize_percent`), so
  Phase 8 and Phase 9 cannot drift apart;
* prices are quantised to 4 dp — the stored scale — *before* they are used downstream;
* a total is rounded once, at the end.

Definitions:

| Field | Meaning |
| --- | --- |
| `realized_pnl` | booked by sells: `qty × (sell − average entry) − exit fees − allocated entry fees`; accumulates and survives a close |
| `unrealized_pnl` | gross mark to market of open shares: `qty × (price − average entry)` |
| `total_pnl` | `equity − initial_capital` — authoritative, net of every fee |
| `open_entry_fees` | entry fees already paid on shares still held |

These reconcile exactly, for every portfolio state:

```
realized_pnl + unrealized_pnl - open_entry_fees == total_pnl
```

### Cash

* BUY: `gross = qty × price`, `total_cost = gross + fees`, requires `cash ≥ total_cost`;
  after a fill `cash = cash − gross − fees`.
* SELL: requires `qty ≤ held`; after a fill `cash = cash + gross − fees`.
* Position sizing always derives the quantity from the available cash, so cash can never go
  negative.

### Fees

`TradingCostConfig` (centralised, configurable per portfolio, defaults in `.env`):

```python
commission_rate = Decimal("0.001")   # 0.1% of gross value per side
min_fee         = Decimal("0")        # floor per side
slippage_rate   = Decimal("0")        # adverse fill adjustment
```

These are simulation assumptions, not any broker's fee schedule.

## 4. Order lifecycle

`PENDING → FILLED | REJECTED` (`CANCELLED` is reserved; Phase 9 fills market orders
immediately). Market orders execute at the supplied simulation price, adjusted only by
configured slippage.

Every submission is persisted, including rejections, so order history always explains why
something did or did not execute. A rejected order never changes cash, positions or P&L — it
only adds a `REJECTED` row with a `rejection_code` and a human-readable reason.

Execution runs inside a single transaction that writes the order, the trade, the position,
the cash movement and the derived portfolio figures together. If any step fails the whole
transaction is rolled back and nothing is persisted.

### Rejection codes

| Code | HTTP | Raised when |
| --- | --- | --- |
| `PORTFOLIO_INACTIVE` | 409 | portfolio is `PAUSED` or `CLOSED` |
| `INVALID_QUANTITY` / `INVALID_PRICE` | 422 | quantity ≤ 0 or price ≤ 0 |
| `INSUFFICIENT_CASH` | 409 | a buy cannot be funded |
| `INSUFFICIENT_POSITION` | 409 | a sell exceeds the shares held, or none are held |
| `RISK_LIMIT_EXCEEDED` | 409 | a configured risk limit would be breached |

## 5. Positions

Long-only, matching Phase 8: there is no short selling, and a quantity can never go negative.

* BUY — quantity increases and the average entry price becomes the quantity-weighted average
  of the buy prices (`10 @ 100` then `10 @ 120` → `110`).
* SELL — realised P&L is booked only for the shares sold, the remaining shares keep the same
  average cost, and the row is deleted once the quantity reaches zero.
* Position growth rules (quantity, value, allocation) apply to buys. Sells are never blocked
  by them, so a portfolio can always de-risk itself.

## 6. Risk controls

All limits are optional; `null` means the rule is disabled, and each is configurable per
portfolio.

| Rule | Meaning |
| --- | --- |
| `max_position_quantity` | maximum shares in one stock |
| `max_position_value` | maximum market value of one position |
| `max_position_allocation_pct` | maximum share of portfolio equity in one position |
| `max_drawdown_pct` | blocks **new buys** once the drawdown from peak equity reaches the limit; sells stay allowed |
| insufficient cash | a buy can never commit more cash than the portfolio holds |
| oversell protection | a sell can never exceed the shares held |

Allocation and drawdown are measured against the equity *after* the fill, and the peak
includes the initial capital plus every snapshot, so a loss is measured even before the first
snapshot exists.

## 7. Strategy integration

Phase 9 **reuses** the existing signal layer rather than duplicating it:

```
Market Data → Analytics (Phase 6) → SignalEngine (Phase 7)
            → Phase7TechnicalStrategy (Phase 8 registry)
            → OrderIntent → risk validation → simulated execution → position → valuation
```

`PaperStrategyBridge` asks `SignalService` for the indicator context, hands it to the Phase 8
strategy from the registry, and translates the decision:

* `enter` → BUY, sized as a percentage of available cash (default 10%) or an explicit quantity;
* `exit` → SELL the whole open position;
* `hold` → **no order**.

Duplicate protection is deterministic and application-level: the strategy event key is
`phase9:{stock_id}:{signal_date}`, so re-running the same signal bar returns the original
order instead of executing twice. No distributed locking is involved.

## 8. Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/portfolios` | Create a paper portfolio |
| `GET` | `/api/v1/portfolios` | List portfolios |
| `GET` | `/api/v1/portfolios/{id}` | Portfolio summary |
| `PATCH` | `/api/v1/portfolios/{id}/status` | ACTIVE / PAUSED / CLOSED (`CLOSED` is terminal) |
| `GET` | `/api/v1/portfolios/{id}/positions` | Open positions with allocation |
| `POST` | `/api/v1/portfolios/{id}/orders` | Place a simulated order |
| `GET` | `/api/v1/portfolios/{id}/orders` | Order history (filter by status/stock) |
| `GET` | `/api/v1/portfolios/{id}/trades` | Trade history |
| `POST` | `/api/v1/portfolios/{id}/valuation` | Mark to market (no trades, no cash movement) |
| `POST` | `/api/v1/portfolios/{id}/snapshots` | Create an equity-curve point |
| `GET` | `/api/v1/portfolios/{id}/snapshots` | Equity curve |
| `GET` | `/api/v1/portfolios/{id}/performance` | Performance, fees, counts, drawdown |
| `POST` | `/api/v1/portfolios/{id}/strategy-orders` | Phase 7/8 strategy decision as a paper order |

## 9. Worked example

```bash
# Portfolio with ₹100,000 of virtual cash
curl -X POST localhost:8000/api/v1/portfolios \
  -H 'Content-Type: application/json' \
  -d '{"name": "Demo", "initial_capital": "100000"}'

# Buy 10 shares at ₹1,000 (fee 0.1% = ₹10)
curl -X POST localhost:8000/api/v1/portfolios/1/orders \
  -H 'Content-Type: application/json' \
  -d '{"stock_id": 1, "side": "BUY", "quantity": 10, "requested_price": "1000"}'
# cash 89,990.00 · equity 99,990.00

# Mark to ₹1,100 → gross unrealised P&L ₹1,000
curl -X POST localhost:8000/api/v1/portfolios/1/valuation \
  -H 'Content-Type: application/json' \
  -d '{"prices": [{"stock_id": 1, "price": "1100"}]}'

# Sell everything at ₹1,100 → realised P&L ₹979.00 (₹1,000 − ₹11 exit − ₹10 entry)
curl -X POST localhost:8000/api/v1/portfolios/1/orders \
  -H 'Content-Type: application/json' \
  -d '{"stock_id": 1, "side": "SELL", "quantity": 10, "requested_price": "1100"}'
# cash 100,979.00 · equity 100,979.00 · total P&L 979.00
```

## 10. Migration

```bash
alembic upgrade head   # revision c7d3e91a4f52
```

Creates `portfolios`, `portfolio_positions`, `portfolio_orders`, `portfolio_trades` and
`portfolio_snapshots`. All money columns are `DECIMAL`; no `FLOAT` is used anywhere. No
existing table is dropped or altered.

## 11. Tests

```bash
pytest tests/test_portfolio_engine.py -v   # pure engine: calculator, risk, executor
pytest tests/test_portfolio_api.py   -v    # end-to-end through the FastAPI app
pytest tests/ -q                           # full suite, including Phase 1-8 regression
```

## 12. Limitations

* Long-only; no short selling, no options, no margin, no multi-currency.
* Market orders only; `LIMIT`, `STOP` and time-in-force are not implemented.
* Fill price is whatever the caller supplies (or the stored latest close for a strategy
  order); there is no order book, queue position or partial fill.
* Snapshots are created on request — Phase 9 runs no scheduler.
* The drawdown rule uses the recorded peak equity, so it only reacts to states the engine has
  observed (initial capital, orders, valuations and snapshots).
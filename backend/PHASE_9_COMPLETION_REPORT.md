# Phase 9 Completion Report — Portfolio & Paper-Trading Engine

**Paper trading / simulation only.** No brokerage integration, no credentials, no real
orders. Every portfolio and order is persisted with `trading_mode = "PAPER"`.

---

## 1. Final Status

**COMPLETE** — all acceptance criteria met, 379/379 tests passing (250 pre-existing Phase 1–8
tests unchanged and green, plus 129 new Phase 9 tests), manual verification against MySQL
completed, no Phase 8 regression.

## 2. Architecture Changes

Layer boundaries follow the existing project exactly: `router → service → repository → model`.
No business logic in routers, no SQL in services, no calculations in repositories, and the
money rules live in pure functions testable without FastAPI or a database.

### New files

**Models**
- `app/models/portfolio.py` — `Portfolio`, `PortfolioPosition`, `PortfolioOrder`,
  `PortfolioTrade`, `PortfolioSnapshot`

**Schemas**
- `app/schemas/portfolio.py` — request/response models + `PortfolioStatus`, `OrderSide`,
  `OrderType`, `OrderStatus`, `OrderSource`, `RejectionCode`, `StrategyAction`

**Repositories** (persistence only)
- `app/repositories/portfolio_repository.py`
- `app/repositories/position_repository.py`
- `app/repositories/order_repository.py`
- `app/repositories/trade_repository.py`
- `app/repositories/portfolio_snapshot_repository.py`

**Engine** (`app/services/portfolio/`)
- `models.py` — internal dataclasses: `TradingCostConfig`, `RiskLimits`, `PositionState`,
  `PortfolioAccount`, `OrderIntent`, `ExecutionPlan`, `Rejection`, `PortfolioValuation`,
  `PortfolioPerformance`, `StrategyOrderPlan`, plus config coercion helpers
- `calculator.py` — every monetary calculation (pure, no I/O)
- `risk.py` — `RiskManager` and the deterministic risk rules
- `executor.py` — `OrderExecutor`: validation order, fills, rejections (pure)
- `defaults.py` — centralised defaults sourced from application settings
- `strategy_bridge.py` — Phase 7 signals + Phase 8 rules → paper order intent
- `service.py` — `PaperTradingService`: orchestration, transactions, persistence
- `__init__.py` — public exports

**API / wiring**
- `app/api/v1/portfolios.py` — router (13 endpoints)
- `alembic/versions/c7d3e91a4f52_phase_9_portfolio_tables.py` — migration

**Tests / docs**
- `tests/portfolio_fixtures.py`, `tests/test_portfolio_engine.py` (unit),
  `tests/test_portfolio_api.py` (integration), `docs/phase-9.md`

### Modified files (10, all additive)

| File | Change |
| --- | --- |
| `app/core/config.py` | `PAPER_TRADING_*` settings (costs, position sizing, risk defaults) |
| `app/core/exceptions.py` | Portfolio domain errors with HTTP mapping |
| `app/api/dependencies.py` | `get_paper_trading_service()` |
| `app/api/v1/__init__.py` | register `portfolios_router` |
| `app/models/__init__.py`, `app/repositories/__init__.py`, `app/schemas/__init__.py`, `app/services/__init__.py` | exports only |
| `.env.example`, `README.md` | documentation |

**No Phase 1–8 logic file was modified.** `app/services/backtest/*`,
`app/services/signal_service.py`, `app/schemas/signals.py`, `app/schemas/backtest.py`,
`app/api/v1/backtests.py`, `app/models/backtest.py` and the Phase 7/8 tests are byte-identical
(verified with `git diff --name-only`). No existing endpoint changed; no public API was removed.

## 3. Database Changes

Revision `c7d3e91a4f52` (down_revision `b512c4aa5add`). Five new tables, no existing table
dropped or altered. Applied cleanly to MySQL 8 (`alembic upgrade head`).

| Table | Purpose | Key columns |
| --- | --- | --- |
| `portfolios` | virtual account | `initial_capital`, `current_cash`, `equity`, `realized_pnl`, `unrealized_pnl`, `total_pnl`, `status`, `trading_mode`, `cost_config_json`, `risk_config_json` |
| `portfolio_positions` | open long positions | `quantity`, `average_entry_price`, `cost_basis`, `entry_fees_total`, `realized_pnl`, `current_price`, `market_value`, `unrealized_pnl` |
| `portfolio_orders` | every submission incl. rejections | `side`, `order_type`, `status`, `source`, `quantity`, `requested_price`, `execution_price`, `gross_value`, `fees`, `net_cash_impact`, `realized_pnl`, `rejection_code`, `rejection_reason`, `strategy_event_id`, `idempotency_key`, `created_at`, `executed_at` |
| `portfolio_trades` | immutable executions | `order_id` (unique), `side`, `quantity`, `execution_price`, `gross_value`, `fees`, `net_cash_impact`, `realized_pnl`, `executed_at` |
| `portfolio_snapshots` | equity curve | `timestamp`, `cash`, `market_value`, `equity`, `realized_pnl`, `unrealized_pnl`, `total_pnl`, `total_fees`, `return_pct` |

- **DECIMAL everywhere**: money `Numeric(18,2)`, prices `Numeric(18,4)`, percentages
  `Numeric(10,4)`. No `FLOAT`/`REAL` column exists.
- **Constraints**: FK `portfolio_id → portfolios.id ON DELETE CASCADE`, `stock_id → stocks.id
  ON DELETE RESTICT`, `order_id → portfolio_orders.id ON DELETE CASCADE`,
  `uq_position_portfolio_stock (portfolio_id, stock_id)`,
  `uq_order_portfolio_idempotency (portfolio_id, idempotency_key)`, unique `order_id` on trades.
- **Indexes**: `portfolio_id`, `stock_id`, `status`, `executed_at`, `created_at`,
  `strategy_event_id`, `idempotency_key`, plus composite history/curve indexes
  (`portfolio_id, created_at, id`, `portfolio_id, executed_at, id`,
  `portfolio_id, timestamp, id`, `status, created_at`).
- `alembic check` reports **no drift** for any `portfolio*` table (the drift it prints concerns
  Phase 1/3 tables and predates Phase 9).
- A downgrade path is provided and drops only the five new tables.

## 4. Portfolio Engine

Lifecycle: `ACTIVE → PAUSED → ACTIVE`, or `→ CLOSED` (terminal — reopening a closed portfolio
would silently rewrite its recorded P&L, so it is refused with 422).

A new portfolio starts flat: `current_cash = equity = initial_capital`, all P&L zero, no
positions. Cash moves only through a filled order:

- BUY: `cash = cash − gross_value − fees`
- SELL: `cash = cash + gross_value − fees`

Position sizing always derives quantity from available cash (budget %, floored to whole
shares, fees included in the unit cost), so cash can never go negative.

**Valuation** (`POST /valuation`, snapshots) updates marks only — no order, no trade, no cash
movement. It recomputes `market_value`, `equity = cash + market value`, `unrealized_pnl`,
`total_pnl = equity − initial_capital`, `return_pct` and the persisted position/portfolio
aggregates.

**Performance** adds order/trade/rejection counts, winning/losing sells, fee totals, peak
equity and drawdown. Peak equity = max(initial capital, current equity, every snapshot), and
max drawdown reuses the Phase 8 `max_drawdown()` metric so both phases measure a peak the
same way.

**Snapshots** are created on request (no background scheduler) and form the equity curve.

## 5. Order Engine

`PENDING → FILLED | REJECTED`; `CANCELLED` is modelled but unused because Phase 9 fills market
orders immediately. Fill price = requested price adjusted only by configured slippage
(adverse direction, Phase 8 semantics).

Validation runs in a fixed order so a request breaking several rules always reports the same
one: portfolio ACTIVE → quantity > 0 → price > 0 → sell covered by the position → buy funded
by cash → risk limits.

Rejections are **recorded as `REJECTED` orders** with `rejection_code` + `rejection_reason`
and then raised as a domain error, so order history always explains itself while cash,
positions and P&L stay untouched.

Execution is atomic: order, trade, position, cash and derived portfolio figures are written in
one transaction, committed together, rolled back on any failure (verified by a test that fails
mid-write and asserts a completely unchanged portfolio and zero persisted rows).

## 6. Position Engine

Long-only (matching Phase 8): no short selling, quantity can never go negative, and the row is
deleted when the quantity reaches zero.

- BUY — quantity accumulates; the average entry price becomes the quantity-weighted average of
  the buy prices: `10 @ 100` then `10 @ 120` → `110`.
- SELL — realised P&L is booked only for the shares sold; the remaining shares keep the
  **same** average cost (never reset); full exit removes the row and retains the realised P&L.

Entry fees are tracked separately from the average price, so `average_entry_price` stays a
clean average of execution prices, and are released into realised P&L pro rata when shares are
sold.

## 7. P&L Engine

| Field | Definition |
| --- | --- |
| `realized_pnl` | `qty_sold × (sell − average entry) − exit fees − allocated entry fees`; accumulates and survives a close |
| `unrealized_pnl` | gross mark to market: `qty × (current − average entry)` |
| `total_pnl` | `equity − initial_capital` — authoritative, net of all fees |
| `open_entry_fees` | entry fees paid on shares still held |
| `return_pct` | `(total_pnl / initial_capital) × 100`, safely `0` when capital is `0` |

These reconcile **exactly**, for every state:

```
realized_pnl + unrealized_pnl − open_entry_fees == total_pnl
```

Netting entry fees into realised P&L is what makes a full round trip's reported realised P&L
equal to the actual cash change — a property tested explicitly. All arithmetic is `Decimal`,
quantised once at the end through the shared Phase 8 helpers (`quantize_money`,
`quantize_percent`); a precision sweep test asserts the identity across a series of prices.

## 8. Risk Controls

All configurable per portfolio (`RiskLimitSettings`) or globally via `PAPER_TRADING_*`;
`null` disables a rule, so "no limit" stays distinguishable from "a limit of zero".

| Rule | Behaviour |
| --- | --- |
| Insufficient cash | a buy can never commit more than available cash (fees included) |
| Oversell protection | a sell never exceeds shares held; no position, no negative quantity |
| `max_position_quantity` | maximum shares in one stock |
| `max_position_value` | maximum market value of one position |
| `max_position_allocation_pct` | maximum share of portfolio equity in one position |
| `max_drawdown_pct` | blocks **new buys** once the drawdown from peak equity reaches the limit |

Growth rules apply to buys only; sells are never blocked by them, so a losing portfolio can
always de-risk itself. Allocation and drawdown are measured against the **post-fill** equity
(so fees are charged against the basis) and the peak includes the initial capital, so a loss is
visible even before the first snapshot.

## 9. Phase 7/8 Integration

Phase 9 **reuses** the existing signal layer; it duplicates no indicator, signal or strategy
rule. `PaperStrategyBridge`:

1. asks `SignalService.build_context()` (Phase 6 analytics) for the stock's indicator context;
2. hands it to the Phase 8 `Phase7TechnicalStrategy` obtained from `strategy_registry`;
3. translates the decision — `enter` → BUY sized as a % of available cash (default 10%) or an
   explicit quantity, `exit` → SELL the whole open position, `hold` → **no order**;
4. the resulting intent goes through the identical risk validation and execution path as a
   manual order.

Because the rules are the Phase 8 objects themselves, Phase 7/8 behaviour cannot drift away
from Phase 9. Duplicate protection is deterministic and application-level: the strategy event
key is `phase9:{stock_id}:{signal_date}`, so re-running the same signal bar returns the
original order instead of executing twice — no distributed locking. Manual orders accept an
`idempotency_key` with the same guarantee (unique per portfolio).

## 10. API Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/v1/portfolios` | Create a paper portfolio (201) |
| GET | `/api/v1/portfolios` | List portfolios |
| GET | `/api/v1/portfolios/{id}` | Summary: cash, equity, P&L, fees, counts |
| PATCH | `/api/v1/portfolios/{id}/status` | ACTIVE / PAUSED / CLOSED |
| GET | `/api/v1/portfolios/{id}/positions` | Open positions (+ `stock_id` filter) |
| POST | `/api/v1/portfolios/{id}/orders` | Place a simulated order (201) |
| GET | `/api/v1/portfolios/{id}/orders` | Order history (`status`, `stock_id`, paging) |
| GET | `/api/v1/portfolios/{id}/trades` | Trade history (`stock_id`, paging) |
| POST | `/api/v1/portfolios/{id}/valuation` | Mark to market |
| POST | `/api/v1/portfolios/{id}/snapshots` | Capture equity-curve point (201) |
| GET | `/api/v1/portfolios/{id}/snapshots` | Equity curve |
| GET | `/api/v1/portfolios/{id}/performance` | Performance and drawdown |
| POST | `/api/v1/portfolios/{id}/strategy-orders` | Phase 7/8 decision as a paper order (201) |

Status codes: 201 created, 200 read/update, 404 unknown portfolio/stock, 409 rejection
(`INSUFFICIENT_CASH`, `INSUFFICIENT_POSITION`, `RISK_LIMIT_EXCEEDED`,
`PORTFOLIO_INACTIVE`), 422 validation / closed-portfolio reopen.

## 11. Tests

| Suite | Count | Result |
| --- | --- | --- |
| `tests/test_portfolio_engine.py` (unit: calculator, risk, executor) | 74 | all pass |
| `tests/test_portfolio_api.py` (integration, end-to-end through FastAPI) | 55 | all pass |
| **Phase 9 total** | **129** | **all pass** |
| Phase 1–8 (pre-existing) | 250 | all pass, unmodified |
| **Total suite** | **379** | **379 passed, 0 failed** |

Coverage of the required matrix: portfolio creation; BUY (cash, position, average price,
trade, FILLED); multiple BUYS (weighted average, accumulation); SELL (quantity, cash,
realised P&L); full exit; partial exit (average preserved); insufficient cash; sell without
position; oversell; fees (calculation, cash impact, P&L); valuation (market value, unrealised,
equity); portfolio P&L and return %; all four risk limits and their rejections; portfolio
states; snapshots and equity-curve consistency; order history ordering/status/filtering; trade
history; duplicate order; transaction rollback; Decimal precision.

Integration scenarios 1–6 are implemented exactly as specified (buy→value→sell; multi-stock
allocation; signal→entry; position→exit; insufficient cash; risk exceeded), plus strategy
hold, strategy idempotency, persistence, OpenAPI/Swagger and safety checks.

## 12. Phase 8 Regression

**The full Phase 8 test suite still passes.** The complete suite was run before Phase 9
(250 passed) and after (379 passed, same 250 plus the 129 new tests). No Phase 1–8 file was
modified, so `SignalEngine`, `SignalService`, the analytics indicator calculations, the
screener, the backtest simulator, trade generation, backtest P&L and metrics, and every
existing endpoint are unchanged. Phase 9 reuses the Phase 8 `quantize_money`/`quantize_percent`
helpers and the Phase 8 `max_drawdown()` metric rather than forking them, so the two phases
cannot drift apart numerically. No Phase 8 change was needed, so no Phase 8 regression test was
added.

## 13. Manual Verification

Server started against the real MySQL database (`uvicorn`, port 8011), then verified:

| # | Check | Result |
| --- | --- | --- |
| 1 | `GET /health` | `{"status":"ok","database":"connected"}` |
| 2 | `/docs`, `/redoc`, `/openapi.json` | 200 / 200 / 200; 13 portfolio operations under the `Paper Trading` tag |
| 3 | Create portfolio ₹100,000 | cash 100000.00, equity 100000.00, all P&L 0, no positions |
| 4 | BUY 10 @ 1000 | FILLED @ 1000.0000, gross 10000.00, fee 10.00, cash 89990.00, equity 99990.00 |
| 5 | Positions | AAPL qty 10, avg 1000.0000, mv 10000.00, alloc 10.0010% |
| 6 | Valuation @ 1100 | mv 11000.00, **unrealized 1000.00**, equity 100990.00, total_pnl 990.00 |
| 7 | SELL 10 @ 1100 | FILLED, fee 11.00, **realized 979.00** (= 1000 − 11 − 10), cash 100979.00 |
| 8 | Positions after full exit | 0 open positions; realised P&L retained |
| 9 | Performance | equity 100979.00, realized 979.00, unrealized 0, total_fees 21.00, 2 orders, 2 trades, 1 winning, return 0.9790% |
| 10 | Order history | 2 orders, both FILLED, newest first |
| 11 | Trade history | 2 immutable trades with fees, cash impact and realised P&L |
| 12 | Snapshot | equity 100979.00, total_pnl 979.00, return 0.9790% |
| 13 | Equity curve | 1 snapshot returned oldest-first |
| 14 | BUY 1000 @ 1000 on ₹10,000 | 409 `INSUFFICIENT_CASH` |
| 15 | SELL with no position | 409 `INSUFFICIENT_POSITION` |
| 16 | Portfolio after rejections | cash/equity/positions unchanged; only `REJECTED` order rows added |
| 17 | Allocation cap 20%, BUY 90 @ 1000 | 409 `RISK_LIMIT_EXCEEDED` (`max_position_allocation_pct`); BUY 10 @ 1000 accepted |
| 18 | Same `idempotency_key` twice | same order id, `idempotent_replay=true`, cash and trade count unchanged |
| 19 | `PAUSED` portfolio | order rejected 409 `PORTFOLIO_INACTIVE` |
| 20 | Strategy, live MySQL data (stock 59) | action `enter`, score 26, 9 Phase 7 indicators, BUY 91 FILLED |
| 21 | Strategy re-run same signal | `hold`, no order, cash and trade count unchanged (no double execution) |
| 22 | Strategy on a bearish tape (stock 999) | action `hold`, no order placed |
| 23 | MySQL persistence | portfolios/orders/trades/positions/snapshots written as `DECIMAL`, `trading_mode=PAPER` |

The verification portfolios created in the dev database are ids 1–9 and can be deleted with
`DELETE FROM portfolios WHERE id BETWEEN 1 AND 9;` (cascades to orders, trades, positions and
snapshots).

## 14. Bugs Found and Fixed

All four were found by the new tests during implementation and each now has a regression test.

| # | Symptom | Root cause | Fix | Regression test |
| --- | --- | --- | --- | --- |
| 1 | A second BUY on the same stock started a brand-new position (quantity 10 instead of 20) instead of averaging into the existing one | `apply_buy` built the new position with `stock_id=position.stock_id if position else 0`; a flat portfolio therefore produced `stock_id=0`, which no later lookup could ever match | `apply_buy` now requires the target `stock_id` and uses it when there is no existing position | `test_two_buys_average_to_their_weighted_mean`, `test_average_is_quantity_weighted_not_arithmetic` |
| 2 | A flat portfolio buying 50% of itself was measured as a 100% position (and a real 100% buy was under-measured against a `cash`-only basis) | The allocation basis used `cash_after + account.total_market_value`; the pre-trade value of the position being increased was counted *and* the new shares were added, valuing it twice | Projected equity now replaces the position's pre-trade value with its post-fill value | `test_allocation_of_a_flat_portfolio_is_measured_against_total_equity`, `test_allocation_accounts_for_the_existing_position_being_increased` |
| 3 | Order and trade history pagination raised `AttributeError: 'ScalarResult' object has no attribute 'offset'` (500 on any page > 0) | `offset()`/`limit()` were called on the `ScalarResult` returned by `db.scalars(...)` instead of on the statement | Applied paging to the `Select` before `db.scalars()` in all five Phase 9 repositories | exercised by every history test (default and filtered pages) |
| 4 | Portfolio creation with an explicit `risk_limits` block crashed with `TypeError: BaseModel.validate() missing 1 required positional argument`; the Pydantic API models and the engine dataclasses also collided, producing `RiskLimits-Input/-Output` OpenAPI schema names | Both layers defined a class named `RiskLimits`/`TradingCostConfig`; the engine called `.validate()` on the Pydantic model, and pydantic disambiguated the duplicate names in the schema registry | The API models are now `RiskLimitSettings`/`TradingCostSettings` (distinct, documented names; wire contract unchanged) and `as_risk_limits()` / `as_trading_cost_config()` coerce either representation into the engine dataclass | `test_openapi_documents_portfolios`, all portfolio-creation and risk-limit tests |

Two further defects were corrected while writing the expectations, both in the tests rather
than the engine, and are noted for completeness: a precision expectation that dropped the
`0.01` remainder of `100000 − 99.99` (`test_decimal_precision_is_preserved_over_http`), and a
drawdown expectation that assumed full investment (`test_performance_reports_drawdown`).

## 15. Remaining Limitations

1. **Long-only.** No short selling, margin, options, multi-currency or corporate actions.
   This matches Phase 8 and is a deliberate scope choice.
2. **Market orders only.** `LIMIT`, `STOP`, stop-loss/take-profit orders and time-in-force are
   not implemented; `CANCELLED` is modelled but never produced.
3. **Simplified fill model.** The fill price is what the caller supplies (or the stored latest
   close for a strategy order). There is no order book, queue position, partial fill, market
   impact or spread beyond the optional flat slippage rate.
4. **No partial fill or multi-instrument order.** One order touches one stock.
5. **Snapshots are on-demand.** Phase 9 runs no scheduler, so the equity curve only advances
   when a snapshot is requested; intraday marks are not recorded automatically.
6. **The drawdown rule reacts to observed states only.** Its peak is the initial capital plus
   every order, valuation and snapshot the engine has processed, so an unseen intra-day low is
   invisible to it.
7. **`portfolio_orders` keeps rejected orders forever.** There is no retention/archival policy.
8. **Concurrency.** Idempotency is application-level per process (a unique index backs it), so
   two simultaneous requests racing on the same key rely on the database constraint rather than
   a lock. No distributed locking was introduced.
9. **OpenAPI naming.** A model used in both a request and a response is documented as
   `Name-Input`/`Name-Output` because `Decimal` serialises losslessly as a string while being
   accepted as a number or a string on input. Fields, enums and validation are documented
   normally.
10. **No tax or dividend modelling.** Realised P&L is pre-tax.

## 16. Final Recommendation

**Phase 9 is ready to be marked COMPLETE.**

Every item on the acceptance checklist is satisfied and verified: portfolio creation, cash
accounting, BUY/SELL execution, persisted positions/orders/trades, average entry price,
realised and unrealised P&L, total P&L and return percentage, valuation, snapshots, rejection
handling, insufficient-cash and oversell protection, risk limits, portfolio state
restrictions, duplicate-execution protection, Decimal precision, transactional execution, the
API endpoints, Swagger documentation, 129 new unit and integration tests, the 250 pre-existing
Phase 1–8 tests still passing, Phase 8 backtesting behaviour unchanged, no brokerage
integration, no secrets introduced, no critical TODOs and no known failing tests.

The two follow-ups a reviewer should consider before any real-money scope is ever
contemplated: the fill model is deliberately optimistic (no spread, no impact, no partial
fills), and the paper engine has never been exercised against live order-book conditions.
Those are simulation-fidelity questions, not correctness defects in the delivered scope.
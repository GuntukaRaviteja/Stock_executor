# Phase 10 — Risk Management and Portfolio Analytics

**Simulation only.** Nothing in this phase contacts a brokerage, stores credentials, or places
a real order. Every portfolio, order and trade is virtual.

This phase turns the Phase 9 paper-trading engine into something you can *reason about*: it
answers, before you commit to a trade, whether the trade is allowed; it tells you what size to
take; and it reports what happened afterwards in numbers you can check.

---

## 1. What this phase adds

| Area | What you can now ask |
|------|---------------------|
| Pre-trade risk | "Would this trade be accepted?" — without sending it |
| Effective limits | "Which rules are in force, and which am I already breaching?" |
| Position sizing | "How many shares should this be?" — by budget, percentage or risk |
| Valuation | "What is my exposure and how much capital is at work?" |
| Performance | P&L, return, drawdown, volatility, Sharpe, win rate, profit factor |
| Equity curve | The recorded curve, and the drawdown curve derived from it |
| Paper execution | Record an order now, fill it later, deliberately |
| Historical replay | Step through stored bars and trade them sequentially |

---

## 2. Pre-trade risk check

`POST /api/v1/portfolios/{id}/risk/check` evaluates a hypothetical trade and **changes
nothing**: no order, no trade, no cash movement, no position.

```bash
curl -X POST http://localhost:8000/api/v1/portfolios/1/risk/check \\
  -H "Content-Type: application/json" \\
  -d '{"stock_id": 1, "side": "BUY", "quantity": 300, "requested_price": "250"}'
```

```json
{
  "accepted": false,
  "rejection_code": "MAX_EXPOSURE_EXCEEDED",
  "violations": [
    {
      "rule": "max_exposure_pct",
      "reason": "Maximum portfolio exposure exceeded: this order would leave 75000.00 invested, 75.0000% of portfolio equity, above the configured 50% limit.",
      "limit": "50.0000",
      "observed": "75.0000",
      "details": {"projected_equity": "100000.00", "resulting_market_value": "75000.00"}
    }
  ],
  "equity": "100000.00",
  "cash": "100000.00",
  "exposure_pct": "0.0000",
  "position_count": 0
}
```

Two properties matter here:

* **Every breached rule is reported, not just the first.** A trade that is over both its
  exposure and its cash-reserve limit comes back with both, so one call is enough.
* **`rejection_code` names the cause.** It is a stable enum value, so a client can branch on
  it without parsing prose.

The projection the check reports is computed by the same code the executor uses before it
fills, so the numbers here are by construction the numbers that would apply.

---

## 3. Risk rules

| Limit | Blocks | Boundary |
|-------|--------|----------|
| `max_drawdown_pct` | BUY once equity is at or beyond this far below peak equity | `>=` |
| `max_positions` | BUY that would open a position beyond this count | `>=` |
| `max_position_quantity` | BUY beyond this many shares in one stock | `>` |
| `max_position_value` | BUY giving one stock more than this market value | `>` |
| `max_position_allocation_pct` | BUY making one stock more than this share of equity | `>` |
| `max_exposure_pct` | BUY leaving more than this share of equity invested | `>` |
| `max_trade_allocation_pct` | BUY committing more than this share of equity in one order | `>` |
| `min_cash_reserve_pct` | BUY leaving less cash than this share of equity | `<` |

The drawdown guard is `>=` because hitting the limit exactly is the limit being reached. Every
other limit is `>`, so a value exactly at the limit is still accepted — a portfolio with
`max_exposure_pct: 50` may hold exactly 50%.

**SELL orders are never blocked by a growth limit.** Selling always reduces risk; only
oversell protection (`INSUFFICIENT_POSITION`) and an inactive portfolio apply.

Limits live per portfolio in `portfolios.risk_config_json`, falling back to the
`PAPER_TRADING_*` application defaults. An unset limit is disabled, not defaulted to an
arbitrary cap.

---

## 4. Position sizing

`POST /api/v1/portfolios/{id}/position-size` derives a quantity and places no order.

| Method | Formula | Example (100,000 equity) |
|--------|---------|-------------------------|
| `FIXED_CAPITAL` | `floor(capital / unit_cost)` | 10,000 at 250 → **40** |
| `PERCENTAGE` | `floor(equity × pct / unit_cost)` | 5% at 250 → **20** |
| `RISK_BASED` | `floor(equity × risk_pct / \|entry − stop\|)`, capped by available cash | 1% risk, entry 100, stop 95 → **200** |

Three details worth knowing:

* **Quantities are floored, never rounded.** 10,000 at 300 buys 33 shares, not 34.
* **Fees are included in the unit cost.** The same 10,000 at 100 with 0.1% fees buys 9
  shares, because 10 would cost 1,010.
* **Risk-based sizing is a risk budget, not a cash budget.** The example risks 1,000 to make
  200 shares worth 20,000. The optional cash cap is what keeps the order fundable.

A stop equal to the entry price is refused: it defines no risk per share, so there is no
defensible size to derive.

---

## 5. Deferred execution

By default `POST /orders` fills immediately. Pass `defer_execution: true` to record the order
as `PENDING` without touching cash, then fill it deliberately:

```bash
# Record only — cash is untouched
curl -X POST http://localhost:8000/api/v1/portfolios/1/orders \\
  -H "Content-Type: application/json" \\
  -d '{"stock_id": 1, "side": "BUY", "quantity": 100, "requested_price": "250",
       "defer_execution": true}'

# Fill it later
curl -X POST http://localhost:8000/api/v1/portfolios/1/orders/42/execute
```

The fill is **re-validated from scratch**. A position may have closed, or cash may have been
spent, in between — in which case the order is refused with `INSUFFICIENT_CASH` rather than
filled at a price the portfolio can no longer afford. Executing an already-settled order is a
conflict (`409 ORDER_NOT_EXECUTABLE`), not a silent no-op.

---

## 6. Valuation and exposure

```
equity           = cash + Σ(position quantity × current price)
invested_capital = Σ cost basis of open positions
exposure_pct     = market_value / equity × 100
position_weight  = position market_value / equity × 100
```

`exposure_pct` is `null`, not zero, when equity is not positive — a percentage of nothing is
not 0%.

Two positions of 10,000 each against 100,000 of capital is 20% exposure, and the two position
weights (10% + 10%) sum to it.

---

## 7. Performance reporting

`GET /api/v1/portfolios/{id}/performance` reports realised and unrealised P&L, total P&L,
return, drawdown, exposure, trade statistics, volatility and Sharpe.

**A metric that cannot be computed is `null`, never a misleading `0`.**

| Metric | `null` when |
|--------|-------------|
| `win_rate_pct` | no closed round trips |
| `profit_factor` | no losses (the denominator is zero) or no trades |
| `average_winning_trade` / `average_losing_trade` | no wins / no losses |
| `annualized_volatility_pct` | fewer than two daily returns |
| `sharpe_ratio` | fewer than two returns, or zero volatility |

Two returns are required because volatility and Sharpe are defined over a *series*: one
observation carries no dispersion. Intraday snapshots on the same day count as one
observation, not several, so marking to market repeatedly in a day does not manufacture
volatility.

Daily returns are computed between consecutive **days** only. A snapshot's equity is the
portfolio's mark, and marks are not independent observations.

### Drawdown

```
drawdown_pct(point) = (point.equity − running peak) × 100 / running peak
```

The curve carries the **signed** value (`0` at a peak, negative below it). The response also
reports the **magnitude**: `max_drawdown_pct` is positive and matches the Phase 8 backtest
metric of the same name, so a portfolio drawdown and a backtest drawdown are the same number.

For the sequence 100,000 → 110,000 → 105,000 → 90,000 → 95,000:
peak **110,000**, maximum drawdown **20,000**, maximum drawdown **18.1818%**.

The equity curve, the drawdown curve and the risk engine all read the same persisted
`peak_equity` / `drawdown_pct` / `exposure_pct` columns, rather than each re-deriving them and
disagreeing.

---

## 8. Historical replay

`POST /api/v1/portfolios/{id}/simulate` steps through one stock's stored bars in order:

1. **execute** the decision taken on the previous bar, at this bar's open;
2. **mark** the position to this bar's close and record an equity point;
3. **decide** from this bar's close only, producing the decision for the next bar.

Every fill goes through the same validation, risk check, position update and atomic
persistence path as a manual order. The engine adds timing, not a second set of rules.

### Look-ahead prevention

Two independent guards:

* **Execution** — under the `OPEN` policy the execution bar must be strictly later than the
  signal bar. Under `CLOSE` the fill is the signal bar's own close, which is already known at
  decision time.
* **Indicators** — the context for bar *N* is built by the Phase 8
  `PointInTimeContextBuilder`, which by construction contains nothing from bar *N+1* onward.
  Phase 10 reuses that replay rather than recomputing indicators over the whole series.

Every simulated order records **both** `signal_date` and `executed_at`, so the guarantee is
verifiable from the order history rather than taken on trust. The regression tests assert that
`signal_date < executed_at` for every order, and that no fill price ever equals a price from a
bar other than its own.

### Execution policies

| Policy | Signal | Fill |
|--------|--------|------|
| `OPEN` (default) | close of bar N | open of bar N+1 |
| `CLOSE` | close of bar N | close of bar N |

`OPEN` is the Phase 8 convention and is the default. `CLOSE` executes a bar earlier and pays
no overnight gap. The engine never silently switches between them.

### When a run trades nothing

A run over too little history produces no orders — that is correct, not an error. The response
reports `bars_processed` and `skipped_bars`, so "no orders" is explained. A series below the
minimum bar count is a data problem and is refused with `422` and
`details.reason = "insufficient_bars"`.

---

## 9. Rejections are auditable

A refused order is **recorded** with its code and reason, then rejected. That is deliberate:
a refusal you cannot see later is a silent failure.

What does *not* change on a refusal: cash, positions, trades, market value, equity, realised
and unrealised P&L. The only permitted change is one more row in the order history.

```
409  INSUFFICIENT_CASH
{
  "error": {
    "code": "INSUFFICIENT_CASH",
    "message": "Insufficient cash: the order requires 90090.00 but the portfolio has 10000.00 available.",
    "details": {
      "required_cash": "90090.00",
      "available_cash": "10000.00",
      "rule": "insufficient_cash_protection"
    }
  }
}
```

---

## 10. Money, precision and atomicity

* Every monetary figure is `Decimal`, quantized to 2 decimals; percentages to 4.
* A percentage serialises identically whether or not it is zero (`"0.0000"`, never `"0"`), so
  a client never has to special-case a zero.
* A fill price is money and carries 2 decimals like everything else.
* One order is one transaction: the order, trade, position, cash and P&L are written together
  or not at all.

---

## 11. Configuration

Application-wide defaults; per-portfolio limits always win.

| Variable | Meaning |
|----------|---------|
| `PAPER_TRADING_MAX_EXPOSURE_PCT` | Share of equity that may be invested |
| `PAPER_TRADING_MAX_TRADE_ALLOCATION_PCT` | Share of equity one trade may commit |
| `PAPER_TRADING_MIN_CASH_RESERVE_PCT` | Share of equity that must stay in cash |
| `PAPER_TRADING_MAX_POSITIONS` | Maximum stocks held at once |
| `PAPER_TRADING_EXECUTION_PRICE_POLICY` | `OPEN` or `CLOSE` |
| `PAPER_TRADING_RISK_FREE_RATE_PCT` | Annual risk-free rate for Sharpe |

---

## 12. Out of scope

Stated explicitly so nothing here is mistaken for a missing feature:

* No brokerage integration, no credentials, no live orders.
* No leverage, margin or short selling. Long-only: a position can never go negative.
* No order book, no partial fills. An order fills completely or is rejected.
* No dividends, corporate actions or borrow costs.
* No background scheduler. Snapshots are recorded when you ask for them.
* No tax accounting.

---

## 13. Migration

```bash
make migrate          # or: alembic upgrade head
```

Adds six columns, all nullable or defaulted, so existing portfolios, orders and snapshots are
untouched and historical rows keep working:

| Table | Column |
|-------|--------|
| `portfolio_orders` | `signal_as_of`, `execution_price_policy`, `metadata_json` |
| `portfolio_snapshots` | `drawdown_pct`, `exposure_pct`, `peak_equity` |

Reversible with `alembic downgrade -1`.

---

**Not investment advice.** Results are a deterministic simulation of a fixed rule set. Past
simulated performance does not predict future outcomes.

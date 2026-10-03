# Phase 11 — Portfolio Construction & Risk Management

> **Simulation only.** No brokerage is contacted, no credentials are stored, no real order is ever
> placed. Everything here is a deterministic decision about a simulated portfolio. Nothing in this
> document is investment advice.

## 1. What this phase adds

Phase 9 and 10 built a portfolio that can hold positions, enforce per-order risk limits and report
on itself. Phase 11 adds the layer that *decides what the portfolio should hold*.

```
Phase 7 screener ──┐
Phase 7 signals  ──┼──► candidates ──► PortfolioConstructionService
explicit prices  ──┘                        │
                                             ├─► PortfolioDecision  (BUY / SELL / HOLD / REDUCE)
                                             ├─► RebalancePlan      (drift vs target weights)
                                             └─► PortfolioRiskReport (what the portfolio is exposed to)
                                                        │
                                                        ▼
                                        PaperTradingService  ← the only writer of portfolio state
                                                        │
                                                        ▼
                                          positions · trades · ledger · snapshots
```

The separation is deliberate and is the point of the phase:

* **The screener decides which stocks qualify.** The portfolio layer decides *how much* to hold.
  Screening rules are not duplicated here.
* **The signal engine produces signals.** No indicator logic moved into the portfolio package.
* **`PaperTradingService` remains the only writer of cash, positions and trades.** The
  construction engine is pure; it emits decisions and the existing order path executes them.

## 2. Architecture

| Layer | Module | Depends on |
|-------|--------|------------|
| Pure math | `allocation.py` | `Decimal` and the Phase 8 quantisation helpers only |
| Pure construction | `construction.py` | `allocation.py`, engine dataclasses — no FastAPI, no session |
| Pure rebalancing | `rebalancing.py` | same |
| Pure risk report | `risk_engine.py` | same |
| Orchestration | `construction_service.py` | the pure engines + repositories + screener |
| Persistence | `portfolio_transaction_repository.py` | SQLAlchemy |
| HTTP | `api/v1/portfolios.py` | Pydantic schemas + services |

Every percentage arrives as an argument. There are no strategy constants inside the engine, which
is what lets a future strategy supply a different configuration without touching the arithmetic.

## 3. Portfolio lifecycle

```
                create                     activate
   (none) ──────────────► ACTIVE ─────────────────────────► PAUSED
                            │  ▲                                │
       deposit / withdraw    │  │ deposit / withdraw / rebalance│
              │              │  └──────────────────────────────┘
              ▼              ▼
            ACTIVE ◄──────────┘
                            │
                          close
                            ▼
                         CLOSED
```

A `PAUSED` portfolio still takes deposits and withdrawals — those are capital movements, not
trades. `CLOSED` is terminal.

## 4. Configuration: two columns, two questions

| Column | Question it answers | Enforcement |
|--------|--------------------|-------------|
| `risk_config_json` (Phase 10) | What may a *single order* never breach? | Hard pre-trade gate. A breach rejects the order. |
| `allocation_config_json` (Phase 11) | How should the portfolio be *built*? | A policy. It proposes; it does not veto. |

They are kept apart on purpose. Merging them would make "the limits" and "the strategy" one blob,
and a replacement write to one would silently reset the other.

```jsonc
{
  "sizing_method": "EQUAL_WEIGHT",      // EQUAL_WEIGHT | RISK_BASED | MAX_POSITION_CAP
  "max_position_weight_pct": "20",
  "max_sector_weight_pct": null,        // null = no sector cap
  "max_positions": null,                // null = no count cap
  "max_exposure_pct": null,             // null = no exposure cap
  "max_position_loss_pct": null,
  "cash_reserve_pct": "10",
  "stop_loss_pct": null,
  "take_profit_pct": null,
  "risk_per_trade_pct": "1",
  "rebalance_threshold_pct": "0.5",
  "min_position_value": null
}
```

Defaults come from `app/core/config.py` (`PORTFOLIO_*`) and are frozen into the portfolio's column
on first use, so a portfolio's policy does not drift when a deployment setting changes. A portfolio
that has never stored one reports the application defaults rather than a set of nulls.

## 5. Position sizing

Three formulas. All of them produce **whole shares, floored** — buying one share more than a target
is how a position silently breaches its own cap. All of them are bounded by investable cash.

### 5.1 Equal weight

```
target_weight = min(100 / N, max_position_weight_pct)
quantity      = floor(equity * target_weight / 100 / (price * (1 + commission)))
```

The cap is applied **after** the split, never before. Two candidates split to 50% each, which the
20% cap reduces to 20% each — the remaining 60% simply stays unallocated. That is correct, not a
special case.

### 5.2 Risk-based

```
risk_budget      = equity * risk_per_trade_pct / 100
per_share_risk   = price - stop_price          # stop_price = price * (1 - stop_loss_pct/100)
quantity         = floor(risk_budget / per_share_risk)
```

A risk budget is **not** a cash budget: risking 1% of 100,000 through a 5% stop is 200 shares,
which costs far more than the 1,000 the budget figure suggests. The result is therefore also
capped by what the portfolio can actually pay for.

Returns `0` when no stop is configured — without one there is no per-share risk and no defensible
size.

### 5.3 Maximum position cap

Take the weight-derived quantity and cap it at `max_position_weight_pct`, then bound it by
investable cash.

### 5.4 The cash reserve bounds size, it does not refuse

Every formula is additionally bounded by `equity - reserve`. Sizing *down* to what is fundable is
deliberate: the reserve is a self-imposed budget, not a prohibition, and "hold 50 shares because
5,000 is investable" is a better answer than "you may not buy the 80 shares the target wanted".

A *risk* limit is the opposite case and does refuse — a risk limit is a boundary, not a budget.

## 6. Portfolio construction — decisions

`POST /portfolios/{id}/decisions` returns a verdict for every relevant stock. Nothing is written
unless `apply=true` is passed.

| Action | Meaning |
|--------|---------|
| `BUY` | Open or add to a position |
| `SELL` | Close the position entirely |
| `REDUCE` | Trim the surplus, keep the position |
| `HOLD` | Examined, no trade needed |

The order of precedence for a held stock is fixed:

1. **No usable price** → `HOLD`, `no_price`. Nothing can be sized.
2. **The risk engine recommends an action** (stop-loss, take-profit, position-loss limit,
   concentration, drawdown) → `SELL`/`REDUCE`, citing the rule that fired. A held position answers
   to the risk engine before it answers to the allocation.
3. **Over the position cap** with drift past the threshold → `REDUCE`.
4. **No target weight** → `HOLD`, `no_target`.
5. **Target value below the minimum** → `HOLD`, `below_min_position_value`. No position is opened
   rather than opening one too small to be meaningful.
6. **Drift inside the threshold** → `HOLD`, `below_rebalance_threshold` or `at_target_weight`.
7. Otherwise → `BUY` or `REDUCE` by the sign of the required quantity.

Skipped candidates are reported in a separate `skipped` list with `eligible: false` and their own
reasons, so "we looked and could not size this" is distinguishable from "we never considered it".

### 6.1 Explainability

Every decision carries `reasons` — stable machine-readable codes — and `explanation`, with exactly
one sentence per code:

```json
{
  "stock_id": 123,
  "action": "BUY",
  "target_weight_pct": "20.0000",
  "target_quantity": 20,
  "current_quantity": 0,
  "required_quantity": 20,
  "reasons": [
    "equal_weight_allocation",
    "position_cap_applied",
    "eligible_candidate",
    "underweight"
  ],
  "explanation": [
    "An equal-weight split targets 20.0000% of equity for each eligible name, so 20 share(s) must be bought to reach it.",
    "The single-position cap of 20% applies, so the target is capped at 20.0000%.",
    "Stock 123 is an eligible candidate with a target weight of 20.0000% of 100000.00 equity (20000.00), which is 20 share(s) at 1000.",
    "The portfolio holds no position in stock 123 (0% of equity), below the 20.0000% target."
  ]
}
```

There is no opaque score anywhere in the engine. Every number in a decision is either a target the
caller supplied or a formula applied to one.

### 6.2 Blocking

A decision can be proposed and still be refused. `blocked` is `true`, `applied` is `false`, and the
blocking reason is appended to the same `reasons` list:

| Code | Meaning |
|------|---------|
| `insufficient_cash` | The target works out to no shares even after the reserve is held back |
| `cash_reserve_reached` | The trade would commit equity that is not already invested |
| `risk_limit_blocked` | The pre-trade risk engine refused it |
| `below_min_position_value` | Target value too small to open |

### 6.3 Determinism

* Candidates sort by signal score descending, then `stock_id` ascending.
* Decisions report in `stock_id` order.
* No function in `allocation.py`, `construction.py`, `rebalancing.py` or `risk_engine.py` reads the
  clock, the network, the database or any global mutable state.

## 7. Risk management

`GET /portfolios/{id}/risk/report` reports what the portfolio *is*, as opposed to what an order
would be allowed to do.

### 7.1 Rules

| Rule | Reported as |
|------|-------------|
| `max_position_weight_pct` — one position exceeds its cap | violation |
| `max_sector_weight_pct` — one sector exceeds its cap | violation |
| `max_positions` — the position count reaches the cap | violation |
| `max_exposure_pct` — invested market value exceeds its share of equity | violation |
| `min_cash_reserve_pct` — cash has fallen below the reserve | violation |
| `max_drawdown_pct` — drawdown from peak equity reaches the limit | violation |
| `stop_loss_pct` — a position has fallen to or through its stop | recommendation |
| `take_profit_pct` — a position has risen to or through its target | recommendation |
| `max_position_loss_pct` — a position's loss exceeds the tolerance | recommendation |

Every breached rule is reported, not just the first — a report that named one would hide the rest.

A **violation** is a limit the portfolio is outside of: a state to be aware of. A
**recommendation** is a level to *act* at: an exit the caller may choose to take. A portfolio whose
stop has been hit is not "in breach" — it has a decision to make, which is why stop-loss and
take-profit appear only as recommendations, with a quantity and a price.

### 7.2 Trigger semantics

* **Stop-loss and take-profit are inclusive.** Sitting exactly on the level counts as triggered,
  because the level is where the position is no longer being tolerated.
* **Position loss is exclusive.** A loss of exactly the limit is still inside the limit, because
  that limit is a tolerance, not a trigger.
* **Growth limits never block a SELL.** Selling always reduces risk.

### 7.3 Exposure

For a long-only portfolio gross exposure equals net exposure equals invested market value over
equity. Both are reported separately so a future long/short portfolio fills in the short term
only, without touching the readers.

### 7.4 Recommendations are returned, never executed

The engine produces `recommendations` — `SELL` and `REDUCE` actions with a quantity and a reason.
They are handed to the construction engine, which turns them into decisions. Nothing is executed
until `apply=true` is passed to `/decisions` or `/rebalance`. That is the whole point of separating
the risk engine from the executor.

## 8. Rebalancing

`POST /portfolios/{id}/rebalance` compares current weights with targets.

```
weight_difference = current_weight - target_weight
|weight_difference| < rebalance_threshold_pct  →  HOLD, below_threshold = true
```

The threshold is what stops rounding noise from producing transactions. The specification's own
examples behave as written: 9.7% against a 10% target is a 0.3% drift and holds under a 0.5%
threshold; 8.9% is a 1.1% drift and trades.

A plan returns an item for **every target and every held position, including the holds**. A
rebalance that returns five items, three of them "no trade needed", is a complete answer; one that
returns only the trades cannot be told apart from a run that never looked.

`drop_unlisted=true` treats a held stock with no target as a target of zero, which proposes
closing it.

### 8.1 Execution order

Items are *reported* in stock-id order, but they are *funded* in a different order: sells first,
largest proceeds first, then buys, smallest cost first, with `stock_id` breaking every tie. A
rebalance that buys before it sells would report itself unfundable for cash it had just committed
to raise, which is a fiction.

Every proposed trade is preflighted against the Phase 10 risk engine before it is reported, so the
plan says which trades are actually executable and why the others are not. An accepted sell adds
its net proceeds to the budget the following buys are checked against.

### 8.2 Applying

`apply=true` submits the plan as simulated orders through the existing order path, which re-runs
the full validation and risk check against live state. A rejection there is reported on the item
rather than raised, so one unfundable proposal does not abandon the rest of the plan.

## 9. Cash and the transaction ledger

```
POST /portfolios/{id}/deposit     {"amount": "2500", "reason": "..."}
POST /portfolios/{id}/withdraw    {"amount": "2000", "reason": "..."}
GET  /portfolios/{id}/transactions
GET  /portfolios/{id}/transactions/summary
```

| Movement | Cash |
|----------|------|
| `BUY` | −(quantity × price + fees + slippage) |
| `SELL` | +(quantity × price − fees − slippage) |
| `DEPOSIT` | +amount |
| `WITHDRAWAL` | −amount |

The ledger is append-only: one row per movement of `current_cash`, written in the same transaction
as the change that caused it. `portfolio_trades` stays what it was — the record of an *execution* —
while the ledger answers "how did the cash balance get to where it is". The two must agree, which
is why every fill writes both or neither.

A withdrawal that would take cash below zero is refused before anything is written.

### 9.1 Deposits are not performance

The P&L basis is opening capital **plus external flows**. A deposit raises equity without raising
invested capital, so reporting it as a gain would be wrong. A 10% gain followed by a deposit that
doubles the capital still reports a 10% return.

## 10. Realized and unrealized P&L

Weighted-average cost, and only weighted-average cost — the accounting method is never mixed.

```
cost_basis_of_sold = average_entry_price × quantity_sold
realized_pnl       = sell_proceeds − cost_basis_of_sold − transaction_costs
unrealized_pnl     = (current_price − average_entry_price) × open_quantity
```

Entry fees are capitalised into the position's cost basis; exit fees are expensed against the
realized result. Partial sells keep the average entry price unchanged.

## 11. Performance metrics

`GET /portfolios/{id}/performance` reports capital, equity, total and realised and unrealised P&L,
cash, invested capital, exposure, maximum drawdown, position and transaction counts, winning and
losing trades, win rate, average gain, average loss, profit factor, and — when there is enough
history — volatility, Sharpe, Sortino and CAGR.

**Insufficient data is reported, never filled in.** A metric that cannot be computed is `null` and
the response carries an explicit `insufficient_data` list naming the metrics and saying why:

```json
{
  "sharpe_ratio": null,
  "sortino_ratio": null,
  "cagr_pct": null,
  "insufficient_data": [
    "sharpe_ratio: fewer than two recorded returns, so there is no dispersion to divide by"
  ]
}
```

## 12. API endpoints

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/portfolios` | Create |
| GET | `/portfolios` | List |
| GET | `/portfolios/{id}` | Summary with positions |
| PATCH | `/portfolios/{id}` | Rename, reconfigure, change status |
| POST | `/portfolios/{id}/deposit` | Add capital |
| POST | `/portfolios/{id}/withdraw` | Remove capital |
| GET | `/portfolios/{id}/positions` | Open positions |
| GET | `/portfolios/{id}/transactions` | Cash ledger |
| GET | `/portfolios/{id}/transactions/summary` | Counts per type |
| POST | `/portfolios/{id}/valuation` | Re-mark to supplied prices |
| POST | `/portfolios/{id}/snapshots` | Persist an equity snapshot |
| GET | `/portfolios/{id}/performance` | Metrics |
| GET | `/portfolios/{id}/risk/report` | Portfolio-level risk |
| POST | `/portfolios/{id}/rebalance` | Plan, and optionally apply |
| POST | `/portfolios/{id}/decisions` | Plan, and optionally apply |

Money and percentages are serialised as decimal strings. A client never has to guess whether
`0` means zero or "not measured".

## 13. End-to-end example

Five candidates, ₹100,000, 20% position cap, 1% risk per trade, 10% cash reserve, no fees.

```bash
PORTFOLIO='{"name":"Demo","initial_capital":"100000.00",
  "trading_costs":{"commission_rate":"0","slippage_rate":"0","min_fee":"0"},
  "allocation_config":{
    "sizing_method":"EQUAL_WEIGHT",
    "max_position_weight_pct":"20",
    "cash_reserve_pct":"10",
    "risk_per_trade_pct":"1",
    "stop_loss_pct":"8"}}
curl -X POST localhost:8000/api/v1/portfolios -H 'content-type: application/json' -d "$PORTFOLIO"
```

### Candidate selection

```bash
curl -X POST localhost:8000/api/v1/portfolios/1/decisions -H 'content-type: application/json' -d '{
  "candidates":[
    {"stock_id":101,"price":"1000"},
    {"stock_id":102,"price":"250"},
    {"stock_id":103,"price":"80"},
    {"stock_id":104,"price":"1450"},
    {"stock_id":105,"price":"310"}
  ],
  "apply": false}'
```

Five candidates would split to 20% each, which is exactly the cap, so no candidate is truncated.
What the *cash* allows is a different question, and the engine answers it per candidate in stock-id
order:

| Stock | Price | Target weight | Target value | Target quantity |
|-------|-------|---------------|--------------|-----------------|
| 101 | 1000 | 20.0000% | 20,000.00 | 20 |
| 102 | 250 | 20.0000% | 20,000.00 | 80 |
| 103 | 80 | 20.0000% | 20,000.00 | 250 |
| 104 | 1450 | 20.0000% | 20,000.00 | 13 |
| 105 | 310 | 20.0000% | 20,000.00 | **35** |

Two rows deserve a note.

**104 is floored to 13 shares, not rounded to 14.** 14 would be 20,300.00, or 20.3% of equity —
above the cap it was sized against.

**105 gets 35 shares, not the 64 its 20% target implies.** The first four commits 78,850 of the
90,000 investable (the other 10,000 is the reserve), leaving 11,150, which buys 35 shares at 310.
The engine sizes the position to what the portfolio can fund rather than refusing the name — and
the next rebalance buys the rest.

### Apply the buys

Re-run with `"apply": true`. Every decision becomes a simulated order through the existing order
path:

| After applying | Value |
|----------------|-------|
| Cash | 10,300.00 (the 10,000 reserve plus 300 spare) |
| Positions | 5 |
| Market value | 89,700.00 |
| Equity | 100,000.00 |

### Rebalance

```bash
curl -X POST localhost:8000/api/v1/portfolios/1/rebalance -H 'content-type: application/json' -d '{
  "target_weights":{"101":"10","102":"20","103":"20","104":"20","105":"20"},
  "apply": false}'
```

Run against the original marks, 102–104 sit at exactly 20% and drift to zero:

| Stock | Action | Side | Required |
|-------|--------|------|----------|
| 101 | REBALANCE | SELL | −10 (20% → 10%) |
| 102 | HOLD | — | 0 |
| 103 | HOLD | — | 0 |
| 104 | HOLD | — | 0 |
| 105 | REBALANCE | BUY | +29 (11% → 20%) |

Both trades are executable, and only because the 10,000 the sell releases is what funds the 8,990
the buy needs. A plan that bought before it sold would report itself unfundable for cash it had
just committed to raise. The three holds are reported rather than dropped: "checked and correct"
is a result.

### Re-mark and check unrealized P&L

```bash
curl -X POST localhost:8000/api/v1/portfolios/1/valuation -H 'content-type: application/json' -d '{
  "prices":[{"stock_id":101,"price":"1120"},{"stock_id":104,"price":"1380"}]}'
```

Only the two supplied prices move; the other three keep their last mark. 101 now carries +2,400.00
unrealized and 104 carries −910.00, so equity is 101,490.00.

### Sell part of a position

```bash
curl -X POST localhost:8000/api/v1/portfolios/1/orders -H 'content-type: application/json' -d '{
  "stock_id": 101, "side": "SELL", "quantity": 5, "requested_price": "1120"}'
```

5 × 1120 proceeds against 5 × 1000 of cost basis, with no fees, books 600.00 realized. 15 shares
remain, and the average entry price is unchanged.

### Trigger a risk violation

Send 101 to 910. Its stop is 8% below the 1000 entry, so 920 is the trigger and 910 is through it:

```bash
curl 'localhost:8000/api/v1/portfolios/1/risk/report?prices=101=910'
```

`recommendations` now carries a `SELL` of the remaining 15 shares citing `stop_loss_pct`, with the
stop price, the current price and the unrealized P&L in its details. `violations` does *not* list
it: a crossed stop is a level to act at, not a limit the portfolio is outside of. Nothing has been
sold — a recommendation is not an execution.

`POST /decisions` with the same price turns that recommendation into a `SELL` decision citing the
same rule, and only `apply: true` would submit it.

None of these identifiers are recommendations. They are placeholders for arithmetic.

The whole walkthrough runs as a test: `tests/test_portfolio_end_to_end.py`.

## 14. Money, precision and rounding

All monetary arithmetic is `Decimal`. Four precisions, each applied deliberately:

| Kind | Helper | Precision |
|------|--------|-----------|
| Money | `quantize_money` | 2 dp |
| Percentage | `quantize_percent` | 4 dp |
| Price | `quantize_price` | 4 dp |
| Quantity | integer, floored | — |

Percentages are quantised as percentages, never as money. Quantising `12.3456%` as money would
silently drop it to `12.35%` — and would print the same number with different precision in the
explanation than in the field beside it.

Totals are rounded once, at the end.

## 15. Out of scope

* **No real-money execution.** No broker is contacted. `PaperTradingService` is the only writer.
* **No short selling.** Long-only; gross and net exposure are reported separately so the short term
  can be filled in later.
* **No tax accounting.** Realised P&L is pre-tax.
* **No order book, no partial fills, no slippage model.** Fill prices come from the Phase 10
  execution policy.
* **No portfolio-level optimisation.** Targets come from a formula or from the caller. There is no
  mean-variance or risk-parity solver.

## 16. Migration

`f5a1c7d94b20_phase_11_allocation_and_cash_ledger.py`, down revision `e4f8b2c7a913`. Purely
additive: a nullable column on `portfolios` and one new table. No Phase 1–10 table, column or row is
dropped, renamed or retyped. Verified in both directions against the ORM schema.
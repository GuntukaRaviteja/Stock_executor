# Phase 13 — Portfolio & Risk Management Engine

> **Simulation only.** No brokerage is contacted, no credentials are stored, no real order is ever
> placed. Everything here is a deterministic rule applied to a simulated portfolio. Nothing in this
> document is investment advice.

## 1. What this phase adds

Phase 9–12 built a portfolio that holds positions, enforces per-order risk limits, builds a target
allocation, replays history, and explains itself. Four gaps remained:

1. **No multi-candidate allocation.** The Phase 11 construction engine could build a *target
   allocation* from signals, but there was no endpoint that takes a list of simultaneous candidate
   signals and returns a decision for each one, sized and ordered, competing for the same cash.
2. **Position sizing could not see a stop.** The Phase 12 sizing model was risk-*budget* based, but
   it floored against available cash only. The textbook formula —
   `min(floor(risk_budget / (entry − stop)), floor(max_position_value / entry))` — was not
   implemented anywhere, so there was no way to answer *"how many shares puts exactly 1% of equity
   at risk between here and the stop?"*
3. **Risk was reported as prose.** `GET /risk/report` returns violations as sentences. A client that
   had to parse English to know *which* limit was breached, *by how much*, and *on which rule* was
   not getting a machine-readable answer.
4. **Backtests were single-stock.** The Phase 8 engine replays one instrument. A portfolio's risk
   only exists when several instruments compete for one cash balance.

Phase 13 closes all four, and it does so by **reusing** everything already built: the Phase 11
ranking key, the Phase 7 point-in-time indicator context, the Phase 8 strategy registry, validator
and cost model, the Phase 9/10 order path, and the Phase 12 drawdown and min-trade-value guards.

```
                    ┌────────────────────────────────────────────┐
  candidates ──────►│ 1. collect  2. validate  3. rank          │
  (stock_id, price, │    composite_score desc, stock_id asc     │  the Phase 11 key
   stop, score)     │ 4. size  RISK_AND_WEIGHT + cash bound     │  ◄── sizing.py
                    │ 5. allocate one at a time, re-check limits│
                    │ 6. stop when capital / exposure / count   │
                    └───────────────────┬────────────────────────┘
                                        ▼
                        ┌───────────────────────────────┐
                        │ AllocationResult              │
                        │  decision per candidate, with │
                        │  every rule it broke          │
                        └───────────────┬───────────────┘
                                        │ apply=true only
                                        ▼
                            the ordinary Phase 10 order path
                            (the pre-trade engine has the last word)
```

---

## 2. New settings

Three, all **disabled by default** so an existing portfolio behaves exactly as it did:

| Setting | Default | Meaning |
|---------|---------|---------|
| `PORTFOLIO_MAX_DRAWDOWN_PCT` | unset (off) | Share of peak equity the portfolio may fall before the drawdown flag is raised |
| `PORTFOLIO_MAX_TOTAL_RISK_PCT` | unset (off) | Share of equity that may sit at risk to the stops of *all* open positions combined |
| `PORTFOLIO_MIN_TRADE_VALUE` | unset (off) | Smallest gross trade value the engine will propose; below it, no position is opened |

These feed `default_portfolio_risk_policy()`, which supplies the Phase 13 defaults. Every one of
them can also be overridden per request through the `policy` object, which is a **complete
replacement**, not a patch.

---

## 3. The sizing model

```
risk_budget         = equity * max_risk_per_trade_pct / 100
risk_per_share      = entry_price - stop_price                  (long only)
quantity_by_risk    = floor(risk_budget / risk_per_share)

max_position_value  = equity * max_position_weight_pct / 100
quantity_by_capital = floor(max_position_value / entry_price)

quantity_by_cash    = largest whole-share quantity whose price + fees fits in available cash

quantity            = floor(min(quantity_by_risk, quantity_by_capital, quantity_by_cash))
```

**Why the minimum of both bounds.** The risk bound alone is not a capital policy: 1% of 100,000
risked through a 5-per-share stop is 20,000 shares — 100,000 of stock on a 100,000 portfolio. The
weight bound is what stops one name from *becoming* the portfolio. Taking the minimum is what makes
the answer both safe (the stop can be honoured) and diversified (no single name dominates), and
reporting **which** bound applied is what makes the decision explainable.

`quantity_by_cash` is the largest quantity whose price **plus fees** fits the available cash, which
is what guarantees a sized order can always be funded and the cash balance can never go negative.

Both figures are floored, never rounded up: a position one share larger than the risk budget allows
would put more than the configured risk at risk, which is the exact thing the budget exists to
prevent.

### Refused, not rounded

| Input | Outcome |
|-------|---------|
| `entry_price <= 0` | `InvalidOrderError` |
| `stop_price` missing | `InvalidStopPriceError` — no stop, no per-share risk, no defensible size |
| `stop_price >= entry_price` | `InvalidStopPriceError` — a long cannot lose on a stop above entry |
| `equity <= 0` | quantity `0`, binding constraint `unaffordable` |
| cash buys less than one share | quantity `0` |
| result below `min_trade_value` | quantity `0` — a size the policy cannot afford is **no position, not a smaller one** |
| `method = MAX_POSITION_CAP` | capital-only sizing; the weight and cash bounds apply, and **no risk bound is claimed** |

A quantity of `0` is a legitimate answer and is always explained. It is never rounded *up* into a
trade.

---

## 4. The allocation endpoint

`POST /api/v1/portfolios/{portfolio_id}/allocate`

Six steps, in order:

1. **collect** the candidates, in whatever order they were sent;
2. **validate** the side, the entry price, the stop, and that the stock exists;
3. **rank** by `composite_score` descending, then `stock_id` ascending — the same
   `Candidate.sort_key` the Phase 11 engine uses, so the two can never disagree about which
   candidate is better, and neither invents a score;
4. **size** each one with `RISK_AND_WEIGHT`, bounded by the weight capacity still available;
5. **allocate one at a time**, re-checking every portfolio limit against the state the previous
   allocation actually left behind;
6. **stop** when capital is exhausted, the exposure ceiling is reached, the position count is full,
   or no remaining candidate could be accepted.

### Why allocation is sequential

Candidates compete for one pool of cash and one exposure ceiling, so they cannot be sized
independently. Sizing them in parallel would let three candidates each be approved for a third of
the portfolio and then all three fill it. One at a time, with the constraints re-checked after
each, is what makes the outcome a *portfolio* rather than a list of individually reasonable trades.

### Every refusal is complete

A refused candidate carries **every** rule it broke, not only the first. A client that had to call
three times to discover three problems with one proposal was not told anything useful.

| `RejectionCode` | Raised when |
|-----------------|-------------|
| `MAX_POSITION_WEIGHT_EXCEEDED` | the position would exceed the single-name cap |
| `MAX_PORTFOLIO_EXPOSURE_EXCEEDED` | total invested would exceed the exposure ceiling |
| `MAX_POSITIONS_REACHED` | opening this would exceed the position count |
| `MAX_RISK_PER_TRADE_EXCEEDED` | the loss at the stop exceeds the per-trade budget |
| `MAX_SECTOR_WEIGHT_EXCEEDED` | the sector would exceed its cap |
| `MAX_TOTAL_RISK_EXCEEDED` | open positions plus this trade exceed the total risk budget |
| `MIN_CASH_RESERVE_BREACHED` | the trade would leave less than the cash reserve |
| `INSUFFICIENT_CAPITAL` | no whole share fits every bound |
| `INVALID_STOP_PRICE` | the stop is missing, at or above the entry, or non-positive |
| `INVALID_PRICE` | the entry price is absent or non-positive |
| `INVALID_PORTFOLIO_STATE` | the candidate does not exist or cannot be priced |
| `UNSUPPORTED_SIDE` | anything other than `BUY` (this engine is long-only) |

A candidate the run **never reached** — because the run stopped first — is still returned, with
`stop_reason` on the response naming why. "This stock was on the list and was not reached" is a
different result from "this stock was reached and refused", and a caller planning a rebalance needs
to tell them apart.

### Nothing is written unless asked

`apply` defaults to `false`: an allocation is a recommendation. With `apply=true`, every approved
allocation travels through the ordinary Phase 10 order path, so the pre-trade engine has the last
word and a refusal there is reported rather than bypassed.

```bash
curl -X POST localhost:8000/api/v1/portfolios/1/allocate \
  -H 'content-type: application/json' \
  -d '{
        "candidates": [
          {"stock_id": 101, "entry_price": "1000", "stop_price": "950", "composite_score": 72},
          {"stock_id": 102, "entry_price": "340",  "stop_price": "330", "composite_score": 61}
        ],
        "policy": {"max_position_weight_pct": "25", "max_risk_per_trade_pct": "1"},
        "apply": false
      }'
```

---

## 5. The risk-state endpoint

`GET /api/v1/portfolios/{portfolio_id}/risk/state`

Where the portfolio stands against its policy **right now**: exposure, cash weight, invested
weight, per-position weight and risk-to-stop, realised and unrealised P&L, return, current
drawdown, the worst drawdown over the recorded equity curve, and the sector weights that are known.

A flag is a statement about the *portfolio*, not about a proposed trade — for that see
`POST /risk/check`.

| `PortfolioRiskFlag` | Raised when |
|---------------------|-------------|
| `POSITION_CONCENTRATION_BREACHED` | a position exceeds the single-name cap |
| `SECTOR_CONCENTRATION_BREACHED` | a sector exceeds its cap |
| `POSITION_LIMIT_BREACHED` | more positions are open than the limit allows |
| `EXPOSURE_LIMIT_BREACHED` | total invested exceeds the exposure ceiling |
| `CASH_RESERVE_BREACHED` | cash is below the reserve |
| `TOTAL_RISK_LIMIT_BREACHED` | open positions carry more risk to their stops than allowed |
| `DRAWDOWN_LIMIT_BREACHED` | equity is at or beyond its drawdown limit from peak |

Flags are raised in a **fixed order** — concentration, sector, position count, exposure, cash,
total risk, drawdown — because a flag list that reorders itself between two reads of the same
portfolio cannot be compared. Each flag is paired with a detail carrying the stable rule key, the
limit, the observed value, the reason and the offending `stock_id` or `sector`.

Positions whose sector is unknown are **left out of the sector totals** rather than lumped into an
"unknown" bucket: an invented bucket would make the sector rule report a weight belonging to no real
sector, and would let a concentrated portfolio pass the check.

Every percentage divides by **equity**, and every one of them is `null` — not `0` — when equity is
not positive. A weight of `0` means "this is nothing of the portfolio"; a weight of `null` means
"there is no portfolio to be a fraction of", and reporting the first when the second is true would
let a wiped-out portfolio look like a cautious one.

### Boundary behaviour

Comparisons follow the Phase 12 convention, which is deliberately **not** a single rule:

* **Concentration, exposure and cash limits are exclusive.** A portfolio sitting exactly on its
  limit is still *inside* it. A position at exactly 30% is allowed; one share above is not.
* **The drawdown guard is inclusive.** A portfolio sitting exactly on its drawdown limit *is*
  breaching it, and new buys are blocked.

The distinction is deliberate. A concentration limit answers "how much of this portfolio may one
name be", and holding exactly that much is the policy working as intended. A drawdown limit answers
"when does this portfolio stop being allowed to add risk", and the level where the guard fires is
the level where adding is no longer permitted.

---

## 6. The portfolio backtest endpoint

`POST /api/v1/portfolios/{portfolio_id}/backtest`

The Phase 8 engine applied at **portfolio** level: same bars, same point-in-time indicator context,
same strategy registry, same cost model, same chronological stepping — with several stocks competing
for one cash balance, one exposure ceiling and one position count, each entry sized by the Phase 13
risk-and-weight rule.

### No look-ahead

For every trading day, in this order:

1. fill each stock's decision taken from its **previous** bar at **this** bar's open;
2. evaluate the resting stop against that bar's own open and low;
3. mark every position to that bar's close;
4. decide from that bar's close, for that stock's next bar.

Sizing uses **last-known closes** for positions already held, so no entry can be sized from a price
the portfolio had not yet seen. `no_look_ahead` in the response is **verified from the run's own
fills**, not asserted.

**Nothing is written.** A backtest is a question about recorded history: no portfolio row, position,
order, trade or snapshot is created or changed.

---

## 7. Determinism

Every engine in this phase is pure — no clock, no database, no FastAPI, no randomness. The same
portfolio, the same candidates and the same policy always produce the same decisions, quantities and
rejection codes.

Where a timestamp is genuinely part of the answer, it is passed in (`as_of` on an allocation,
`as_of` on a risk state) and only defaulted to the wall clock when the caller did not supply one. It
is recorded, never read from.

Monetary values are `Decimal` end to end, including across the HTTP boundary: every monetary figure
in a response is a **string**, never a float.

---

## 8. Relationship to the earlier phases

| Phase 13 | Reuses | Does not change |
|-----------|--------|-----------------|
| ranking | `Candidate.sort_key` (Phase 11) | the key itself |
| indicators | `PointInTimeContextBuilder` (Phase 7) | any indicator |
| strategy | `strategy_registry` (Phase 8) | any strategy |
| costs | `TradingCostConfig` (Phase 9) | the cost model |
| orders | `PaperTradingService.submit` (Phase 10) | the order path |
| risk limits | Phase 12 pre-trade engine | its rules |
| metrics | `PerformanceMetrics` (Phase 8) | the metric definitions |
| drawdown | `peak_equity` / drawdown curve (Phase 12) | the guard's inclusive boundary |

The Phase 9 sizing endpoint (`POST /{id}/position-size`) is untouched. Phase 13 adds
`POST /{id}/risk/position-size` rather than changing it, so nothing that worked before stops
working.

---

## 9. Tests

`tests/test_phase13_*.py` — 208 tests across five files:

| File | Covers |
|------|--------|
| `test_phase13_sizing.py` | the risk/weight/cash bounds, every refusal, boundary inclusivity, explainability |
| `test_phase13_allocation.py` | ranking, sequential competition, every constraint, multi-breach reporting, determinism |
| `test_phase13_risk_state.py` | exposure, weights, P&L, drawdown, total risk, every flag, boundary behaviour |
| `test_phase13_portfolio_backtest.py` | replay mechanics, stops, costs, metrics, and the **no-look-ahead proof** |
| `test_phase13_api.py` | route registration, success and error paths, Decimal serialisation, backward compatibility |

### How the no-look-ahead tests work

Each builds **two** price paths that are identical up to a decision point and diverge afterwards,
then asserts the decision taken before that point is the same in both. Any influence from the
future shows up as a difference — the test cannot pass while a future bar is leaking backwards.
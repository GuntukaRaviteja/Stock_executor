# Phase 13 Completion Report — Portfolio & Risk Management Engine

> **Simulation only.** No brokerage is contacted, no credentials are stored, no real order is ever
> placed. Everything in this phase is a deterministic rule applied to a simulated portfolio.
> Nothing here is investment advice.

**Status: COMPLETE**

| | |
|---|---|
| Phase 12 baseline | 894 passed, 12 warnings |
| Phase 13 final | **1115 passed, 12 warnings** |
| New tests | **210** across 5 files |
| Regressions | **0** — no Phase 9–12 test modified or weakened |
| Migration | **none** — no column or table added |
| Existing endpoints changed | **none** |
| OpenAPI | builds clean; 4 new paths, 10+ new schemas |

---

## 1. What was built

Four capabilities, each in a **pure** engine (no clock, no database, no FastAPI, no randomness)
behind a thin DB-facing service and an HTTP endpoint.

| Endpoint | Engine | What it answers |
|----------|--------|-----------------|
| `POST /{id}/allocate` | `allocation_engine.py` | "Given these candidate signals, what should this portfolio buy, in what order, how much, and why not the rest?" |
| `GET /{id}/risk/state` | `risk_state.py` | "Where does this portfolio stand against its policy right now, and which limits are breached?" |
| `POST /{id}/backtest` | `portfolio_backtest.py` | "What would these several stocks have done together, chronologically, without look-ahead?" |
| `POST /{id}/risk/position-size` | `sizing.py` | "How many shares puts exactly 1% of equity at risk between here and the stop?" |

### The sizing model

```
quantity = floor(min(
    floor(risk_budget / (entry_price - stop_price)),   # loss at the stop ≤ 1% of equity
    floor(max_position_value / entry_price),           # one name ≤ 25% of equity
    quantity_by_cash))                                # price + fees must fit in cash
```

Both bounds floored, never rounded up. Every intermediate figure is returned, and the response
names **which** bound applied, so the answer can be re-derived from the response alone.

### Reuse, not duplication

| Phase 13 | Reuses | Changed? |
|-----------|--------|----------|
| ranking | `Candidate.sort_key` (Phase 11) | no — the same key |
| indicators | `PointInTimeContextBuilder` (Phase 7) | no |
| strategy lookup | `strategy_registry` (Phase 8), injected like `PaperTradingEngine` | no |
| costs | `TradingCostConfig` (Phase 9) | no |
| order application | `PaperTradingService.submit` (Phase 10) | no |
| pre-trade limits | Phase 12 risk engine | no |
| metrics | `PerformanceMetrics` (Phase 8) | no |
| drawdown | Phase 12 peak equity / curve | no |

---

## 2. Defects found and fixed

These were not found by reading the code. Each was exposed by a test or by the manual end-to-end
run, and each is now covered by a regression test.

### 2.1 Every BUY was rejected through the API — `allocation_engine.py`

`str(OrderSide.BUY)` renders as `'OrderSide.BUY'`, not `'BUY'`. The engine's
`str(open_side).upper() != "BUY"` check therefore rejected **every real buy** with
`UNSUPPORTED_SIDE`, making `POST /allocate` completely non-functional over HTTP while all
pure-engine tests passed (they passed strings, not enum members).

Fixed with one shared normaliser, `side_value()`, in `allocation.py`, used by the engine, the
service and the request guard, so an enum member, a plain string and a mixed caller all work.

### 2.2 `MAX_POSITION_CAP` sizing always returned zero shares — `sizing.py`

`_capital_only` passed a literal `0` where the risk bound goes, so
`_smallest(0, by_capital, by_cash)` computed `min(0, …)` and every capital-only size was zero.
`Optional` bounds are now honoured, and the reported binding constraint names whichever bound
actually applied.

### 2.3 A refused trade still reported risk at its stop — `sizing.py`

A size below `min_trade_value` set the binding constraint to `unaffordable` but left the
quantity, capital and `risk_amount` populated — the breakdown claimed risk for a trade it was
refusing. The refusal now zeroes quantity, capital and fees together, and names the refused value
in the explanation so it stays auditable.

### 2.4 `Decimal(None)` raised `TypeError`, not the domain error — `sizing.py`

`risk_per_share_long` accepted an `Optional` stop but called `Decimal(stop_price)` immediately,
so a missing stop escaped as `TypeError` rather than `InvalidStopPriceError` and became an
unhandled 500.

### 2.5 `max_total_risk_pct` ignored risk already at risk — `allocation_engine.py`

`_WorkingState.total_risk` was seeded from `ZERO` and nothing let a caller seed it, so the limit
measured only the risk the current run created. A portfolio already at its risk ceiling could
keep adding to it. `existing_total_risk` now seeds it, as `sector_weights` already seeded sectors.

### 2.6 The per-position weight cap was charged for the whole portfolio — `portfolio_backtest.py`

`_size` used *total* invested as the "existing value" for a stock not yet held, so
`capacity = cap − total_invested`. The first name to fill its allowance left every later name with
zero capacity: a portfolio backtest could hold exactly one position regardless of policy. Now
measured against *that position's* value.

### 2.7 A stop-less policy crashed the backtest — `portfolio_backtest.py`

`_size` hardcoded `RISK_AND_WEIGHT` and caught only `ValueError`/`ArithmeticError`, so a policy
without `stop_loss_pct` raised `InvalidStopPriceError` out of the replay instead of declining the
entry. Sizing now falls back to the capital-only model when there is no stop, and the handler
catches `AppValidationError` too.

### 2.8 A stock that does not exist could be allocated — `allocation_service.py`

Eligibility checked only the price, so a caller could pass an arbitrary `stock_id` with an
explicit `entry_price` and receive an **approved** allocation for an instrument that does not
exist. Now ineligible, with the reason reaching the caller in the decision.

### 2.9 `GET /risk/state` returned 500 for any position with a sector — `schemas/risk.py`

`sector_weights: List[Dict[str, Decimal]]` typed the sector *name* as a `Decimal`, so the moment
a held position had a known sector the response failed validation. Found only by the manual
end-to-end run — every API test used a stock with no company profile. Replaced with a typed
`SectorWeightResponse`. Two regression tests added.

### 2.10 The risk-state docstring contradicted the code — `risk_state.py`

The module claimed every comparison was inclusive while the code implemented the Phase 12
convention: concentration/exposure/cash **exclusive**, drawdown **inclusive**. The code was
right; the documentation was rewritten to state the real rule and why the two differ.

### 2.11 Out-of-range policy values raised instead of returning 422 — `schemas/risk.py`

The docstring promised values the engine "cannot act on" were "refused by the field constraints",
but the bounds lived only in a validator. Now declared as field constraints (`gt=0, le=100`), so
a weight of 150% is a clean 422.

### 2.12 `/allocate` accepted an empty candidate list — `schemas/risk.py`

The endpoint is documented as operating over "one or more candidates" but accepted zero and
returned a meaningless empty result. Now `min_length=1`.

---

## 3. Design decisions worth recording

### Allocation is sequential, and that is the whole point

Candidates compete for one pool of cash and one exposure ceiling. Sized independently, three
candidates would each be approved for a third of the portfolio and all three would fill it. One at
a time, with every limit re-checked against the state the previous allocation actually left, is
what makes the outcome a portfolio rather than a list of individually reasonable trades.

### Every refusal is complete

A refused candidate carries **every** rule it broke, in a fixed evaluation order. A client that
had to call three times to find three problems with one proposal was not told anything useful.

A candidate the run **never reached** is reported distinctly from one that was reached and
refused, with `stop_reason` naming why. "On the list and not reached" and "considered and
declined" are different answers for a rebalance.

### Two boundary rules, deliberately

- Concentration, exposure and cash limits are **exclusive** — a position at exactly 30% is inside
  the policy. This is the Phase 12 pre-trade rule.
- The drawdown guard is **inclusive** — sitting exactly on the limit *is* breaching it. This is the
  Phase 12 drawdown rule.

Both are tested at the boundary and one unit either side. Keeping them different is correct: a
concentration limit says "how much may one name be", and holding exactly that much is the policy
working; a drawdown limit says "when does adding stop being allowed", and the level where the
guard fires is that level.

### Limits that sizing already enforces are not tested as rejection paths

`MAX_POSITION_WEIGHT_EXCEEDED` and `MAX_RISK_PER_TRADE_EXCEEDED` are structurally unreachable: the
sizing formula caps the quantity at the very limits they check. Rather than write tests that
assert a code path which cannot fire, the tests assert the **guarantee** — over several stops and
several held positions, no approved trade ever exceeds the weight cap and the risk at the stop
never exceeds the budget. Testing an unreachable branch would prove nothing.

### Non-positive equity yields `null` weights, not `0`

A weight of `0` means "this is nothing of the portfolio". A weight of `null` means "there is no
portfolio to be a fraction of". Reporting the first when the second is true would let a wiped-out
portfolio look like a cautious one.

### Unknown sectors are excluded, not bucketed

Positions with no sector are left out of the sector totals entirely. An "unknown" bucket would
report a weight belonging to no real sector and would let a genuinely concentrated portfolio pass
the sector check.

### `apply` defaults to `false`

An allocation is a recommendation. With `apply=true` every approved allocation still travels
through the ordinary Phase 10 order path, so the pre-trade engine has the last word and a refusal
there is reported rather than bypassed.

---

## 4. The no-look-ahead proof

Each test builds **two** price paths that are identical up to a decision point and diverge
afterwards, then asserts the decision taken before that point is the same in both. Any influence
from the future shows up as a difference, so a test cannot pass while a future bar leaks backwards.

| Test | What it proves |
|------|----------------|
| `test_a_future_move_does_not_change_an_earlier_decision` | A crash after day 2 does not alter the day-1 entry fill |
| `test_a_future_rally_cannot_be_used_to_buy_earlier` | A rally from day 3 does not change what was paid on day 2 |
| `test_the_decision_bar_is_never_the_fill_bar` | A signal from bar N fills at bar N+1's open |
| `test_the_execution_log_shows_every_fill_after_its_signal` | Every recorded fill post-dates its signal |
| `test_the_run_says_whether_it_was_clean` | `no_look_ahead` is derived from the run's own fills, not asserted |

Sizing uses last-known closes for positions already held, so no entry can be sized from a price
the portfolio had not yet seen.

---

## 5. Manual verification

An end-to-end run over HTTP against the real router, DI, services and database: create portfolio →
size a trade → allocate across three candidates → apply → read risk state → backtest three stocks.
**Run twice; the JSON payloads are byte-identical.**

| Step | Result |
|------|--------|
| `POST /risk/position-size` | 200 shares, binding constraint `risk`, budget 1,000.00, per-share risk 5.0000, fees 20.00 — every figure matches the formula by hand |
| `POST /allocate` | ranked by score then id, 3 approved, exposure 50.0000%, cash 49,950.00 |
| `POST /allocate` with `apply` | order created through the Phase 10 path, `applied: true` |
| `GET /risk/state` | equity 100,980.00, exposure 20.7962%, 1 position, flag raised, sector weight reported |
| `POST /backtest` | 8 days processed, `no_look_ahead: true`, no writes |

**On the backtest reporting 0 trades:** this is genuine strategy behaviour, not a silent failure.
The real `phase7_technical` strategy requires a composite score of at least 20; on synthetic
flat-OHLC data it peaks at 9, so it correctly never enters, and the endpoint reports that honestly
rather than inventing trades. The replay's fill, stop, liquidation and cost behaviour is proven
separately by the 29 unit tests, which drive a scripted strategy through the registry seam — the
same technique the existing Phase 9 paper-engine tests already use.

---

## 6. Coverage

| File | Tests | Covers |
|------|-------|--------|
| `test_phase13_sizing.py` | 44 | risk/weight/cash bounds, every refusal, boundary inclusivity, fees, capacity override, explainability, determinism |
| `test_phase13_allocation.py` | 45 | ranking and tie-breaks, sequential competition, all twelve rejection codes, multi-breach reporting, stop reasons, refusal neutrality, determinism |
| `test_phase13_risk_state.py` | 49 | exposure, weights, P&L, drawdown, total risk, all seven flags, flag ordering, both boundary rules, unknown-sector exclusion, null weights |
| `test_phase13_portfolio_backtest.py` | 29 | replay mechanics, stops, multi-stock competition, costs, metrics, liquidation, **and the no-look-ahead proof** |
| `test_phase13_api.py` | 43 | route registration, schema publication, success and error paths, Decimal serialisation, sector weights, backward compatibility |

Every expected figure is derived from the rule under test rather than copied from a run, so a
failure points at the engine rather than at the fixture. Fixtures that construct a portfolio
balance cash against positions, because an unbalanced portfolio makes every percentage meaningless.

---

## 7. Compatibility

| Guarantee | Status |
|-----------|--------|
| No migration required | ✅ no column or table added |
| No existing endpoint changed shape | ✅ Phase 9 `POST /{id}/position-size` untouched; Phase 13 adds `POST /{id}/risk/position-size` |
| Phase 9–12 tests unchanged and passing | ✅ 894 → 1115 |
| Every monetary figure a `Decimal` string across HTTP | ✅ never a float |
| Bad policy values refused, never clamped | ✅ 422 by field constraint |
| OpenAPI builds | ✅ 4 new paths |
| Paper trading only | ✅ no brokerage, no credentials, no real orders |
| Determinism | ✅ identical payloads across repeated runs |

---

## 8. Known limitations

Stated plainly rather than left to be discovered:

1. **Long-only.** SELL reduces or closes a long; the engines do not open shorts, and
   `UNSUPPORTED_SIDE` says so rather than mis-sizing one.
2. **Equity is a supplied snapshot, not a mark-to-market chain.** Allocation sizes against the
   equity the caller supplies. A caller passing a stale equity gets a correctly computed answer to
   a stale question.
3. **The backtest replays the registered strategy only.** Adding a strategy means adding one
   registry entry; nothing about the portfolio engine needs to change.
4. **Two limit checks are structurally unreachable** (weight and per-trade risk), because the
   sizing formula already enforces them. They remain as defence against rounding drift rather than
   as live paths, and are tested as invariants instead.
5. **`BacktestTradeResponse` / `BacktestEquityPointResponse` exist in both `schemas/backtest.py`
   and `schemas/risk.py`.** FastAPI disambiguates them by module prefix in the OpenAPI component
   names. Harmless, but Phase 8's classes were left untouched rather than renamed.
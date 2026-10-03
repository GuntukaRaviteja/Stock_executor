# Phase 15 — Portfolio Risk Analysis, Stress Testing & Exposure Control

> **Simulation only.** No brokerage is contacted, no credentials are stored, no real order is ever
> placed. Everything here is a deterministic rule applied to a simulated portfolio. Nothing in this
> document is investment advice, a prediction, or a guarantee of any outcome.

## 1. What this phase adds

Phase 14 allocated capital across strategies and Phase 13 sized the resulting orders. Both answer
*"what should we buy?"*. Neither answers *"is the book they produced acceptable?"*. Four gaps
remained:

1. **No portfolio-level exposure.** Phase 13 reported a single position at a time. Nothing measured
   the combined weight of one sector, one strategy, one asset class, or the largest N positions —
   and nothing aggregated the same instrument held by several strategies into one exposure.
2. **No covariance.** Volatility was per-position. Portfolio volatility includes diversification,
   which needs the correlation structure of the book, and nothing in the codebase computed it.
3. **No stress testing.** "Within limits" says nothing about what happens when the market falls
   20%, when correlations go to one, or when a strategy stops working.
4. **No decision.** The phases below could *report*; none could *decide*, and none could act on
   what it found.

Phase 15 closes all four. It sits above the earlier phases, reads their output, and returns one
report containing a decision.

```
  positions + cash + return series          PortfolioRiskEngine (pure)
            │                                       │
            ▼                                       ▼
   ┌──────────────────┐            ┌──────────────────────────────────┐
   │ 1. validate      │            │ 7. risk metrics                   │
   │ 2. exposure      │───────────►│ 8. liquidity                     │
   │ 3. concentration │            │ 9. seven stress scenarios        │
   │ 4. covariance    │            │ 10. violations                   │
   │ 5. risk budgets  │            │ 11. deterministic reduction      │
   │ 6. data quality  │            │ 12. decision + explanation       │
   └──────────────────┘            └──────────────────────────────────┘
                                                        │
        APPROVED ─ ADJUSTED ─ REJECTED ─ DATA_INSUFFICIENT
```

The engine is **pure**: no clock, no database, no randomness, no HTTP. `PortfolioRiskService`
converts a validated request into an engine input and a persisted portfolio into the same input; the
API layer converts the result back. Every figure in the report is an estimate from the historical
sample that was supplied, and every scenario is a deterministic simulation of today's holdings.

---

## 2. New settings

All Phase 15 settings live under the `PORTFOLIO_RISK_` prefix and are documented in
`.env.example`. Three rules hold for all of them:

- **An empty value disables the rule.** It does not mean "zero tolerance". A disabled limit is
  reported as `null` in `config_applied`, and nothing is measured against it.
- **A limit may also be sent per request** inside `risk_config`. A limit the caller names
  overrides the application default for that call only; a limit it does not name is untouched; and
  an explicit `null` in the request disables that rule for that call.
- **A request cannot invent a new limit.** An unknown name is refused with 422.

The limits, grouped:

| Group | Settings |
|-------|----------|
| Concentration | `MAX_SINGLE_ASSET_WEIGHT`, `MAX_SECTOR_WEIGHT`, `MAX_STRATEGY_WEIGHT`, `MAX_ASSET_CLASS_WEIGHT`, `MAX_TOP_N_CONCENTRATION`, `TOP_N_COUNT` |
| Risk metrics | `MAX_VOLATILITY`, `MAX_BETA`, `MAX_DRAWDOWN`, `MAX_CORR_CONCENTRATION` |
| Stress | `MARKET_CRASH_SHOCK`, `SEVERE_CRASH_SHOCK`, `SECTOR_SHOCK`, `HIGH_BETA_SHOCK`, `HIGH_BETA_TILT`, `STRATEGY_SHOCK`, `LIQUIDITY_HAIRCUT`, `CORRELATION_SPIKE_MULTIPLIER`, `CORRELATION_SPIKE_VOL_MULTIPLIER`, `MAX_STRESS_LOSS` |
| Liquidity | `MIN_LIQUIDITY_SCORE`, `MAX_PARTICIPATION_RATIO` |
| Counts | `MIN_POSITION_COUNT`, `MAX_POSITION_COUNT` |
| Evaluation | `WARNING_UTILIZATION`, `ANNUALIZATION_PERIODS`, `MIN_OBSERVATIONS`, `MIN_VAR_OBSERVATIONS`, `CORR_CLUSTER_THRESHOLD`, `BENCHMARK_SYMBOL` |
| VaR | `VAR_CONFIDENCE`, `VAR_HORIZON_DAYS` |
| Budgets | `BUDGET_ASSET_CONC`, `BUDGET_SECTOR_CONC`, `BUDGET_STRATEGY_CONC`, `BUDGET_VOLATILITY`, `BUDGET_BETA`, `BUDGET_DRAWDOWN`, `BUDGET_VAR`, `BUDGET_STRESS` |
| Policy | `MANDATORY_LIMITS`, `MAX_ADJUSTMENT_PASSES`, `ENABLE_ADJUSTMENT`, `DISABLE_STRESS_TESTS` |

---

## 3. Missing data is never zero

This is the rule the whole phase is built around.

A metric that cannot be computed from the data supplied is reported as
`"value": null` with `"status": "DATA_INSUFFICIENT"` and an explanation naming the missing input.
It is **never** reported as `0`. A zero volatility would say the portfolio cannot move; a zero VaR
would say it cannot lose money; a zero participation ratio would say it is liquid. All three are
claims, and none of them follows from absent data.

The same rule applies to scenarios and to rules:

| Situation | Status | Why |
|-----------|--------|-----|
| A metric has no data | `DATA_INSUFFICIENT` | unmeasured is not zero |
| A scenario could not run (missing sector, volume or covariance) | `DATA_INSUFFICIENT` | it was never tested |
| A scenario had nothing to act on (every position liquid, portfolio empty) | `NOT_APPLICABLE` | it passed trivially, and saying so is different from saying it passed a test |
| A rule has no configured limit | not evaluated at all | there is no limit to breach |

Where data is partially available, the report says so rather than hiding it: the covariance block
carries `data_quality` (`SUFFICIENT` / `PARTIAL` / `INSUFFICIENT`), the symbols it excluded, the
weight it covered, and an explanation naming the gap. A volatility computed from 60% of the book
is reported as the figure it is.

---

## 4. Exposure and concentration

A symbol may appear once per strategy — Phase 14 hands the same instrument to several strategies —
so the engine **aggregates lots into one portfolio-level exposure** and reports `total_positions`
alongside `total_lots` so the difference is visible.

Every weight is measured against **equity** (`positions + cash`), never against invested value
alone. That is why a portfolio holding 20% cash loses 20% of its equity in a -20% market, not 25%.

Concentration metrics produced: largest single position, sector weight, strategy weight, asset-class
weight, top-N concentration, and weighted average pairwise correlation. Each carries its limit,
utilization, status, and an explanation with the numbers in it.

A position or group with no sector classification is grouped under `UNCLASSIFIED` and reported with
its weight. It is not dropped, and the sector limit is not applied to a group that holds an
arbitrary mixture.

---

## 5. Covariance, volatility and risk contribution

The engine aligns each symbol's return series to the shortest common length, computes the sample
correlation and covariance matrices, and derives `w' Σ w` for the portfolio.

Three refusals are built in, because each would otherwise produce a plausible-looking wrong number:

- **Insufficient data.** Fewer than `MIN_OBSERVATIONS` overlapping returns, or fewer than two
  eligible symbols, yields no matrix and a `DATA_INSUFFICIENT` result.
- **Flat series.** A series with zero variance says nothing about its correlation; it is excluded
  and named, never treated as uncorrelated (which would read as zero risk).
- **An unusable matrix.** The matrix is checked for completeness and symmetry, and its leading
  principal minors are checked for positive semi-definiteness. A matrix that fails cannot produce a
  real variance, so the run is refused rather than repaired.

`ρ' = (1 − m)·ρ + m` keeps the spiked matrix positive semi-definite **by construction**, which is
why the correlation-spike scenario is safe to run on a book that is already perfectly correlated.

Risk contributions are reported per position as marginal (`MRC_i = (Σw)_i / σ_p`) and component
(`CRC_i = w_i · MRC_i`), sorted descending. Their sum reconciles to total portfolio volatility, and
the residual is reported as `reconciliation_error_pct` — a check that is printed rather than assumed.

Beta is `Σ w_i β_i`. Positions carrying a beta are used when supplied; otherwise beta is estimated
from the return series against the benchmark. **Beta for a single-position portfolio is computed
even when no covariance matrix exists**, because one asset and a benchmark is enough to define it.

---

## 6. Risk metrics

| Metric | Definition | Limit |
|--------|-----------|-------|
| Volatility | `σ_daily · √annualization_periods` from the covariance matrix; falls back to the standard deviation of the supplied portfolio return series, and says so | `MAX_VOLATILITY` |
| Beta | weighted position betas, else `Cov(R_i, R_m) / Var(R_m)` | `MAX_BETA` |
| Max drawdown | worst peak-to-trough decline of the equity curve | `MAX_DRAWDOWN` |
| VaR | interpolated historical quantile of the portfolio return series | `BUDGET_VAR` |
| Expected Shortfall | mean of the worst `⌈α·n⌉` returns | none by default |

Three decisions worth stating:

- **Drawdown is not the largest daily loss.** A slow bleed produces a larger drawdown than any
  single day's fall. The report keeps `current_drawdown_pct` separate from the worst one so the two
  cannot be confused.
- **The VaR quantile is interpolated**, so the result moves smoothly with the sample instead of
  jumping between observations.
- **The Expected Shortfall tail is the worst `⌈α·n⌉` observations, not "everything below the
  interpolated threshold".** Because the threshold falls *between* two observations, including the
  one above it would average in a loss that is less bad than the level VaR names, and would pull
  Expected Shortfall back toward VaR.

Beyond one day, VaR and Expected Shortfall scale by `√horizon` — the standard approximation, which
assumes returns are independent. That is exactly what a correlated book violates, so the report says
so rather than presenting the number as precise.

---

## 7. The seven stress scenarios

| Scenario | What it does |
|----------|--------------|
| `MARKET_CRASH_20` | every position falls `MARKET_CRASH_SHOCK`% |
| `MARKET_CRASH_30` | every position falls `SEVERE_CRASH_SHOCK`% |
| `SECTOR_SHOCK` | the targeted sector falls `SECTOR_SHOCK`%, others flat |
| `HIGH_BETA_SHOCK` | base `HIGH_BETA_SHOCK`% plus `HIGH_BETA_TILT` points per unit of beta above 1.0 |
| `CORRELATION_SPIKE` | correlations blended toward 1.0 by `CORRELATION_SPIKE_MULTIPLIER`, volatilities scaled, loss is 1.645 × the stressed daily volatility |
| `LIQUIDITY_STRESS` | positions above the participation limit take an extra `LIQUIDITY_HAIRCUT`% |
| `STRATEGY_DRAWDOWN` | the largest strategy falls `STRATEGY_SHOCK`%, others flat |

Target selection is deterministic: the sector scenario targets `STRESS_SECTOR` when the portfolio
actually holds that sector, otherwise the heaviest classified sector with ties broken
alphabetically. **Targeting a configured sector that the portfolio does not hold produces
`DATA_INSUFFICIENT`, not a zero loss** — reporting "no loss" from a shock that touched nothing
would read as safety.

Each outcome carries the per-position contributions, the assumptions it was computed under, and its
utilization against `MAX_STRESS_LOSS`.

---

## 8. The decision

Four outcomes, checked in this order:

| Decision | When |
|----------|------|
| `DATA_INSUFFICIENT` | a **mandatory** metric could not be evaluated at all |
| `REJECTED` | a mandatory breach survives adjustment, or no legal reduction exists |
| `ADJUSTED` | a breach was found and corrected deterministically |
| `APPROVED` | every evaluable limit is satisfied |

`DATA_INSUFFICIENT` is checked first and on purpose: a mandatory metric that could not be measured
cannot be traded around, so the honest answer is "I do not know", not "approved".

A coverage gap on a **non-mandatory** metric is reported with its own `EVALUATION_GAP` detail and
named in the explanation, but does not by itself reject a portfolio whose evaluable limits pass.
The `decision_details` list keeps those gap entries alongside the summary — an approval whose detail
list names no unmeasured metric would hide the very caveat its explanation states.

Breaches and gaps are counted separately in the report text. A limit that could not be evaluated is
not a limit that was broken, and a report that calls a coverage gap a breach overstates what is
known in the direction of sounding alarming about missing data.

---

## 9. Deterministic reduction

Adjustments only ever **reduce**. Shares are sold — whole shares, rounded up so no residual breach
survives — and **the proceeds stay in cash**. Equity is therefore identical before and after, and
an adjusted portfolio never carries more leverage than the one it was handed.

Actions are chosen by a fixed priority order, and each one is re-evaluated against the **whole**
measurement stack before the next is considered:

1. single position weight → 2. sector weight → 3. strategy weight → 4. asset class weight →
5. top-N concentration → 6. correlated cluster → 7. risk contribution → 8. stress loss →
9. participation ratio

Limits in `MANDATORY_LIMITS` are **not** adjusted; a breach of one is reported and the portfolio is
rejected. That is the point of the list: a limit an operator declared mandatory is a limit the
engine refuses to fix on its own.

Some breaches cannot be fixed by trading and are **reported rather than traded around**:

- a historical drawdown, VaR or Expected Shortfall of the supplied sample — these are facts about
  the past, and selling today does not rewrite them;
- a `MIN_POSITION_COUNT` that conflicts with a concentration rule — satisfying one would violate the
  other;
- a beta breach with no per-asset betas — there is nothing to steer with;
- an unevaluable mandatory metric.

Every adjustment records the symbol, quantity, before and after weight, the constraint it fixed, the
expected improvement, and a sentence of reasoning.

---

## 10. Endpoints

Three new routes, registered **before** the `/{portfolio_id}` routes so FastAPI does not try to
parse `"risk"` as a portfolio id:

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/v1/portfolios/risk/analyze` | evaluate a submitted portfolio |
| `POST` | `/api/v1/portfolios/risk/adjust` | run the reduction loop and return before and after |
| `POST` | `/api/v1/portfolios/{portfolio_id}/risk/analyze` | evaluate a persisted Phase 9 paper portfolio |

### Submitted portfolios

```json
{
  "portfolio_id": 1,
  "portfolio_value": "20000.00",
  "cash": "4000.00",
  "positions": [
    {"symbol": "AAA", "quantity": 50, "price": "100", "sector": "Technology",
     "beta": "1.2", "avg_daily_traded_value": "5000000"}
  ],
  "asset_returns": {"AAA": ["0.011", "0.004", "..."]},
  "portfolio_returns": ["0.003", "..."],
  "benchmark_symbol": "SPY",
  "benchmark_returns": ["0.002", "..."],
  "risk_config": {"max_single_asset_weight": "25"}
}
```

`portfolio_value` **must** reconcile with `cash + Σ(quantity × price)`; a mismatch is refused with
422 rather than silently re-scaled, because a report measured against a denominator it ignored
would be worse than no report.

Every response is `{"success": true, "data": {...}, "meta": {}}`, and **every decimal is a string**.
An assertion that passed through `float()` would hide a precision regression.

### Persisted portfolios

The stored route reads positions, cash, sector classification and traded value from the database:
traded value is the mean close×volume of the most recent stored bars, and sector comes from the
company profile. Only stored data is used. A position with no volume reports liquidity as
unavailable rather than assuming it is liquid, and a position with no usable price is refused
rather than valued at zero.

Its body carries **only** `risk_config`. A caller cannot declare a `portfolio_value` the engine will
not measure.

Strategy attribution comes from stored order metadata: a strategy name written there is used;
otherwise a lot placed by a strategy event is labelled `STRATEGY` and a manual one `MANUAL`. Naming
a specific strategy that was never stored would invent the grouping the report is meant to measure.

---

## 11. Determinism

Every engine in this phase is pure: no clock, no database, no randomness, no FastAPI. The same
portfolio, return series and policy always produce the same report — including the same matrix
indexing (symbols are always sorted), the same violation order, and the same reduction sequence.

Where a timestamp is genuinely part of the answer it is **passed in** and recorded, never read from
a clock. `Decimal` is used end to end, including across HTTP.

---

## 12. Relationship to the earlier phases

| Phase 15 | Reuses | Does not change |
|----------|--------|-----------------|
| positions | `PaperTradingService` position rows (Phase 9/10) | the order path |
| money | `quantize_money` / `quantize_percent` (Phase 8) | the rounding helpers |
| sector | `Company.sector` (Phase 2) | the profile |
| strategy lots | Phase 14 strategy allocation | the allocation |
| limits | Phase 12/13 policy style | any earlier limit |

Phase 15 adds routes rather than changing existing ones, so nothing that worked before stops
working. It also writes nothing: analysis is read-only.

---

## 13. Tests

`tests/test_phase15_*.py` — **212 tests** across seven files:

| File | Tests | Covers |
|------|-------|--------|
| `test_phase15_exposure.py` | 38 | lot aggregation, weights against equity, sector/strategy/asset-class exposure, top-N ladder, reconciliation refusals |
| `test_phase15_covariance.py` | 38 | known matrices, perfect and zero correlation, flat series, incomplete/asymmetric/indefinite matrix refusals, risk contributions, clusters, beta, correlation spikes |
| `test_phase15_risk_metrics.py` | 34 | hand-computed volatility, drawdown, VaR and Expected Shortfall; horizon scaling; every unmeasurable path |
| `test_phase15_stress.py` | 32 | all seven scenarios, target selection, `DATA_INSUFFICIENT` vs `NOT_APPLICABLE`, impact reconciliation, determinism |
| `test_phase15_controls.py` | 20 | violation detection, mandatory-vs-gap decisions, reduction priority, whole-share selling, equity preservation, convergence |
| `test_phase15_api.py` | 38 | the three routes, Decimal serialisation, 16 refusal cases, override semantics, route ordering |
| `test_phase15_integration.py` | 12 | Phase 13/14 chain → Phase 15 report, aggregation across strategies, config scope, read-only behaviour |

The whole suite is **1445 tests, all passing**.

### What the tests refuse to let slide

- **Hand-computable numbers.** Every headline figure has an independent arithmetic derivation in
  the test that checks it, rather than a snapshot of whatever the engine produced.
- **Controlled comparisons.** Where two books are compared, their individual volatilities are
  matched first, so only the variable under test differs.
- **Honest absences.** Every metric, scenario and rule has a test for the case where its input is
  missing, asserting `null`/`DATA_INSUFFICIENT` and never `0`.
- **Determinism.** Every engine and every HTTP response is asserted equal across two runs.
- **Refusals.** Sixteen API cases assert 422, and the engine-level refusals (non-finite value,
  non-reconciled equity, unusable matrix) are asserted as raised errors.
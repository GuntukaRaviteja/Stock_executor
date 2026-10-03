# Phase 15 Completion Report — Portfolio Risk Analysis, Stress Testing & Exposure Control

> **Simulation only.** No brokerage is contacted, no credentials are stored, no real order is ever
> placed. Everything in this phase is a deterministic rule applied to a simulated portfolio.
> Nothing here is investment advice, a prediction, or a guarantee of any outcome.

**Status: COMPLETE**

| | |
|---|---|
| Phase 14 baseline | 1233 passed |
| Phase 15 final | **1445 passed, 12 warnings** |
| New tests | **212** across 7 files |
| Regressions | **0** — no Phase 9–14 test modified or weakened |
| Migration | **none** — no column or table added |
| Existing endpoints changed | **none** — three routes added |
| OpenAPI | builds clean; 3 new paths, 2 new schemas |

---

## 1. What was built

One capability — *is the portfolio the earlier phases produced acceptable?* — behind a pure engine,
a DB-facing service and three routes.

| Route | What it answers |
|-------|-----------------|
| `POST /portfolios/risk/analyze` | Given this book, what is its risk, which limits does it break, and what is the decision? |
| `POST /portfolios/risk/adjust` | Reduce it deterministically; what did you sell, why, and what remains? |
| `POST /portfolios/{id}/risk/analyze` | The same, for a persisted Phase 9 paper portfolio, from stored state only |

### The engine

```
app/services/portfolio_risk/
  config.py              validated policy: limits, overrides, utilization, mandatory set
  models.py              frozen dataclasses — snapshot, exposure, metrics, violations, decisions
  evaluation.py          shared status / utilization / assessment helpers
  exposure_analyzer.py   lot aggregation, weights vs equity, sector/strategy/class, concentration
  covariance_analyzer.py correlation, covariance, volatility, beta, contributions, clusters
  risk_metrics.py        volatility, beta, max drawdown, historical VaR, Expected Shortfall
  liquidity_analyzer.py  participation ratio, liquidity score, liquidation estimate
  stress_tester.py       seven deterministic scenarios
  risk_controller.py     violations, the decision, deterministic reduction
  risk_engine.py         orchestration, budgets, data quality, explanations
  risk_service.py        request conversion, schema reporting, Phase 9 DB adapter
```

The engine reads no clock and no database. `PortfolioRiskService` is the only piece that touches
storage, and the API layer only converts. Nothing is computed twice for the API: the engine
produces the report shape and it is returned as-is.

---

## 2. The rule the whole phase is built around

**Missing data is never zero.**

A metric that cannot be computed comes back as `"value": null` with
`"status": "DATA_INSUFFICIENT"` and an explanation naming the missing input. A zero volatility
would claim the portfolio cannot move; a zero VaR would claim it cannot lose money; a zero
participation ratio would claim it is liquid. All three are claims, and none follows from absent
data.

Where data is partial, the report says so: the covariance block carries `data_quality`, the symbols
it excluded, the weight it covered, and a sentence naming the gap. A volatility computed from 60% of
the book is reported as the figure it is.

Two distinctions that are easy to lose and were kept explicit:

| Situation | Status |
|-----------|--------|
| A scenario could not run (missing sector, volume or covariance) | `DATA_INSUFFICIENT` |
| A scenario had nothing to act on (every position liquid, portfolio empty) | `NOT_APPLICABLE` |
| A limit has no configured value | not evaluated at all |

---

## 3. Defects the tests exposed and fixed

The tests were written to check claims rather than to record output, and they found real problems.

### In code written earlier in this phase

| Defect | Why it mattered | Fix |
|--------|-----------------|-----|
| **Beta was refused whenever a covariance matrix could not be built** | A single-position portfolio has a perfectly well-defined beta, and refusing it reported a measurable risk as missing | Beta is computed from one asset plus a benchmark, independently of the covariance path |
| **`Expected Shortfall` had an unreachable fallback branch** | With the interpolated quantile the tail is never empty, so the branch was dead code implying a false possibility — and `value <= threshold` pulls ES back toward VaR by including an observation less bad than the level VaR names | The tail is the worst `⌈α·n⌉` observations, stated in `tail_rule` |
| **Breaches and coverage gaps were counted together** | The report said "2 risk limit(s) are breached" for two metrics that were never evaluated — alarming about missing data | Breaches and gaps are counted separately and worded separately |
| **`EVALUATION_GAP` details were discarded on the APPROVED path** | The explanation said N metrics could not be evaluated while `decision_details` named none of them | Gap details are kept alongside the summary |
| **A disabled limit was reported as the string `"None"`** | `str(None)` reads to a consumer as a limit of no value rather than as no limit | `config_applied` emits JSON `null` for a disabled rule |
| **A partial `risk_config` silently cleared every unset limit** | `model_dump(exclude_unset=False)` reported each *omitted* limit as an explicit null, so overriding one limit produced a policy with no shocks, no tilts and no thresholds — then refused it | `exclude_unset=True`, which is also what makes "an explicit null disables the rule" work |

### Defects in the pre-existing codebase

| Defect | Why it mattered | Fix |
|--------|-----------------|-----|
| **`POST /{id}/risk/analyze` could not accept a config-only body** | The route's parameter was the full request schema, so a caller wanting to tighten one limit for a stored portfolio got `422 portfolio_value: Field required` — the documented route behaviour was unreachable | A dedicated `PortfolioRiskOverridesSchema` carrying only `risk_config`. A caller can no longer declare a `portfolio_value` the engine will not measure |
| **`_order_source_map` did not exist** | The entire persisted-portfolio path raised `AttributeError` on first use — a route that had never run | Implemented against stored order metadata, with `STRATEGY` / `MANUAL` fallbacks and a note on why a specific strategy name is never invented |
| **Partial beta coverage was silent** | A position with no beta takes the base shock, which is an assumption, but the outcome did not say so | The outcome names the uncovered weight and the symbols |
| **`max_adjustment_passes`, `enable_adjustment`, `disable_stress_tests` were unreachable** | The reduction budget materially changes behaviour but could not be configured | Wired to `PORTFOLIO_RISK_MAX_ADJUSTMENT_PASSES`, `PORTFOLIO_RISK_ENABLE_ADJUSTMENT`, `PORTFOLIO_RISK_DISABLE_STRESS_TESTS` |

### Defects in the test helpers themselves

Two helpers would have let a broken engine pass:

| Helper | Problem | Fix |
|--------|---------|-----|
| `perfect_pair` | Scaled the second series by 2, so the pair correlated perfectly but the second had **four times the variance**. Every hand-checked portfolio variance built on it was wrong | Offset instead of scale: an exact affine image, correlation 1, identical variance |
| `oscillating` | Its "falls" phase produced only *smaller positive* returns, so equity never fell. A drawdown test written against it passed while checking nothing | The second half is negative, so the curve really does cross back under its peak |

---

## 4. What the tests refuse to let slide

- **Hand-computable numbers.** Every headline figure has an independent arithmetic derivation in
  the test that checks it — variance from centred sums, the 5th percentile interpolated between two
  named observations, a drawdown from a hand-listed equity curve — rather than a snapshot of
  whatever the engine produced.
- **Controlled comparisons.** Where two books are compared, their individual volatilities are
  matched first (`rescale_to_volatility`), so the only difference is the variable under test. The
  correlation-spike test would otherwise have "passed" by comparing a quiet book to a loud one.
- **Honest absences.** Every metric, scenario and rule has a test for the missing-input case,
  asserting `null` / `DATA_INSUFFICIENT` and never `0`.
- **Determinism.** Every engine and every HTTP response is asserted equal across two runs, with
  only the server-generated timestamp excluded.
- **Refusals.** Sixteen API cases assert 422; engine-level refusals (non-finite value,
  non-reconciled equity, incomplete/asymmetric/indefinite matrix, unknown limit name) are asserted
  as raised errors with the reason.
- **Invariant arithmetic.** `Σ position_impacts == portfolio return`, `Σ risk contributions ==
  portfolio volatility`, `cash + Σ positions == equity`, `obligations + inflows == both sides of
  every cash ledger`.

---

## 5. Documentation

| Artefact | Covers |
|----------|--------|
| [`docs/phase-15.md`](docs/phase-15.md) | the rule, exposure, covariance, metrics, the seven scenarios, the decision, reduction priority, endpoints, determinism, tests |
| [`README.md`](README.md) | the three endpoints, the missing-data rule, the reduction guarantee, the key settings, a 50-item completion checklist |
| [`.env.example`](.env.example) | every `PORTFOLIO_RISK_*` setting, grouped, with an empty value meaning the rule is off |

---

## 6. Standing constraints

- **Paper trading only.** No brokerage, no credentials, no real orders.
- **No migration.** Phase 15 adds no column and no table.
- **No existing endpoint changed shape.** Three routes were added; nothing was altered.
- **Purity.** Every engine takes all its inputs and returns everything it produces: no clock, no
  database, no randomness, no HTTP. Timestamps are caller-supplied and recorded, never read.
- **`Decimal` end to end**, including across HTTP: every figure in a response is a string.
- **Adjustment only ever reduces.** Whole shares are sold, proceeds stay in cash, equity and total
  leverage are unchanged, and no exposure is added elsewhere to compensate.
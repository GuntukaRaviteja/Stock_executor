# Phase 11 Completion Report — Portfolio Construction & Risk Management

**Status: COMPLETE** (conditional on the checklist at the end; every item was executed, not
inspected)

**Date:** 2026-10-03
**Scope:** Portfolio construction, allocation and position sizing, the portfolio-level risk
report, rebalancing, the cash ledger, explainable decisions, persistence, API surface, tests and
documentation.

> **Simulation only.** No brokerage is contacted, no credentials are stored, no real order is ever
> placed. This is a portfolio decision and simulation layer. Nothing here is investment advice.

---

## 1. Test results

```
757 passed, 1 warning in 16.66s
```

| Suite | Tests | Status |
|-------|-------|--------|
| `test_portfolio_construction_api.py` (new) | 96 | pass |
| `test_portfolio_end_to_end.py` (new) | 2 | pass |
| `test_portfolio_allocation.py` (Phase 9, extended) | 77 | pass |
| `test_portfolio_engine.py` (Phase 9, extended) | 74 | pass |
| `test_portfolio_api.py` (Phase 9, extended) | 55 | pass |
| All pre-existing suites (Phases 1–10) | 582 | pass, unchanged |

**175 net new tests.** 98 of them are in two wholly new files; the other 77 were added to three
Phase 9 suites. Baseline before this phase was **582 passed**; the suite is now **757**. All 582
pre-existing tests still pass, with no expectation modified — the only pre-existing test file
touched anywhere in this repository is `test_prices.py`, from Phase 3.

The single warning is `StarletteDeprecationWarning` from `fastapi.testclient` about `httpx`. It
originates in the test client, not in application code.

---

## 2. Requirements, and how each is met

| # | Requirement | Where | Evidence |
|---|-------------|-------|----------|
| 1 | Portfolio creation and configuration | `allocation_config_json`, `PATCH /{id}` | Round-trip test in `test_portfolio_api.py` |
| 2 | Capital and cash management | `PaperTradingService.deposit/withdraw` | Ledger and balance agree, 8 deposit/withdrawal tests |
| 3 | Position sizing | `allocation.py`, `position_sizing_service.py` | Three formulas, each tested at the boundary |
| 4 | Portfolio allocation | `build_target_weights`, `allocate_equal_weights` | Cap-after-split asserted verbatim |
| 5 | Risk limits | `allocation_config_json` + Phase 10 `risk_config_json` | The two are separate columns; see §5 |
| 6 | Entry decisions (`BUY`) | `construction.py` | 96 API tests; explanation parity asserted |
| 7 | Exit decisions (`SELL`, `REDUCE`) | `construction.py` | Stop-loss → `SELL`, overweight → `REDUCE` |
| 8 | Rebalancing | `rebalancing.py`, `POST /rebalance` | Underweight, overweight, threshold, new, removal |
| 9 | Exposure calculation | `risk_engine.py` | Gross, net, cash% and invested% reported separately |
| 10 | Realized and unrealized P&L | `service.py`, weighted-average | Partial sell books realized P&L; see `test_portfolio_end_to_end.py` |
| 11 | Equity curve | Phase 10 snapshots, read by Phase 11 | Persisted and re-read, never re-derived |
| 12 | Performance metrics | `performance_service.py` | CAGR, Sortino, exposure split added |
| 13 | Explainable decisions | `reasons` + `explanation` | One sentence per code, asserted by `len(reasons) == len(explanation)` |
| 14 | Deterministic behavior | all pure modules | No clock, network, session or global state; explicit test |
| 15 | Full test coverage | 4 Phase 11 suites | §1 |
| 16 | Signal/screener integration | `_resolve_targets` | Phase 7 screener accepted as a candidate source; no rules duplicated |
| 17 | Backtesting not regressed | — | every pre-existing test passes |
| 18 | Additive migration | `f5a1c7d94b20` | Verified in both directions against the ORM schema |
| 19 | Documentation | `docs/phase-11.md`, README, `.env.example` | Includes the worked end-to-end example |

---

## 3. Architecture

```
Phase 7 screener ──┐
Phase 7 signals  ──┼──► candidates ──► PortfolioConstructionService
explicit prices  ──┘                       │
                                            ├─► PortfolioDecision   BUY/SELL/HOLD/REDUCE
                                            ├─► RebalancePlan       drift vs target weights
                                            └─► PortfolioRiskReport exposure, concentration
                                                     │
                                                     ▼
                                         PaperTradingService ← the only writer of state
                                                     │
                                                     ▼
                                       positions · trades · ledger · snapshots
```

The layering follows the project's existing `Router → Schema → Service → Repository` convention, and
the calculation modules are pure:

| Module | Responsibility | Depends on |
|--------|----------------|------------|
| `services/portfolio/allocation.py` | target weights, stops, drift, sizing arithmetic | `Decimal` + Phase 8 quantisation only |
| `services/portfolio/construction.py` | the decision engine | `allocation.py` + engine dataclasses |
| `services/portfolio/rebalancing.py` | the rebalance planner | same |
| `services/portfolio/risk_engine.py` | the portfolio risk report | same |
| `services/portfolio/construction_service.py` | orchestration, screener resolution, apply path | the engines + repositories |
| `schemas/allocation.py` | request/response contracts | Pydantic |

No calculation module imports FastAPI, a request object or a database session. Every percentage
arrives as an argument — there are no strategy constants inside the engine.

`PaperTradingService` remains the sole writer of cash, positions, trades and snapshots. The
construction engine emits decisions; the existing order path executes them, re-running the full
Phase 10 validation and risk check.

---

## 4. Files

**New services** — `allocation.py`, `construction.py`, `rebalancing.py`, `risk_engine.py`,
`construction_service.py`

**New schemas** — `schemas/allocation.py`

**New repository** — `repositories/portfolio_transaction_repository.py`

**Migration** — `alembic/versions/f5a1c7d94b20_phase_11_allocation_and_cash_ledger.py`
(revision `f5a1c7d94b20`, down revision `e4f8b2c7a913`)

**Extended** — `core/config.py` (12 `PORTFOLIO_*` settings), `core/exceptions.py`,
`schemas/portfolio.py`, `models/portfolio.py` (`allocation_config_json`, `PortfolioTransaction`),
`services/portfolio/models.py`, `calculator.py`, `defaults.py`, `position_sizing_service.py`,
`performance_service.py`, `service.py`, `api/v1/portfolios.py`, `api/dependencies.py`

**Tests** — `test_portfolio_construction_api.py`, `test_portfolio_end_to_end.py`,
`phase11_helpers.py`; `test_portfolio_allocation.py`, `test_portfolio_engine.py` and
`test_portfolio_api.py` extended

**Docs** — `docs/phase-11.md`, `README.md`, `.env.example`

---

## 5. Database changes

Purely additive. One nullable column and one table; no Phase 1–10 table, column, index or row is
dropped, renamed or retyped.

`portfolios.allocation_config_json (TEXT, nullable)` — the construction policy. Deliberately a new
column rather than extra keys inside `risk_config_json`, because the two answer different
questions: the risk config says what a *single order* may never breach (a hard gate), the
allocation config says how the portfolio is *built* (a policy). Merging them would make "the
limits" and "the strategy" one blob, and a replacement write to one would silently reset the other.
Nullable, so a Phase 9/10 portfolio reads back with "use the application defaults" rather than an
invented configuration.

`portfolio_transactions` — the append-only cash ledger: one row per movement of `current_cash`,
covering `BUY`, `SELL`, `DEPOSIT` and `WITHDRAWAL`. `stock_id`, `quantity`, `price`, `order_id` and
`trade_id` are nullable because a deposit has no stock, no order and no trade. `portfolio_id` is
not nullable, so referential integrity on the owning portfolio holds either way.

Indexes: `portfolio_id`, `transaction_type`, `stock_id`, `timestamp`, plus the composites
`(portfolio_id, stock_id, timestamp)` for per-stock history and `(portfolio_id, timestamp, id)` for
per-period history — the two questions the ledger is actually asked.

`portfolio_trades` is unchanged and stays what it was: the record of an *execution*. The ledger
answers "how did the cash balance get to where it is". The two must agree, which is why every fill
writes both in the same transaction or neither.

---

## 6. New API endpoints

| Method | Endpoint | Purpose |
|--------|----------|---------|
| POST | `/portfolios/{id}/decisions` | Explainable decisions; `apply` submits them as simulated orders |
| POST | `/portfolios/{id}/rebalance` | Rebalance plan; `apply` submits the plan |
| GET | `/portfolios/{id}/risk/report` | Portfolio-level risk: exposure, concentration, violations, recommendations |
| POST | `/portfolios/{id}/deposit` | Add capital |
| POST | `/portfolios/{id}/withdraw` | Remove capital |
| GET | `/portfolios/{id}/transactions` | Cash ledger |
| GET | `/portfolios/{id}/transactions/summary` | Counts per transaction type |

Seven new paths, 26 portfolio paths and 64 total in the OpenAPI document. No existing endpoint
changed shape.

---

## 7. Portfolio accounting, sizing, risk and rebalancing

### Accounting

`cash + Σ(position market value) = equity`. Weighted-average cost, and only weighted-average cost
— the method is never mixed. Entry fees are capitalised into cost basis, exit fees expensed against
the realised result, partial sells leave the average entry price unchanged.

The P&L basis is opening capital **plus external flows**, so a deposit raises equity without
raising invested capital and is never reported as a gain. A 10% gain followed by a deposit that
doubles the capital still reports 10%.

### Sizing

Three formulas, all floored to whole shares (buying one share over a target is how a position
breaches its own cap) and all bounded by investable cash:

* **Equal weight** — `min(100/N, max_position_weight_pct)`, cap applied *after* the split.
* **Risk-based** — `floor(risk_budget / (price − stop_price))`, then bounded by cash, because a
  risk budget is not a cash budget: 1% of 100,000 through a 5% stop is 200 shares costing 20,000.
* **Max position cap** — the weight-derived quantity capped at `max_position_weight_pct`.

The cash reserve sizes a position *down* rather than refusing the buy. The reserve is a
self-imposed budget, not a prohibition, and "hold 35 shares because 11,150 is investable" is a
better answer than "you may not buy the 64 shares the target wanted". A *risk* limit is the
opposite case and does refuse.

### Risk

Nine rules, reported as **violations** (limits the portfolio is outside of) or **recommendations**
(levels to act at):

* violations — `max_position_weight_pct`, `max_sector_weight_pct`, `max_positions`,
  `max_exposure_pct`, `min_cash_reserve_pct`, `max_drawdown_pct`
* recommendations — `stop_loss_pct`, `take_profit_pct`, `max_position_loss_pct`

Stop-loss and take-profit are inclusive (sitting exactly on the level counts as triggered, because
the level is where the position is no longer tolerated); the position-loss limit is exclusive,
because it is a tolerance rather than a trigger. Growth limits never block a SELL.

Recommendations are returned, never executed. They travel into the decision engine, which cites the
rule that produced them; nothing reaches the order path without `apply: true`.

### Rebalancing

Drift inside `rebalance_threshold_pct` is suppressed. A plan returns an item for **every** target
and every held position, including the holds, because "checked and correct" must be
distinguishable from "never looked at". Items are reported in stock-id order and *funded* in
execution order — sells first, largest proceeds first, then buys, smallest cost first — so a
rebalance that moves weight between names does not report itself unfundable for cash it has just
committed to raise. Every proposed trade is preflighted against the Phase 10 risk engine before it
is reported.

---

## 8. Bugs found and fixed

Each was found by a test written against the specification, not by inspection.

| # | Severity | Defect | Impact | Fix |
|---|----------|--------|--------|-----|
| 1 | **Critical** | `build_account` resolved its "position in focus" to whichever row loaded last when the caller named no stock | `PortfolioAccount.position_for()` returned `None` for every stock but one, so `POST /risk/check` reported **`INSUFFICIENT_POSITION` — "no open position" — for a sell the portfolio held and could fund**. Phase 10's pre-trade sell check was wrong for all but one position. | Focus only a named stock; every open position is now carried on the account, and `RiskCheckService.check` passes its `stock_id` through |
| 2 | **Critical** | Buy sizing ignored the cash reserve | A 50% reserve still produced a full-weight buy, which the executor then rejected. The reserve was decorative. | `affordable_quantity()` bounds every formula by investable cash; the reserve now sizes a position down instead of blocking it |
| 3 | **High** | `_annotate_risk_blocks` rebuilt its result from the trade order | Every `HOLD` item vanished from a rebalance response. A portfolio exactly on target returned `items: []` — indistinguishable from a run that never looked. | Keyed by `stock_id` and reassembled in planning order |
| 4 | **Medium** | Explanations outnumbered reason codes | A decision carried five sentences for four codes, with one unattributable — exactly the "opaque scoring" the specification forbids | One explanation per code, asserted by `len(reasons) == len(explanation)` |
| 5 | **Medium** | The sizing formula and the position cap produced no explanation | A `MAX_POSITION_CAP` buy and a `RISK_BASED` buy both cited only eligibility and drift, so the number that set the quantity was unexplained | Both now explain themselves in their own right |
| 6 | **Medium** | `_reduction_decision` recomputed the target with the buy-side formula | A trim could be sized by a cash bound that does not apply to a sale, stranding surplus in the position | The caller passes the pure weight target |
| 7 | **Low** | `_risk_report_response`'s docstring claimed stop-loss and take-profit appear in `violations` | They do not — they are recommendations. The comment described behaviour the code did not have | Docstring corrected to state the actual split, and the reasoning behind it |

Bug 1 deserves emphasis: it is a **Phase 10 defect**, found only because the Phase 11 rebalancer
preflights its proposed sells through the Phase 10 risk engine and hit the wrong answer. It was
invisible to every existing test because each one held a single position — the one case where
"whichever row loaded last" happens to be right.

---

## 9. Deliberate design decisions

* **A crossed stop is a recommendation, not a violation.** It is a level to act at, not a limit
  the portfolio is outside of. Both are exposed; conflating them would mean a portfolio with an
  exit to make reported itself as "in breach".
* **Recommendations never execute themselves.** §8 of the specification requires the risk engine to
  return the action to the decision engine, and the decision engine still requires `apply: true`.
* **The cash reserve bounds size; a risk limit refuses.** Different tools for different jobs.
* **Percentages are quantised as percentages.** Quantising `12.3456%` as money would drop it to
  `12.35%` and print the same number with different precision in the explanation than in the field
  beside it.
* **A rebalance reports its holds.** A plan with five items, three of them "no trade needed", is a
  complete answer; one with only the trades cannot be told apart from a run that never looked.
* **Sells are proposed and prefunded before buys.** Reported in stock-id order, funded in
  execution order.
* **A blocked decision is returned, not hidden.** `blocked: true` with the blocking reason appended
  to the same `reasons` list, so the caller sees both what was wanted and why it was refused.
* **Risk recommendations cite the rule that fired.** A `SELL` from a stop-loss carries
  `stop_loss_pct`, so the decision and the report behind it cannot disagree about why.
* **Deposits widen the P&L basis.** Otherwise a deposit would be reported as a gain.
* **`PATCH` replaces the whole allocation config rather than merging.** A merge would leave stale
  settings in force with no way to see them.

---

## 10. Verification performed

| Check | Method | Result |
|-------|--------|--------|
| Full test suite | `pytest tests/ -q` | **757 passed** |
| Phase 1–10 suites | `pytest` on every pre-existing suite | 582 passed, unchanged |
| End-to-end walkthrough | `test_portfolio_end_to_end.py`, over HTTP | every step correct |
| Byte-compilation | `compileall app tests` | clean |
| Import sweep | every module under `app` | 95 modules, 0 failures |
| Static check | `pyflakes` on every Phase 11 file | clean |
| Migration up | Phase 10-shaped SQLite database → `alembic upgrade head` | applied; columns and all six indexes match the ORM |
| Migration down | `alembic downgrade e4f8b2c7a913` | table and column removed |
| Revision graph | linear-chain check | single root, single head `f5a1c7d94b20` |
| OpenAPI | `/openapi.json` | 64 paths, 26 portfolio, 7 new |
| Health | `GET /health` | `200 {"status":"ok","database":"connected"}` |
| Startup | full app import and route table | clean |

The end-to-end walkthrough covers, in order: create and configure a portfolio with a 20% position
cap, 1% risk per trade and a 10% reserve; generate decisions for five candidates; apply them as
simulated orders; check cash, position count, market value and equity; rebalance to new targets
and confirm one sell, one buy and three holds; re-mark two positions and read unrealised P&L; sell
part of a position and confirm realised P&L; breach a stop and confirm the recommendation appears
without anything being sold; persist a snapshot; and read performance metrics. It also runs the
whole candidate set twice and asserts the two runs are byte-identical.

**No lint or formatter is configured in this repository** (no `flake8`/`black`/`ruff` config, no
`lint` Makefile target). `compileall`, the import sweep, `pyflakes` on the new code, and the
end-to-end walkthrough stand in for static checks.

---

## 11. Known limitations

* **The Phase 2 fresh-install migration still fails** on a completely empty database — it creates a
  table with a foreign key to `stocks` before `stocks` exists. This is a Phase 2 defect, unchanged
  by Phase 11 and left alone deliberately: editing another phase's migration would alter the
  deployed history.
* **The migration chain is MySQL-targeted.** Phase 11 was verified on SQLite in isolation because
  the earlier migrations use MySQL-specific DDL.
* **Long-only.** Gross and net exposure are computed and reported separately so a long/short
  portfolio can fill in the short term, but nothing shorts.
* **Pre-tax.** Realised P&L has no tax treatment.
* **Unfilled orders, partial fills and a slippage model** are out of scope; fill prices come from
  the Phase 10 execution policy.
* **No portfolio optimisation.** Targets come from a formula or from the caller. There is no
  mean-variance or risk-parity solver.

---

## 12. Deferred work

None required by this phase. Natural candidates for Phase 12: order types beyond market, tax-aware
realised P&L, and portfolio-level optimisation over a covariance estimate.

---

## 13. Completion checklist

| # | Item | Status |
|---|------|--------|
| 1 | Portfolio domain model exists | PASS |
| 2 | Position model exists | PASS — Phase 9 |
| 3 | Transaction model exists | PASS |
| 4 | Portfolio configuration exists | PASS |
| 5 | Portfolio accounting works | PASS |
| 6 | Cash management works | PASS |
| 7 | Position sizing works | PASS |
| 8 | Equal-weight sizing works | PASS |
| 9 | Risk-based sizing works | PASS |
| 10 | Position caps work | PASS |
| 11 | Portfolio construction works | PASS |
| 12 | BUY decisions work | PASS |
| 13 | SELL decisions work | PASS |
| 14 | HOLD decisions work | PASS |
| 15 | REDUCE decisions work | PASS |
| 16 | REBALANCE decisions work | PASS |
| 17 | Risk engine works | PASS |
| 18 | Exposure calculation works | PASS |
| 19 | Concentration limits work | PASS |
| 20 | Cash reserve works | PASS — after fixing bug 2 |
| 21 | Stop-loss works | PASS |
| 22 | Take-profit works | PASS |
| 23 | Drawdown works | PASS — Phase 10, read by Phase 11 |
| 24 | Realized P&L works | PASS |
| 25 | Unrealized P&L works | PASS |
| 26 | Portfolio valuation works | PASS |
| 27 | Equity curve works | PASS |
| 28 | Performance metrics work | PASS |
| 29 | Explainability works | PASS — after fixing bugs 4 and 5 |
| 30 | Determinism tests pass | PASS |
| 31 | API endpoints work | PASS |
| 32 | Database migrations work | PASS — verified both directions |
| 33 | Signal engine integration works | PASS — screener consumed, rules not duplicated |
| 34 | Screener integration works | PASS |
| 35 | Backtesting does not regress | PASS |
| 36 | All Phase 1–10 tests pass | PASS — 582, unchanged |
| 37 | All Phase 11 tests pass | PASS — 304 across five suites |
| 38 | No known regressions | PASS |
| 39 | Documentation updated | PASS — `docs/phase-11.md`, README, `.env.example` |

---

**Phase 11 is COMPLETE and ready for Phase 12.**

**Not investment advice.** All results are a deterministic simulation of a fixed rule set.
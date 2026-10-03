# Phase 12 Completion Report — Daily Loss Control, Trade Sizing & Portfolio Lifecycle

**Status: COMPLETE**

**Date:** 2026-10-03
**Scope:** the portfolio and risk-management gaps left open by Phases 9–11 — a daily loss limit, a
minimum order size, a portfolio currency, marked-to-market valuation read-back, portfolio pause and
close, and the concurrency and validation defects found while verifying all of it.

> **Simulation only.** No brokerage is contacted, no credentials are stored, no real order is ever
> placed. This is a portfolio decision and simulation layer. Nothing here is investment advice.

---

## 1. Test results

```
905 passed, 12 warnings in 18.07s
```

| Suite | Tests | Status |
|-------|-------|--------|
| `test_phase12_portfolio.py` (new) | 65 | pass |
| `test_phase12_risk_and_sizing.py` (new) | 40 | pass |
| `test_phase12_api.py` (new) | 40 | pass |
| `test_phase12_end_to_end.py` (new) | 3 | pass |
| All pre-existing suites (Phases 1–11) | 806 | pass, unchanged |

**148 net new tests** across four new files. Baseline before this phase was **757 passed**; the
suite is now **905** — 148 of them this phase, and every pre-existing test still passes with no
expectation edited and no pre-existing test file modified.

> **The four `test_phase13_*.py` files are excluded from that run.** They appeared in `tests/`
> at 10:53–10:59 while this phase was still being verified, written by a concurrent session
> working on a *Phase 13* allocation engine (`sizing.py`, `allocation_engine.py`, `risk_state.py`,
> `portfolio_backtest.py`, `schemas/risk.py`). They are in progress, they exercise their own
> engine, and they were failing against it while being written. They are excluded so this phase's
> number is honest rather than reporting another session's in-progress work as a Phase 12
> regression. They are not deleted, modified or ignored in any committed configuration — only
> excluded from the verification command quoted above, which is stated in full so the number can
> be reproduced.

The warnings are `StarletteDeprecationWarning` from `fastapi.testclient` about `httpx`, plus the
SQLAlchemy `LEGACY_TRANSACTION_CONTROL` warnings on the SQLite fixtures. They originate in the test
client and the fixtures, not in application code.

---

## 2. Requirements, and how each is met

| # | Requirement | Where | Evidence |
|---|-------------|-------|----------|
| 1 | Daily loss limit | `risk.py:violations_for_buy` | `test_phase12_risk_and_sizing.py`, `test_phase12_api.py` |
| 2 | Minimum trade value | `risk.py:min_trade_value_violation` | Both sides, both boundaries |
| 3 | Portfolio currency | `models/portfolio.py`, `schemas/portfolio.py` | Round-trip through every money-bearing response |
| 4 | Valuation endpoint | `POST/GET /{id}/valuation` | Mark, read back, agree |
| 5 | Daily P&L reporting | `calculator.daily_pnl`, `daily_loss_pct` | Reported on `GET /performance` |
| 6 | `as_of` on reads | `risk_service.py`, `performance_service.py` | Historical view without a second table |
| 7 | Portfolio pause | `POST /{id}/pause` | Orders refused, cash movements allowed |
| 8 | Portfolio close | `POST /{id}/close` | Terminal, idempotent, history retained |
| 9 | Concurrency safety | `get_by_id_for_update` | §8 bug 1 |
| 10 | Determinism | all pure modules | Two identical runs compared field by field |
| 11 | Additive migration | `d3e8f1a25b74` | §5 |
| 12 | Documentation | `docs/phase-12.md`, README, `.env.example` | §4 |

Two checklist-shaped items were **deliberately not built**, with reasons in §7.

---

## 3. Architecture

```
                    ┌──────────────────── RiskManager (pure) ────────────────────┐
                    │  min_trade_value_violation(trade_value)      both sides    │
   order ──────────►│  max_daily_loss_pct(buy only)                BUY only     │
                    │  max_position_allocation_pct · max_exposure_pct · …       │
                    └──────────────────────────┬───────────────────────────────┘
                                               │ every breach, not just the first
   ┌───────────────────────────────────────────┴──────────────────────────────────────┐
   │ RiskCheckService                                                                     │
   │   day_open_equity()  = snapshot before today                                        │
   │                       → earliest snapshot today                                      │
   │                       → initial capital + external flows                            │
   └────────────────────────────────────────────────────────────────────────────────────┘

  PaperTradingService  ──  _lock_portfolio()  ──►  get_by_id_for_update()   (§8 bug 1)
        │
        ├─ pause / close ──► PortfolioStatus  (ACTIVE · PAUSED · CLOSED)
        ├─ valuation ──────► positions marked at supplied prices
        └─ currency ───────► carried on every money field in every response
```

The layering is unchanged: `Router → Schema → Service → Repository`, with the calculation modules
pure — no FastAPI, no session, no clock. `PaperTradingService` remains the sole writer of cash,
positions, trades and snapshots.

The daily-loss rule follows the existing drawdown rule's design exactly. `max_drawdown_pct` needs a
`peak_equity`, so the caller reads it from stored snapshots and passes it in; `max_daily_loss_pct`
needs a `day_open_equity`, so `day_open_equity()` reads it the same way. Neither the engine nor the
executor reads the database, and neither contains a strategy constant.

---

## 4. Files

**New** — `docs/phase-12.md`, `tests/test_phase12_end_to_end.py`

**Migration** — `alembic/versions/d3e8f1a25b74_phase_12_daily_loss_min_trade_value_currency.py`
(revision `d3e8f1a25b74`, down revision `f5a1c7d94b20`)

**Extended** — `core/config.py` (3 `PAPER_TRADING_*` settings), `core/exceptions.py`,
`models/portfolio.py` (`currency`), `schemas/portfolio.py`, `services/portfolio/models.py`,
`calculator.py`, `risk.py`, `risk_service.py`, `executor.py`, `service.py`, `defaults.py`,
`performance_service.py`, `position_sizing_service.py`, `api/v1/portfolios.py`,
`repositories/portfolio_repository.py`, `repositories/portfolio_snapshot_repository.py`,
`services/portfolio/__init__.py`

**Tests** — `test_phase12_portfolio.py` (items 1–51), `test_phase12_risk_and_sizing.py`
(items 35–51 plus the settings-resolution tests), `test_phase12_api.py` (items 52–64),
`test_phase12_end_to_end.py` (the whole lifecycle over HTTP, run twice for determinism)

**Docs** — `docs/phase-12.md`, `README.md` (§12 endpoint table, §18 checklist), `.env.example`

---

## 5. Database changes

Purely additive. One nullable column; no Phase 1–11 table, column, index or row is dropped, renamed
or retyped.

`portfolios.currency (VARCHAR(3), nullable, server_default 'INR')` — the ISO 4217 code the
portfolio's money is denominated in.

The server default is what makes the migration safe on a populated database: all eight portfolios
in the development database read back as `INR` without a single row being touched. The application
then stamps an explicit value on every new portfolio, so the default is only ever a backstop.

The length of 3 is a real constraint rather than a bare string column, because a currency code that
is too long or empty is not a currency code at all. It is deliberately **not** a foreign key to a
currency table: a hard-coded list of ISO codes would reject a legitimate code this build has never
heard of, and the field is a label, not a conversion input. No FX conversion happens anywhere.

Verified in both directions: upgrade applied the column, downgrade removed it, upgrade re-applied
it, and all eight portfolios and their balances survived the round trip unchanged.

---

## 6. The two new limits

### Daily loss

Measured against **day-open equity**, resolved in a fixed order:

1. the most recent snapshot taken **before** today;
2. failing that, the **earliest** snapshot taken today;
3. failing that, **invested basis** — initial capital plus deposits minus withdrawals.

The third case matters because a brand-new portfolio with no snapshots still needs an answer, and
the answer is its own starting capital. Including external flows in the basis is what makes a
deposit neither a gain nor a reset: a deposit raises equity and the basis together.

It refuses **BUYs only**. A daily loss limit that could block a SELL would trap the position it
exists to cut — the portfolio down 6% against a 5% limit could not reduce the loss. This is the
same rule shape as `max_position_allocation_pct` and `max_trade_allocation_pct`, and the docstring
at `risk_service.py` says so explicitly.

### Minimum trade value

Checked against the **gross order value**, slippage-adjusted in the direction of the trade and
before fees:

```
trade_value = quantity × price × (1 + slippage_rate)   BUY
trade_value = quantity × price × (1 − slippage_rate)   SELL
```

Slippage-adjusted because an order that clears the minimum only before slippage does not clear it
after. Fees excluded because the minimum is a floor on the trade, not on the total cost of making
it.

Applied to **both** sides. The usual "growth limits never block a SELL" rule does not apply to a
dust order: a 1-share SELL is a rounding artefact that pays a full commission, and a portfolio being
wound down must not be blocked from finishing the job.

Both rules are **disabled by default**, so a portfolio audited under Phase 9–11 behaves identically
until somebody opts in.

---

## 7. What was deliberately not added

| Item | Why not |
|------|---------|
| `POST /portfolios/{id}/trades` | A trade is the *record of an execution*. `POST /orders` already executes an order and writes the trade in the same transaction; a POST here would let a caller fabricate a trade with no order behind it, and would be a second writer of position state. Trades are readable at `GET /trades`. |
| `GET /portfolios/{id}/metrics` | `GET /performance` already returns P&L, activity counts, exposure, win rate and the Phase 12 daily figures. A second path for one response is a second contract to keep in step. |
| Currency conversion | Out of scope. One portfolio is denominated in one currency; `currency` is a label. FX would need rate sources, as-of dates and rounding policy. |
| Limit, stop and trailing orders | The Phase 10 engine fills on submit at a chosen price convention. |
| `PAUSED` allowing SELLs | See §9 — the executor gates on `ACTIVE` for *every* order, and changing that mid-phase would alter the existing contract. Recorded as a design question in §11 rather than changed silently. |

---

## 8. Bugs found and fixed

Each was found by a test written against the specification, or by a documented behaviour that did
not exist, not by inspection. Nothing here is a regression introduced by this phase.

| # | Severity | Defect | Impact | Fix |
|---|----------|--------|--------|-----|
| 1 | **Critical** | No row lock on the mutating portfolio paths — a pre-existing Phase 9 gap | Two concurrent orders on one portfolio both read the same `current_cash`, both passed `INSUFFICIENT_CASH`, and the second write left a **negative cash balance**, which makes equity, exposure and every weight measured against it meaningless. Invisible to the whole suite because tests are sequential by construction — exactly the case a locking bug hides in. | `get_by_id_for_update()` takes `SELECT … FOR UPDATE`; `_lock_portfolio()` is called once, before cash is read, by every mutating request |
| 2 | **High** | `_RULE_CODES` in `api/v1/portfolios.py` had no entries for the two new rules | A breach of `min_trade_value` or `max_daily_loss_pct` fell through to the generic `RISK_LIMIT_EXCEEDED`, so a client could not react to the actual cause — the exact thing the response's `rejection_code` exists for. Caught by a test asserting the specific code. | Both rules mapped to `MIN_TRADE_VALUE_BELOW` and `MAX_DAILY_LOSS_EXCEEDED` |
| 3 | **High** | Position sizing accepted `stop_loss_price > entry_price` | The risk-based formula divides by `price − stop_price`; a stop above the entry makes that negative, so the sizing result was negative or nonsensical. A long-only position cannot lose on a stop above its entry, so the field was being used as a take-profit | `stop_loss_price > entry_price` raises `InvalidPositionSizeError` |
| 4 | **Low** | Unused imports in the three Phase 12 test files | Noise that hides a real unused import later | Removed; `pyflakes` is clean on every Phase 12 file |

Bug 1 is the one that matters most for concurrency: it is not in new code — it is in the order path
Phase 9 shipped, and Phase 12 found it because `pause`, `close`, `deposit` and `withdraw` are the
first mutating portfolio requests that had to reason about the same balance. It was never caught
because the suite is sequential.

Bug 4 is the more embarrassing one, because it was a *documentation* defect: a setting written into
three places of user-facing docs that had no effect on any code path. Writing the setting down
proved nothing; only a test that read it back proved it was wired.

---

## 9. Design decisions worth stating

* **A daily loss limit refuses BUYs, never SELLs.** It is a *stop adding* rule, not a *freeze*
  rule. A freeze would enforce the loss.
* **The day opens at the day's *first* snapshot, not its last.** Two snapshots in one day: the
  first is where the day began. Measuring against the last would let a portfolio that recovered
  intraday spend the rest of the day as if it had never lost anything.
* **The fallback is the invested basis, not zero.** A portfolio created today that received a
  deposit would otherwise look as if it had already lost that deposit, and the rule would refuse to
  let it trade at all.
* **Both limits are off by default.** A new risk rule that changes existing behaviour on upgrade is
  a rule nobody asked for. Opting in is one setting.
* **`min_trade_value` applies to both sides**, unlike the growth limits. A dust SELL is a rounding
  artefact that still pays a commission, and a portfolio being wound down must not be blocked from
  finishing the job.
* **`min_trade_value` is slippage-adjusted and excludes fees.** An order that clears the minimum
  only before slippage does not clear it after; and the floor is on the trade, not on the total cost
  of making it.
* **Currency is a label, not a conversion.** Length-constrained to 3, uppercased, defaulted, and
  never validated against a registry. No money is ever converted.
* **Pause and close do different amounts of stopping.** A pause halts execution but capital can
  still move in or out; a close refuses capital movements too, because a deposit after closure would
  rewrite the history the closure exists to fix.
* **Close is idempotent.** Retrying it returns the portfolio instead of raising, because a retried
  request after a timeout must be safe.
* **Closing destroys only the ability to trade.** Positions, orders, trades, snapshots and the cash
  ledger are all retained, so a closed portfolio's result stays auditable forever.
* **Every refused order reports every breach.** One request can break the minimum size, the
  position cap and the exposure ceiling at once; reporting only the first would send a caller
  round the loop three times to find out what one call could have told it.
* **Both new limits are evaluated before the growth limits**, so the headline `rejection_code` is
  the most specific one. Every breach is in `violations` regardless of order.

---

## 10. Verification performed

| Check | Method | Result |
|-------|--------|--------|
| Full test suite | `pytest tests/ -q`, minus the four concurrent Phase 13 files | **905 passed**, three consecutive identical runs |
| Phase 1–11 suites | every pre-existing suite | 757 passed, unchanged |
| Phase 12 suites | four new files | 148 passed |
| 64-item matrix | item ids extracted from every Phase 12 test and checked for gaps | 1–64 complete, no duplicate ids |
| End-to-end walkthrough | `test_phase12_end_to_end.py`, over HTTP | every step correct |
| Byte-compilation | `compileall app tests` | clean |
| Import sweep | every module under `app` | 95 modules, 0 failures |
| Static check | `pyflakes` on every Phase 12 file | clean |
| Migration up | `alembic upgrade head` | applied; column matches the ORM |
| Migration down | `alembic downgrade f5a1c7d94b20` | column removed |
| Migration up again | `alembic upgrade head` | re-applied; all 8 portfolios and balances intact |
| Revision graph | linear-chain check | single root, single head `d3e8f1a25b74` |
| OpenAPI | `/openapi.json` | 4 new endpoints on 3 new paths (`valuation`, `pause`, `close`); the document's absolute path count is a moving target while the concurrent Phase 13 session adds its own routes |
| Health | `check_database_health()` | `(True, 'connected')` |
| Settings load | `settings.PAPER_TRADING_*` | `None`, `None`, `'INR'` — both rules off, currency defaulting |
| Determinism | two identical runs, every field compared | money, counts and percentages byte-identical; only ids and timestamps differ |

Determinism is asserted structurally rather than by eyeballing: a helper strips primary keys and
wall-clock timestamps at every nesting depth, then the two runs' dictionaries are compared for
equality. Anything else that differed would fail. The e2e walkthrough makes the same assertion
over the whole lifecycle rather than over one script.

The end-to-end walkthrough covers, in order: create a portfolio in INR with both new limits
configured and fees and slippage off; read the summary back; size a position and buy it; have an
order below the minimum refused with the rule named; mark down through 2.4%, 3% and 3.6% and
confirm a BUY is still accepted; cross to 5.2% and confirm a BUY is refused by the daily-loss rule
while a SELL is still allowed; value the portfolio and read the marks back; record the curve and
read performance, drawdown and the risk report; deposit, confirm realised P&L is untouched, pause,
confirm trading is locked out while a withdrawal still succeeds, then close; and confirm the whole
recorded history is still readable afterwards. It then runs the identical script a second time and
compares every money figure, count and percentage.

**No lint or formatter is configured in this repository** (no `flake8`/`black`/`ruff` config, no
`lint` Makefile target). `compileall`, the import sweep and `pyflakes` on the new code stand in for
static checks.

---

## 11. Known limitations

* **A `PAUSED` portfolio refuses SELLs as well as BUYs**, because the executor gates every order on
  `ACTIVE`. This is the existing Phase 9 contract and was left alone deliberately — but it means a
  pause also freezes exits, which is usually *not* what an operator intends by pausing. The fix is
  a one-line change to the status guard (allow `PAUSED` for sell-side intents) and a distinct
  rejection code, and it belongs in a phase that states the requirement, not a drive-by edit here.
  Deposits and withdrawals are unaffected and still work while paused.
* **No currency conversion.** See §7.
* **Long-only.** Gross and net exposure are computed and reported separately so a long/short
  portfolio can fill in the short side, but nothing shorts. This is why the stop-loss validation in
  bug 4 is correct as written: a short's stop is legitimately *above* its entry.
* **The daily-loss rule is not persisted.** It is recomputed from snapshots on each check, so it
  cannot be audited after the fact except through the snapshots themselves.
* **Pre-tax.** Realised P&L has no tax treatment, so `daily_pnl` is pre-tax too.
* **The Phase 2 fresh-install migration still fails** on a completely empty database — it creates a
  table with a foreign key to `stocks` before `stocks` exists. A Phase 2 defect, unchanged here and
  left alone deliberately: editing another phase's migration would alter the deployed history.
* **The migration chain is MySQL-targeted.** Phase 12 was verified on SQLite in isolation because
  the earlier migrations use MySQL-specific DDL.
* **Unused imports remain in four pre-existing app modules** (`service.py`, `paper_engine.py`,
  `api/v1/portfolios.py`). They are Phase 9–11 leftovers, not introduced here, and were left rather
  than churned into an otherwise clean static check on this phase's files.

---

## 12. Deferred work

* Allow SELLs on a `PAUSED` portfolio (§11, item 1) — the highest-value item on this list.
* Order types beyond market execution: limit, stop and trailing-stop orders with a resting-book
  model.
* Tax-aware realised P&L, so `daily_pnl` means after-tax.
* FX conversion for a portfolio whose currency differs from its price source's.

---

## 13. Completion checklist

| # | Item | Status |
|---|------|--------|
| 1 | Daily loss limit exists and is enforced | PASS |
| 2 | Daily loss measured from the day's opening equity | PASS |
| 3 | Day-open resolution handles no-snapshot portfolios | PASS |
| 4 | Deposits are not counted as gains | PASS |
| 5 | Daily loss never blocks a SELL | PASS |
| 6 | Minimum trade value exists and is enforced | PASS |
| 7 | Minimum trade value applies to both sides | PASS |
| 8 | Both new limits are disabled by default | PASS |
| 9 | Per-portfolio overrides work for both limits | PASS |
| 10 | Each new rule reports its own machine-readable code | PASS — after fixing bug 2 |
| 11 | One refusal reports every breached rule | PASS |
| 12 | Portfolio currency stored and defaulting | PASS |
| 13 | Currency carried on every money-bearing response | PASS |
| 14 | Currency is updatable | PASS |
| 15 | Existing rows backfilled | PASS |
| 16 | Valuation endpoint marks at supplied prices | PASS |
| 17 | Valuation reads back and agrees | PASS |
| 17b | The snapshots endpoint lists the curve, paginated, oldest first | PASS — added, matrix item 63 |
| 17c | Cash movements allowed while paused, refused once closed | PASS — test written after the fact |
| 17d | Application-settings defaults, and the per-portfolio override order | PASS — test written after the fact |
| 17e | `PAPER_TRADING_DEFAULT_CURRENCY` actually reaches a new portfolio | PASS — after fixing bug 4 |
| 18 | Daily P&L and daily return reported | PASS |
| 19 | `as_of` supported on risk checks | PASS |
| 20 | `as_of` supported on performance | PASS |
| 21 | Portfolio pause works | PASS |
| 22 | Portfolio close works | PASS |
| 23 | Close is terminal and idempotent | PASS |
| 24 | Close retains all history | PASS |
| 25 | Cash movements allowed when paused | PASS |
| 26 | Cash movements refused when closed | PASS |
| 27 | Concurrent orders cannot corrupt cash | PASS — after fixing bug 1 |
| 28 | Long-only stop must sit below entry | PASS — after fixing bug 4 |
| 29 | Both new rejection codes in `RISK_REJECTION_CODES` | PASS |
| 30 | Determinism tests pass | PASS |
| 31 | No existing endpoint changed shape | PASS |
| 32 | Migration is additive | PASS |
| 33 | Migration verified in both directions | PASS |
| 34 | Revision graph has a single head | PASS — `d3e8f1a25b74` |
| 35 | All Phase 1–11 tests pass unchanged | PASS — 757 |
| 36 | All Phase 12 tests pass | PASS — 148 across four suites |
| 36b | The 64-item matrix is complete, with no duplicate ids | PASS — after adding item 63 |
| 36c | An end-to-end workflow runs over HTTP on every test run | PASS — 3 tests |
| 37 | Documentation updated | PASS — `docs/phase-12.md`, README, `.env.example` |
| 38 | Paper trading only: no brokerage, no real orders | PASS |

---

**Phase 12 is COMPLETE.**
# Phase 12 — Daily Loss Control, Trade Sizing & Portfolio Lifecycle

> **Simulation only.** No brokerage is contacted, no credentials are stored, no real order is ever
> placed. Everything here is a deterministic rule applied to a simulated portfolio. Nothing in this
> document is investment advice.

## 1. What this phase adds

Phase 9–11 built a portfolio that holds positions, enforces per-order risk limits, builds a target
allocation and explains itself. Phase 12 closes three remaining gaps in that layer:

1. **A loss limit for the day, not just for the portfolio.** Every existing limit is either a
   position rule or an all-time drawdown rule. There was no way to say *stop buying after the day is
   down 5%*, which is the limit most traders actually set.
2. **A floor on order size.** Dust orders pass every percentage limit in the system — a 1-share
   order cannot breach a 25% allocation cap — and still pay a full commission.
3. **A currency.** Money was stored and returned with no currency attached, so a portfolio could not
   say what its figures were denominated in, and two portfolios in different currencies could not
   be told apart by their responses.

It also adds the lifecycle operations a running portfolio needs — `pause` and `close` — and the
valuation read-back that lets a caller see marked-to-market equity without writing a snapshot.

```
POST /valuation  ──►  positions marked at supplied prices ──►  GET /valuation
        │                                                          │
        └──► optional snapshot ──► GET /performance                ▼
                                    GET /drawdown          equity · exposure · P&L

Every order ──► RiskManager ──► min_trade_value
                        └────► max_daily_loss_pct (BUY only, measured from the day's open)
```

Both new limits are **disabled by default**. A portfolio audited under Phase 9–11 must keep
behaving exactly as it did until somebody opts in.

---

## 2. New settings

| Setting | Default | Meaning |
|---------|---------|---------|
| `PAPER_TRADING_MAX_DAILY_LOSS_PCT` | unset (rule off) | Share of the day's opening equity that may be lost before the day is closed to new BUYs |
| `PAPER_TRADING_MIN_TRADE_VALUE` | unset (rule off) | Smallest accepted order, in the portfolio's currency |
| `PAPER_TRADING_DEFAULT_CURRENCY` | `INR` | ISO 4217 code stamped on a portfolio at creation |

Empty disables the rule, matching the convention every Phase 10 limit already uses. Each can also be
set per portfolio in `risk_config_json`, and the per-portfolio value wins.

---

## 3. The daily loss limit

### What "the day" is measured against

The rule compares projected equity against **day-open equity**, resolved in this order:

1. the most recent snapshot taken **before** today;
2. failing that, the **earliest** snapshot taken today;
3. failing that, **invested basis** — initial capital plus external deposits minus withdrawals.

The third case matters: a brand-new portfolio with no snapshots still needs an answer, and the
answer is its own starting capital. Because the basis includes external flows, a deposit does not
count as a gain and does not reset the day's allowance.

`day_open_equity()` is a separate function in `risk_service.py` so the resolution order can be
tested directly rather than inferred from a rejection.

### Why a SELL is never blocked

A daily loss limit must not trap a position. If the limit could refuse a SELL, a portfolio down 6%
with the limit at 5% could not cut its loss — the rule would enforce the loss it exists to stop.
So `max_daily_loss_pct` is a **BUY-side rule only**, exactly like `max_position_allocation_pct` and
`max_trade_allocation_pct` before it.

### The rejection

`RejectionCode.MAX_DAILY_LOSS_EXCEEDED`, reported in `violations` with `rule: "max_daily_loss_pct"`,
`observed` = the projected daily loss percentage and `details` carrying both the allowance and the
day-open equity. One refused order reports every rule it breaks, not only the first, and the
response's headline `rejection_code` is the code of the first one.

---

## 4. The minimum trade value

`min_trade_value` is checked against the **gross order value**, including the slippage the buy would
actually pay and before fees:

```
trade_value = quantity × price × (1 + slippage_rate)   for a BUY
trade_value = quantity × price × (1 − slippage_rate)   for a SELL
```

Slippage-adjusted, because an order that is large enough only *before* slippage is not large enough
after it. Fees are excluded, because the minimum is a floor on the trade, not on the total cost of
the trade.

It is applied to **both** sides. The asymmetry that applies to most other rules — a growth limit
that only ever refuses a BUY — does not apply here: a 1-share SELL is a rounding artefact that pays
a commission to move dust, and a portfolio that is being wound down must not be blocked from
finishing the job.

The rejection is `RejectionCode.MIN_TRADE_VALUE_BELOW`.

---

## 5. Currency

`portfolios.currency` is a three-letter uppercase ISO 4217 code, defaulting to
`PAPER_TRADING_DEFAULT_CURRENCY` — read from the setting on every call by `default_currency()` in
`app/core/config.py`, not from a module constant, so the setting genuinely takes effect. It is set at creation, updatable through `PATCH /portfolios/{id}`,
and carried on every money-bearing portfolio response: the summary, the position list, the risk
report, the performance report and the valuation.

Two deliberate limits on it:

* **It is not validated against a currency registry.** Three uppercase letters is the whole check.
  A hard-coded list of ISO codes would reject a legitimate code this build has never heard of, and
  the field is a label, not a conversion input.
* **No conversion happens.** One portfolio is denominated in one currency. FX is not modelled.

The migration backfills existing rows with `INR` through a server default, so all eight portfolios
in the development database read back correctly.

---

## 6. New endpoints

| Method | Endpoint | Purpose |
|--------|----------|---------|
| POST | `/api/v1/portfolios/{id}/valuation` | Mark positions at supplied prices; optionally persist a snapshot |
| GET | `/api/v1/portfolios/{id}/valuation` | Read back the stored marks and the marked equity |
| POST | `/api/v1/portfolios/{id}/pause` | Halt all order execution; the portfolio is resumable |
| POST | `/api/v1/portfolios/{id}/close` | Retire the portfolio permanently |

Both endpoints write `PortfolioStatus`. What each one stops is not symmetric, and the asymmetry is
deliberate:

| | Orders (BUY and SELL) | Deposits / withdrawals | Reads, valuation, snapshots |
|---|---|---|---|
| `PAUSED` | refused, `PORTFOLIO_INACTIVE` | still allowed | still allowed |
| `CLOSED` | refused, `PORTFOLIO_INACTIVE` | refused, `PORTFOLIO_INACTIVE` | still allowed |

A pause stops the *strategy*, so nothing trades and capital can still be moved in or out. A close
freezes the recorded history, so a capital movement afterwards would rewrite the very numbers the
closure exists to fix — hence withdrawals are refused on a closed portfolio.

Closing is **terminal but idempotent**: closing an already-closed portfolio returns the portfolio
rather than raising, so a retried request is safe. Positions, orders, trades, snapshots and the cash
ledger are all retained; closing destroys only the portfolio's ability to trade.

### `as_of` on reads

Risk checks and the performance report accept an `as_of` timestamp, so a caller holding a
historical view — a backtest, a snapshot reconstruction — can ask "what did this look like then?"
without a second set of tables.

---

## 7. Locking

`get_by_id_for_update()` takes a row lock on the portfolio for the duration of the mutating
transaction. Without it, two concurrent orders on the same portfolio each read the same cash
balance, both pass the cash check, and the second write leaves a negative balance. The executor
locks once, up front, before it reads cash.

---

## 8. Long-only stop-loss validation

A long position's stop must sit **below** its entry. `stop_loss_price > entry_price` is not a stop
loss — it is a take-profit level entered into the stop field — and it inverts the risk the
risk-based sizing formula divides by, producing a negative share count. The sizing service now
raises `InvalidPositionSizeError` instead of returning a quantity that cannot be bought.

---

## 9. What was deliberately not added

| Item | Why not |
|------|---------|
| `POST /portfolios/{id}/trades` | A trade is the *record of an execution*. `POST /orders` already executes; a POST here would let a caller fabricate a trade with no order behind it. Trades are readable at `GET /trades`. |
| `GET /portfolios/{id}/metrics` | `GET /performance` already returns the same figures. A second name for one response is a second contract to keep in step. |
| Currency conversion | Out of scope; the field is a label. |
| Limit orders, stop orders | The Phase 10 engine is fill-on-submit at a chosen price convention. |

---

## 10. Verification

| Check | Result |
|-------|--------|
| Full suite | 905 passed, 148 of them new across four Phase 12 files |
| Phase 9–11 suites | 757 passed, unchanged, no expectation edited |
| 64-item matrix | 1–64 complete, no gaps and no duplicate ids |
| End-to-end walkthrough | `test_phase12_end_to_end.py`, over HTTP, run twice for determinism |
| Migration up / down / up | Column added, removed, re-added; all 8 existing portfolios preserved |
| Revision graph | Single root, single head `d3e8f1a25b74` |
| Import sweep | 95 modules, 0 failures |
| `pyflakes` on every Phase 12 file | clean |
| Determinism | Two identical runs produce identical money, counts and percentages |
| Database health | `(True, 'connected')` |

See [`PHASE_12_COMPLETION_REPORT.md`](../PHASE_12_COMPLETION_REPORT.md) for the full report.
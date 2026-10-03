# Phase 7 — Technical Signal Engine & Technical Screener

> **Disclaimer — these signals do not guarantee outcomes.**
> Everything in Phase 7 is a *description of current technical conditions* computed
> by fixed, deterministic rules over historical prices. A `bullish_alignment`
> classification means several indicators currently agree in direction. It does
> **not** mean the price will rise, it is not a prediction, it is not a probability,
> and it is not financial advice. Technical indicators describe past price action;
> they do not cause future price action. Never make investment decisions on the
> basis of these outputs alone.

---

## Table of Contents

1. [Architecture](#1-architecture)
2. [Signal Definitions](#2-signal-definitions)
3. [Indicator Rules](#3-indicator-rules)
4. [Score Methodology](#4-score-methodology)
5. [Screener Filters](#5-screener-filters)
6. [API Examples](#6-api-examples)
7. [Error Handling](#7-error-handling)
8. [Testing](#8-testing)
9. [Future Extensions](#9-future-extensions)

---

## 1. Architecture

### 1.1 Layer flow

Phase 7 follows the existing project layering exactly. **No database query and no
indicator arithmetic lives in a router.**

```
HTTP request
   │
   ▼
app/api/v1/signals.py        app/api/v1/screeners.py      ← routing + DI only
   │                              │
   ▼                              ▼
SignalService                 ScreenerService               ← orchestration
   │                              │
   ▼                              │
SignalEngine  (rule evaluation)  │                         ← pure functions
   │                              │
   ▼                              │
AnalyticsService (Phase 6) ──────┘                        ← indicator math
   │
   ▼
repositories/ → SQLAlchemy models → MySQL
```

### 1.2 Files

| File | Responsibility |
|------|----------------|
| `app/api/v1/signals.py` | Three signal routes, summaries, status codes, DI wiring |
| `app/api/v1/screeners.py` | One screener route, request validation entry point |
| `app/services/signal_service.py` | `SignalEngine` (rules) + `SignalService` (data access) |
| `app/services/screener_service.py` | Candidate selection and filter matching |
| `app/schemas/signals.py` | Signal enums, `SignalItem`, `CompositeSignalResponse` |
| `app/schemas/screeners.py` | `TechnicalScreenerRequest` / `Result` / `Response` |

### 1.3 Two collaborating classes

`SignalEngine` is **stateless and pure**. It takes an `IndicatorContext` and returns
signals. It has no session, no repository, no clock, no randomness. This is what
makes it trivially unit-testable and deterministic.

`SignalService` owns everything impure: loading prices, calling the Phase 6
`AnalyticsService`, deciding the `status` when data is missing, and translating the
engine's output into response models.

The screener depends on `SignalService` (not on `SignalEngine`), so a screener and a
single-stock request always agree by construction — there is one scoring path.

### 1.4 Deliberate decisions

**No `technical_signals` table.** Signals are a pure function of stored prices. A
persisted cache would add a staleness class of bug (cached signals disagreeing with
current prices) for no correctness gain, so `GET /stocks/{id}/signals/history` is
intentionally not implemented. A future phase can add a snapshot table if
backtesting requires it.

**Bollinger Bands and volume average live in `AnalyticsService`, not the engine.**
Phase 6 had no Bollinger Band or volume-average calculation. Rather than duplicating
band/volume math inside the signal engine (which would violate the "reuse Phase 6"
rule and create two places to fix a formula), both were added to the existing
analytics service alongside `calculate_sma`. They are exposed at
`GET /stocks/{id}/analytics/bollinger-bands` and `GET /stocks/{id}/analytics/volume`
so the calculations are independently verifiable and follow Phase 6's
one-endpoint-per-calculation convention. No Phase 6 behaviour changed.

**No ML, no LLM, no external calls.** Every rule is arithmetic on stored numbers.

---

## 2. Signal Definitions

Each signal carries a `direction`, a `strength` (0–100), a `strength_band`, the
`value` that triggered it, the `reference` threshold it was compared against, and a
plain-English `explanation`.

### 2.1 `strength` and `strength_band`

`strength` is a **rule-derived intensity score between 0 and 100**. It answers
"how far, proportionally, is this condition from its threshold" — it is **not** a
probability, **not** a confidence score, and **not** a probability of profit.

| Band | Range |
|------|-------|
| `weak` | 0–39 |
| `moderate` | 40–69 |
| `strong` | 70–100 |

### 2.2 The signals

| Indicator | Type | Direction logic |
|-----------|------|-----------------|
| `rsi` | `momentum` | `bearish` above 70, `bullish` below 30, otherwise no RSI signal |
| `price_vs_ma_20` / `price_vs_ma_50` | `trend` | `bullish` when close > SMA, `bearish` when close < SMA, none when equal |
| `ma_alignment_sma_20_50` | `trend` | follows SMA(20) vs SMA(50) |
| `ma_alignment_ema_12_26` | `trend` | follows EMA(12) vs EMA(26) |
| `macd` | `momentum` | `bullish` when MACD line > signal line, `bearish` when below |
| `bollinger_bands` | `volatility` | `bearish` at/below lower band, `bullish` at/above upper band, else `neutral` |
| `volume` | `volume` | always `neutral` — participation is not a price direction |
| `momentum` | `momentum` | `bullish` on positive period return, `bearish` on negative |

A signal is only emitted when its indicators are actually available. If the data
does not exist, the signal is **absent** — never defaulted, zero-filled, or guessed.

### 2.3 Explanations describe conditions, never forecasts

Every explanation states what is true now. Examples of the accepted form:

> "RSI is above the overbought level of 70 (RSI 71.83); this is a high-momentum condition."

> "Latest close 218.0000 is above the 50-period moving average 172.3000."

> "Latest close 218.0000 is between the Bollinger bands 175.3357 and 222.6643."

The engine never writes "will rise", "will bounce", "should buy", "expected return",
or any similar forward-looking claim. `tests/test_signals.py` asserts this: every
explanation in every response is scanned for forbidden predictive/advice vocabulary.

### 2.4 Volume signals are always neutral

`volume` is reported as `neutral` by design. High participation is a description of
trading activity, not a statement about direction. Because it is `neutral`, it
contributes **0** to the composite score (see §4), while still being visible in the
response as a genuine condition.

---

## 3. Indicator Rules

All indicator values come from `AnalyticsService` (Phase 6) plus the two additive
Phase 6 calculations noted in §1.4. The signal engine never recomputes them.

| Indicator | Calculation | Minimum records |
|-----------|-------------|-----------------|
| SMA(20/50) | arithmetic mean of closes | 20 / 50 |
| EMA(12/26) | exponential moving average | period + signal span |
| RSI(14) | Wilder smoothing, `0–100` | 15 |
| MACD(12,26,9) | EMA(12) − EMA(26) vs EMA(9) of the MACD line | 35 |
| Bollinger(20, 2σ) | SMA ± 2 × population stddev of closes | 20 |
| Volume average(20) | mean volume over the window | 1 |
| Return | close-to-close change over the most recent period | 2 |

**`MIN_SIGNAL_RECORDS = 15`.** Below this threshold no signals are produced at all and
the response reports `insufficient_data`. With 15 or more records signals *are*
produced, and any indicator that still lacks enough history is listed in
`indicator_snapshot.unavailable_indicators` and simply omitted from `signals`.

The strengths themselves scale with distance from the threshold, using documented
constants in `signal_service.py` — for example `_ma_distance_strength` maps the
proportional distance between price and the average onto 0–100, and `_rsi_strength`
maps distance beyond the 30/70 bands. Every threshold and cap in that module is a
named, documented constant; there are no unexplained magic numbers.

---

## 4. Score Methodology

### 4.1 Goal

The composite score must not let one loud indicator dominate, must not double-count
correlated evidence, and must never report a single extreme reading as "strong".

### 4.2 Family capping — how double-counting is avoided

Price above the 20-SMA, the 20-SMA above the 50-SMA, and the EMA alignment are three
signals that are largely the *same fact* ("this stock is trending up"). Naively
summing them would triple-count one observation.

So signals are grouped into **families**:

| Family | Signals | Cap |
|--------|---------|-----|
| `moving_average` | `price_vs_ma_*`, `ma_alignment_*` | 120 |
| `macd` | `macd` | 80 |
| `rsi` | `rsi` | 70 |
| `crossover` | (reserved) | 60 |
| `bollinger_bands` | `bollinger_bands` | 50 |
| `momentum` | `momentum` | 40 |
| `volume` | `volume` | 40 |

Each family's **net** contribution (bullish `+strength`, bearish `−strength`,
neutral `0`, summed within the family) is then clamped to that family's cap. One
family therefore cannot exceed its share of the total regardless of how many signals
it contains.

### 4.3 Normalisation

```
score = Σ( clamped family contributions ) / TOTAL_FAMILY_CAP × 100
```

where `TOTAL_FAMILY_CAP = 460` (the sum of all family caps) is **fixed**. Dividing by
a constant — not by the caps of the families that happened to have data — is what
prevents a lone signal from being reported as a strong alignment. A stock where only
one family is evaluable can still only reach roughly a quarter of the scale.

The result is a rounded integer in **−100 … +100**.

### 4.4 Classification

| Score | Classification |
|-------|----------------|
| ≥ 60 | `strong_bullish_alignment` |
| 20 … 59 | `bullish_alignment` |
| −19 … 19 | `neutral_alignment` |
| −59 … −20 | `bearish_alignment` |
| ≤ −60 | `strong_bearish_alignment` |

These are alignment descriptors. They are **never** `BUY` / `SELL` / `HOLD`.

### 4.5 A sixth value that is not an alignment

`insufficient_data` is returned as the classification whenever the status is not
`ok`. It exists so that "we could not evaluate this" can never be misread as "this
is neutral".

### 4.6 Observed behaviour

| Price shape | Score | Classification |
|-------------|-------|----------------|
| decline then rally | `+26` | `bullish_alignment` |
| rally then decline | `−31` | `bearish_alignment` |
| perfectly monotonic rise | `+1` | `neutral_alignment` |
| perfectly monotonic fall | `−12` | `neutral_alignment` |

The last two rows are a genuine and intentional property, not a bug: a series that
never reverses pins RSI at 100 or 0, so the overbought/oversold reading (bearish /
bullish) cancels the trend evidence exactly. Real markets do not produce
never-reversing series; a synthetic one does. This is documented and covered by
`test_api_monotonic_series_report_mixed_alignment`.

---

## 5. Screener Filters

`POST /api/v1/screeners/technical` accepts all of the following. Every field is
optional; with no filters, every stock that has evaluable signals is returned.

| Field | Type | Validation | Meaning |
|-------|------|-----------|---------|
| `min_score` | int | −100 … 100 | lower bound on composite score |
| `max_score` | int | −100 … 100 | upper bound on composite score |
| `direction` | enum | `bullish` \| `bearish` \| `neutral` | match alignment family |
| `min_rsi` | float | 0 … 100 | lower bound on RSI |
| `max_rsi` | float | 0 … 100 | upper bound on RSI |
| `price_above_sma` | int | 1 … 500 | keep stocks whose close is above SMA(period) |
| `price_below_sma` | int | 1 … 500 | keep stocks whose close is below SMA(period) |
| `macd_direction` | enum | `bullish` \| `bearish` | MACD line vs its signal line |
| `min_volume` | int | ≥ 0 | latest traded volume floor |
| `symbols` | string[] | ≤ 200 entries | restrict to these symbols |
| `skip` | int | ≥ 0 | pagination offset |
| `limit` | int | 1 … 500 | page size (project convention) |

### 5.1 Cross-field validation

| Rule | Error |
|------|-------|
| `min_score` ≤ `max_score` | 422 `VALIDATION_ERROR` |
| `min_rsi` ≤ `max_rsi` | 422 `VALIDATION_ERROR` |
| `price_above_sma` and `price_below_sma` are mutually exclusive | 422 `VALIDATION_ERROR` |

These are enforced by a Pydantic `model_validator`, so they are rejected before any
query runs.

### 5.2 Missing data in filtering

A filter is **positive evidence**. If a stock lacks RSI, it cannot satisfy
`min_rsi`; if it lacks the requested SMA, it cannot satisfy `price_above_sma`. These
stocks are excluded rather than treated as zero or as passing.

Stocks with no price data or too little history never appear in `results`. They are
reported separately as `skipped_insufficient_data`, so the caller can tell "nothing
matched" apart from "much of the universe could not be evaluated".

### 5.3 Result ordering

Results are ordered by `composite_score` descending, then `stock_id` ascending for
determinism.

---

## 6. API Examples

All examples assume the server is running on `http://127.0.0.1:8000` and are fully
visible with request/response schemas at `/docs`.

### 6.1 Signal summary

```bash
curl "http://127.0.0.1:8000/api/v1/stocks/85/signals"
```

```json
{
  "stock_id": 85,
  "symbol": "SIGCLS43D514",
  "as_of": "2025-04-10",
  "status": "ok",
  "message": null,
  "composite_score": 26,
  "classification": "bullish_alignment",
  "signal_count": 9,
  "bullish_count": 6,
  "bearish_count": 1,
  "neutral_count": 2,
  "signals": [
    {
      "indicator": "rsi",
      "signal_type": "momentum",
      "direction": "bearish",
      "strength": 100,
      "strength_band": "strong",
      "value": "97.14238...",
      "reference": "70",
      "explanation": "RSI is above the overbought level of 70 (RSI 97.14); this is a high-momentum condition.",
      "as_of": "2025-04-10"
    },
    {
      "indicator": "price_vs_ma_20",
      "signal_type": "trend",
      "direction": "bullish",
      "strength": 100,
      "strength_band": "strong",
      "value": "218.0000",
      "reference": "199.0000",
      "explanation": "Latest close 218.0000 is above the 20-period moving average 199.0000.",
      "as_of": "2025-04-10"
    }
  ],
  "indicator_snapshot": {
    "latest_price": "218.0000",
    "previous_price": "216.0000",
    "rsi": "97.14238...",
    "sma": { "20": "199.0000", "50": "172.3000" },
    "ema": { "12": "207.0244...", "26": "194.8642..." },
    "macd_line": "12.1601...",
    "macd_signal": "11.3436...",
    "macd_histogram": "0.8165...",
    "bollinger_upper": "222.6643...",
    "bollinger_middle": "199.0000",
    "bollinger_lower": "175.3356...",
    "volume": 1000000,
    "volume_average": "1000000",
    "records": 100,
    "unavailable_indicators": []
  }
}
```

Note how RSI being extreme is reported as `bearish` while the moving averages are
`bullish`, and the family-capped score lands at a moderate `+26` rather than being
decided by either one.

### 6.2 Strongest single signal

```bash
curl "http://127.0.0.1:8000/api/v1/stocks/85/signals/latest"
```

### 6.3 Classification only

```bash
curl "http://127.0.0.1:8000/api/v1/stocks/85/signals/classification"
# "bullish_alignment"
```

### 6.4 Bullish screener

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/screeners/technical" \
  -H "Content-Type: application/json" \
  -d '{"direction": "bullish", "min_score": 20, "limit": 5}'
```

```json
{
  "count": 11,
  "total": 85,
  "skipped_insufficient_data": 28,
  "skip": 0,
  "limit": 5,
  "results": [
    {
      "stock_id": 63,
      "symbol": "P10EBAAC5",
      "composite_score": 26,
      "classification": "bullish_alignment",
      "as_of": "2025-04-10",
      "latest_price": "218.0000",
      "rsi": "97.14238...",
      "volume": 1000000,
      "signal_count": 9
    }
  ],
  "disclaimer": "Technical signals describe current indicator conditions only. They are not financial advice and do not guarantee future price movement."
}
```

### 6.5 Bearish screener

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/screeners/technical" \
  -H "Content-Type: application/json" \
  -d '{"direction": "bearish", "max_score": -15}'
```

### 6.6 Combined filters

```bash
# Oversold, trading under the 50-period average, weak MACD
curl -X POST "http://127.0.0.1:8000/api/v1/screeners/technical" \
  -H "Content-Type: application/json" \
  -d '{"min_rsi": 25, "max_rsi": 45, "price_below_sma": 50, "macd_direction": "bearish", "min_volume": 500000}'

# Liquid stocks only, on a named list
curl -X POST "http://127.0.0.1:8000/api/v1/screeners/technical" \
  -H "Content-Type: application/json" \
  -d '{"symbols": ["AAPL", "MSFT", "TSLA"], "min_volume": 1000000}'
```

### 6.7 Supporting indicator endpoints

```bash
curl "http://127.0.0.1:8000/api/v1/stocks/85/analytics/bollinger-bands?period=20"
curl "http://127.0.0.1:8000/api/v1/stocks/85/analytics/volume?period=20"
```

---

## 7. Error Handling

### 7.1 Envelope

Every Phase 7 error uses the project's existing envelope, produced by the centralised
handlers in `main.py`:

```json
{ "success": false, "error": { "code": "...", "message": "...", "details": {} } }
```

Internal details — stack traces, SQL, driver messages, connection strings — are never
returned. `SignalsUnavailableError` was added to `app/core/exceptions.py` precisely so
the 409 path also uses this envelope instead of FastAPI's bare `detail`.

### 7.2 Status codes

| Code | When | `error.code` |
|------|------|--------------|
| 404 | stock id does not exist | `STOCK_NOT_FOUND` |
| 409 | an individual signal was requested but cannot be produced | `SIGNALS_UNAVAILABLE` / `NO_SIGNALS` |
| 422 | request body or parameters fail validation | `VALIDATION_ERROR` |
| 200 | `GET /signals` with insufficient data — reported in the body, not as an error | — |

### 7.3 Insufficient data is never an error

`GET /stocks/{id}/signals` answers **200** with a `status`, because "this stock has
not enough history" is a normal, expected answer rather than a failure:

| Condition | `status` | `classification` | `signals` |
|-----------|----------|------------------|-----------|
| No stored prices | `no_price_data` | `insufficient_data` | `[]` |
| Fewer than 15 usable records | `insufficient_data` | `insufficient_data` | `[]` |
| Enough records | `ok` | computed alignment | computed |
| Enough records, some indicators short | `ok` | computed alignment | partial, with `unavailable_indicators` listing the gaps |

When the status is not `ok`, the score is `0` **and** the classification is
`insufficient_data`, so "no data" can never be presented as a neutral reading.
`GET /signals/latest` cannot express "no signal", so it returns 409 instead of
inventing one.

Example:

```json
{
  "stock_id": 15,
  "symbol": "TEST2",
  "status": "insufficient_data",
  "message": "Insufficient price history: 5 usable records, 15 required to evaluate signals.",
  "composite_score": 0,
  "classification": "insufficient_data",
  "signal_count": 0,
  "signals": [],
  "indicator_snapshot": { "records": 5,
    "unavailable_indicators": ["rsi", "sma_20", "sma_50", "ema_12", "ema_26", "macd", "bollinger_bands"] }
}
```

The endpoint never crashes on missing data; the four distinct causes (stock not
found / no price data / insufficient history / individual indicator unavailable) are
each reported distinctly.

---

## 8. Testing

```bash
make test
# or
.venv/bin/pytest tests/ -q
```

**199 tests pass**, all against an in-memory SQLite database. No test contacts an
external API, and no test touches development MySQL.

| File | Tests | Coverage |
|------|-------|----------|
| `tests/test_signals.py` | 74 | rule units, strength/band mapping, score aggregation, classification, determinism, forbidden-language scan, API integration |
| `tests/test_screeners.py` | 58 | validation, every individual filter, missing-data exclusion, ordering, pagination, API integration |
| `tests/test_analytics.py` | 19 | Phase 6 indicators, plus the Bollinger Bands and volume average endpoints |
| Phases 1–4 suites | 48 | database, health, stocks, prices, events, companies, ingestion, market data |

65 of the 199 pre-date Phase 7 and were used as the regression baseline; all still pass.

### 8.1 Determinism

All fixtures are fixed `Decimal` price series — no random numbers, no wall clock. A
test asserts that repeated calls on unchanged data return byte-identical payloads,
for both the signals and screener endpoints.

### 8.2 No-predictive-language guarantee

`test_explanations_are_descriptive_not_predictive` scans every explanation produced
across bullish, bearish, neutral, RSI-extreme and band-touching scenarios and fails
if any contains forecasting or advice vocabulary (`will`, `should buy`, `expected
return`, `guaranteed`, `profit`, `target price`, …). Two further tests assert that
classification labels are never trade recommendations and that no signal field is
phrased as a probability or confidence.

This is a real guard, not decoration: temporarily rewriting the oversold RSI
explanation to say the price *"WILL RISE shortly, so you should buy now"* makes the
suite fail immediately, and the original file restores clean.

### 8.3 Known pre-existing bug fixed

`tests/test_analytics.py` had a `stock_id` fixture whose teardown created a
throwaway in-memory engine instead of using the shared `db_session`, producing 13
spurious `no such table: stock_prices` errors. That was a Phase 6 test bug, not a
Phase 7 regression; it was fixed so the suite runs clean.

---

## 9. Future Extensions

Deliberately out of scope for Phase 7 — each would be its own phase:

1. **Signal history snapshots** — persist dated evaluations for backtesting. Requires
   deciding staleness and recomputation policy.
2. **Additional indicators** — Ichimoku, ADX, Stochastics, ATR. Each needs a family
   and a cap in `FAMILY_CAPS`, and the cap table must be rebalanced so the total
   stays meaningful.
3. **Relative-strength screening** — compare a stock against its sector or index
   rather than only against its own history.
4. **Cross-sectional ranking** — return percentile ranks instead of absolute scores.
5. **Alerting on classification changes** — requires signal persistence first.
6. **Configuration of thresholds** — exposing the constants in `signal_service.py` as
   settings. This trades auditability for flexibility, so it should be a deliberate
   decision, not a default.

---

## Disclaimer

Technical signals in this phase are deterministic descriptions of historical price
behaviour under fixed rules. They are not financial advice, not recommendations, and
not predictions. A bullish or bearish classification indicates only that certain
indicators currently agree. Past price behaviour does not determine future prices,
and signals can be wrong, stale, or irrelevant to a particular situation.
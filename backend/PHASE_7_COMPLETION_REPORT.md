# Phase 7 Completion Report

## Phase 7 Status: COMPLETE

All Phase 7 criteria have been met and verified. Phase 7 introduces a reusable,
deterministic, explainable technical signal engine plus a technical screener layer,
built on the existing Phase 1–6 backend with no redesign and no new project.

---

### 1. Files Created

| # | File | Lines | Purpose |
|---|------|-------|---------|
| 1 | `app/services/signal_service.py` | 882 | `SignalEngine` (pure rule evaluation) + `SignalService` (data access) |
| 2 | `app/services/screener_service.py` | 176 | Candidate selection and filter matching |
| 3 | `app/api/v1/signals.py` | 124 | 3 signal endpoints |
| 4 | `app/api/v1/screeners.py` | 41 | 1 screener endpoint |
| 5 | `app/schemas/signals.py` | 166 | Signal enums, `SignalItem`, `IndicatorSnapshot`, `CompositeSignalResponse` |
| 6 | `app/schemas/screeners.py` | 122 | `TechnicalScreenerRequest` / `Result` / `Response` |
| 7 | `tests/test_signals.py` | 922 | 74 tests — rules, scoring, classification, language guarantees, API |
| 8 | `tests/test_screeners.py` | 570 | 58 tests — validation, every filter, API |
| 9 | `docs/phase-7.md` | 594 | Full Phase 7 documentation |

### 2. Files Modified (all additive — no Phase 6 behaviour changed)

| File | Change |
|------|--------|
| `app/services/analytics_service.py` | Added `calculate_bollinger_bands`, `calculate_volume_average`, `get_indicator_context` |
| `app/schemas/analytics.py` | Added `BollingerBandsResponse`, `VolumeAverageResponse`, `IndicatorContext` |
| `app/api/v1/analytics.py` | Added 2 endpoints for the new calculations |
| `app/api/dependencies.py` | Added `get_signal_service()`, `get_screener_service()` |
| `app/api/v1/__init__.py` | Registered signals + screeners routers |
| `app/schemas/__init__.py` | Exported new schemas |
| `app/core/exceptions.py` | Added `SignalsUnavailableError` (409) |
| `README.md` | Phase 7 section, endpoint tables, examples, checklist |
| `tests/test_analytics.py` | Fixed pre-existing fixture bug; added Bollinger/volume tests |

**No database migrations.** Signals are stateless — see §9.

### 3. API Endpoints Added

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/stocks/{stock_id}/signals` | GET | Full signal summary: composite score, classification, all signals, indicator snapshot |
| `/api/v1/stocks/{stock_id}/signals/latest` | GET | Strongest individual signal |
| `/api/v1/stocks/{stock_id}/signals/classification` | GET | Alignment label only |
| `/api/v1/screeners/technical` | POST | Filter the universe by signal/indicator criteria |
| `/api/v1/stocks/{stock_id}/analytics/bollinger-bands` | GET | Supporting: Bollinger Bands + %B + bandwidth |
| `/api/v1/stocks/{stock_id}/analytics/volume` | GET | Supporting: latest vs average volume |

All 6 are fully documented in `/docs` with schemas, descriptions, examples and status
codes. The API grew from 29 to 33 paths (39 operations); every Phase 1–6 endpoint is
intact and verified working.

### 4. Signals Implemented

All 7 required signal families:

| Signal | Type | Direction rule |
|--------|------|----------------|
| `rsi` | momentum | bearish > 70, bullish < 30 |
| `price_vs_ma_20` / `price_vs_ma_50` | trend | bullish when close > SMA, bearish when below |
| `ma_alignment_sma_20_50` | trend | follows SMA(20) vs SMA(50) |
| `ma_alignment_ema_12_26` | trend | follows EMA(12) vs EMA(26) |
| `macd` | momentum | bullish when MACD line > signal line |
| `bollinger_bands` | volatility | bearish at/below lower band, bullish at/above upper |
| `volume` | volume | always `neutral` — participation is not direction |
| `momentum` | momentum | follows the sign of the most recent return |

`ma_crossover_signal` is implemented and wired, but emits nothing while only one MA
history is stored; `test_ma_crossover_emits_nothing_without_history` documents this.

Each signal carries `direction`, `strength` (0–100), `strength_band`, the triggering
`value`, the `reference` threshold, and a plain-English `explanation`.

### 5. Scoring Methodology

**`strength` 0–100** — rule-derived intensity, never a probability, confidence or win
rate. Bands: `weak` 0–39, `moderate` 40–69, `strong` 70–100.

**Composite score −100…+100**, aggregated with family caps to prevent double-counting:

| Family | Cap | Why |
|--------|-----|-----|
| `moving_average` | 120 | Largest trend evidence |
| `macd` | 80 | |
| `rsi` | 70 | A single extreme RSI cannot cancel all trend evidence |
| `crossover` | 60 | |
| `bollinger_bands` | 50 | |
| `momentum` | 40 | |
| `volume` | 40 | Neutral, contributes 0 |

`score = Σ(clamped family net) / TOTAL_FAMILY_CAP(460) × 100`

The denominator is **fixed**, not the sum of the families that happened to have data.
This is what stops a lone signal from ever being reported as "strong".

**Classifications** (never BUY/SELL): `strong_bullish_alignment` (≥60),
`bullish_alignment` (20–59), `neutral_alignment` (−19–19), `bearish_alignment`
(−59…−20), `strong_bearish_alignment` (≤−60), plus `insufficient_data` which is
explicitly *not* an alignment reading.

### 6. Explanation Quality — Verified, Not Asserted

`test_explanations_are_descriptive_not_predictive` scans every explanation produced
across bullish, bearish, neutral, RSI-extreme and band-touching scenarios against
21 forbidden phrases (`will`, `should buy`, `expected return`, `guarantee`,
`target price`, `profit`, …). Two further tests assert labels are never trade
recommendations and no field is phrased as a probability.

**The guard was proven to work.** Injecting a single phrase — rewriting the oversold
RSI explanation to say the price *"WILL RISE shortly, so you should buy now"* — made
2 tests fail immediately. The original file was restored and the suite is green.

Real output:
> "RSI is above the overbought level of 70 (RSI 97.14); this is a high-momentum condition."
> "Latest close 218.0000 is above the 50-period moving average 172.3000."

### 7. Screener Filters

All required filters implemented and each individually tested: `min_score`/`max_score`,
`direction` (bullish/bearish/neutral), `min_rsi`/`max_rsi`, `price_above_sma`,
`price_below_sma`, `macd_direction`, `min_volume`, plus `symbols`, `skip`, `limit`.

Validation: scores −100…100, RSI 0…100, `min_score <= max_score`, `min_rsi <= max_rsi`,
`price_above_sma`/`price_below_sma` mutually exclusive, `limit` 1–500, `skip >= 0`,
`symbols` ≤ 200. Enforced by a Pydantic `model_validator`, so bad requests are
rejected before any query runs.

Missing data is treated as *absence of evidence*, never as zero: a stock without RSI
cannot satisfy `min_rsi`. Unevaluable stocks are reported separately as
`skipped_insufficient_data`.

### 8. Error Handling — Verified Live

| Case | Status | Body |
|------|--------|------|
| Stock not found | 404 | `STOCK_NOT_FOUND` |
| Latest signal with no data | 409 | `SIGNALS_UNAVAILABLE` + `details.status` |
| No stored prices | **200** | `status: no_price_data`, `signals: []` |
| < 15 usable records | **200** | `status: insufficient_data`, `signals: []` |
| Some indicators short | **200** | `status: ok` + `unavailable_indicators` |
| Any validation failure | 422 | `VALIDATION_ERROR` |

All four causes are distinguished. Nothing crashes, and **no signal is ever
fabricated** — when status is not `ok` the response carries an empty signal list, a
score of 0, and `classification: insufficient_data` so missing data can never be
mistaken for a neutral reading.

**One defect found and fixed during verification:** the 409 path originally used
FastAPI's bare `HTTPException(detail=...)`, breaking the app's
`{"success": false, "error": {...}}` convention. Added `SignalsUnavailableError` to
`app/core/exceptions.py`; the path now returns the standard envelope, with a test
asserting `"detail" not in body`.

No stack traces, SQL, driver messages or connection strings are ever returned.
Confirmed by scanning live error bodies.

### 9. Design Decisions

**`SignalEngine` is stateless and pure** — no session, no clock, no randomness. It
takes an `IndicatorContext` and returns signals, which is what makes it trivially
deterministic and testable.

**Bollinger Bands and volume average were added to `AnalyticsService`, not the signal
engine.** Phase 6 had neither. Computing them inside the engine would have duplicated
band/volume math and created two places to fix a formula. Both were added alongside
`calculate_sma` and exposed as endpoints so they are independently verifiable and
follow Phase 6's one-endpoint-per-calculation convention. No indicator math is
duplicated anywhere.

**No `technical_signals` table.** Signals are a pure function of stored prices; a
persisted cache would add a staleness class of bug for no correctness gain.
`GET /stocks/{id}/signals/history` is therefore intentionally not implemented — the
spec permits omitting it. A future phase can add dated snapshots for backtesting.

**No ML, no LLM, no trading/broker/portfolio execution.** Every rule is arithmetic on
stored numbers.

### 10. Testing

**199 tests pass. 65 of them pre-date Phase 7 and all still pass — zero regressions.**

| Suite | Tests | Status |
|-------|-------|--------|
| `test_signals.py` | 74 | ✅ new |
| `test_screeners.py` | 58 | ✅ new |
| `test_analytics.py` | 19 | ✅ 3 new (Bollinger/volume) + fixture bug fixed |
| Phases 1–4 suites | 48 | ✅ unchanged, all green |

All fixtures are fixed `Decimal` series — no randomness, no wall clock, no external
API. Determinism is asserted directly: repeated calls on unchanged data return
byte-identical payloads for both endpoints.

**A real bug was found in my own tests.** The initial API fixtures used a bare
`TestClient(app)` without conftest's `get_db` override, so tests were writing to the
**real development MySQL database** rather than in-memory SQLite — visible as 87
stocks where 1 was expected, and a 69-second suite. Fixed in both new files by using
conftest's `client` fixture (suite dropped to 4.5s). The 19 stray test rows that had
been written to MySQL were identified and deleted; the database returned from 104 to
85 stocks, and final verification confirmed 85 stocks / 3709 prices unchanged.

### 11. Verification Performed

**Full suite:** `199 passed` in 7.6s.

**Static analysis:** `pyflakes` clean on all Phase 7 files. (Findings exist in
pre-existing Phase 1–6 files and were left untouched.)

**Architecture checks, automated:**
- Routers contain no DB queries and no indicator math — grep-verified.
- No hardcoded symbols, credentials, API keys or connection strings in Phase 7 code.
- Every threshold and cap is a named, documented constant; the only bare literals are
  `<= 0` zero-division guards.

**Live server on `http://127.0.0.1:8000`**, against real MySQL data (85 stocks):

- `/docs` and `/openapi.json` → 200; Swagger UI renders.
- All 4 Phase 7 endpoints → 200.
- Every error case verified → correct 404 / 409 / 422 / 200-with-status.
- All 7 screener filters exercised against the real universe.
- Determinism: 3 consecutive calls byte-identical.
- Regression sweep of 19 Phase 1–6 endpoints → all 200.
- Server log: 0 unhandled exceptions, 0 5xx responses.

**Database integrity:** final state 85 stocks / 3709 prices — identical to
pre-verification. The full test suite uses in-memory SQLite and never touches
development data.

### Disclaimer

Technical signals produced by Phase 7 are deterministic descriptions of historical
price behaviour under fixed rules. They are **not financial advice, not
recommendations, and not predictions**. A bullish or bearish classification indicates
only that certain indicators currently agree. Past price behaviour does not determine
future prices, and signals can be wrong, stale, or irrelevant to a particular
situation. No outcome is guaranteed.
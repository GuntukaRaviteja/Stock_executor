# Phase 14: Portfolio Optimization & Multi-Strategy Capital Allocation

## Overview

Phase 14 adds a pure optimization and orchestration layer above the existing Phase 13 portfolio engine. It decides **how much capital each strategy should receive** based on historical backtest performance and pairwise correlation, then delegates to Phase 13 for per-strategy position sizing and risk checks.

This phase introduces **zero** duplication of Phase 13 logic. The optimizer returns a capital-allocation plan; Phase 13's `PortfolioAllocationEngine` consumes each strategy's capital to produce a sized, risk-checked portfolio.

## Architecture

```
API: POST /api/v1/portfolios/{portfolio_id}/optimize
  ↓
StrategyOptimizationService (DB-facing layer)
  - gather_strategy_performance()  → gathers from persisted backtests
  - gather_correlation()           → pairwise Pearson on daily returns
  - optimize()                     → delegates to pure engine
  - optimize_to_schema()           → converts result to response schema
  ↓
StrategyOptimizer (pure engine, no DB access)
  - evaluate_eligibility()         → trade count, volatility, drawdown checks
  - raw_weights_for()              → equal_weight / risk_adjusted / inverse_vol / user_weights
  - solve_constraints()            → clamp to [min, max] + deterministic redistribution
  ↓
OptimizationResult → _result_to_schema() → OptimizationResultSchema
```

## Components

### Pure Engine — `app/services/portfolio/optimization.py`

| Class/Function | Responsibility |
|---|---|
| `StrategyPerformance` | Immutable dataclass: strategy_id, returns, Sharpe, vol, drawdown, trade count |
| `StrategyAssignment` | Result per strategy: weight%, capital, risk_budget, eligibility, reasons |
| `CorrelationMatrix` | Symmetric Pearson matrix with `get()` lookup, status, observation count |
| `OptimizationRequest` | Engine inputs: total_capital, method, strategies, user_weights, bounds, correlation |
| `OptimizationResult` | Engine outputs: assignments, clamped_weights, redistribution, warnings |
| `StrategyOptimizer` | Main orchestrator: eligibility → weights → constraints → assignments |
| `evaluate_eligibility` | Checks min trades (≥2), non-zero vol or drawdown, return data present |
| `raw_weights_for` / `equal_weight_raw_weights` / `risk_adjusted_raw_weights` / `inverse_volatility_raw_weights` / `user_weights_raw_weights` | Four allocation methods |
| `solve_constraints` | Clamp to [min, max], redistribute residual to sum = 100%, detect infeasibility |
| `pearson_correlation` | Pearson coefficient with zero-variance → None |
| `compute_correlation_matrix` | Pairwise matrix over overlapping returns, `SINGLE_STRATEGY`/`SUFFICIENT`/`INSUFFICIENT_DATA` status |

### DB-Facing Service — `app/services/portfolio/optimization_service.py`

| Method | Responsibility |
|---|---|
| `gather_strategy_performance()` | Queries `BacktestService.list_backtests()`, aggregates equity curves, computes Sharpe/vol/drawdown/trades |
| `_fetch_backtests()` | Paginated fetch with optional `[start_date, end_date]` window (no-look-ahead) |
| `_aggregate_equity_curve()` | Aligns multi-backtest daily returns, builds cumulative equity curve |
| `gather_correlation()` | Gathers returns per strategy, delegates to `compute_correlation_matrix()` |
| `optimize()` | Routes: explicit strategies → engine directly; omitted → gather from DB |
| `optimize_to_schema()` | Converts `OptimizationResult` to `OptimizationResultSchema` including correlation matrix |

### API Endpoint — `app/api/v1/portfolios.py`

```
POST /api/v1/portfolios/{portfolio_id}/optimize
```

- Request: `OptimizeRequest` (total_capital, method, optional strategies, user_weights, min/max bounds)
- Response: `OptimizationResultSchema` (assignments with eligibility, correlation matrix, clamped weights, warnings)
- 404 for nonexistent portfolio; 422 for invalid request body; 200 for successful optimization

### Schemas — `app/schemas/optimization.py`

| Schema | Description |
|---|---|
| `AllocationMethod` | Enum: equal_weight, risk_adjusted, inverse_volatility, user_weights |
| `StrategyEligibility` | Enum: eligible, insufficient_data, excluded |
| `CorrelationStatus` | Enum: sufficient, insufficient_data, single_strategy |
| `StrategyPerformanceSchema` | Pydantic model for per-strategy performance input |
| `CorrelationEntrySchema` / `CorrelationMatrixSchema` | Correlation matrix response |
| `StrategyWeightSchema` | User-supplied weight entry |
| `OptimizeRequest` | Full request with `model_validator` for min/max and user_weights constraints |
| `StrategyAssignmentSchema` | Per-strategy allocation result |
| `ClampedWeightSchema` / `RedistributionEntrySchema` | Constraint solver audit trail |
| `OptimizationResultSchema` | Full response with correlation matrix, warnings, disclaimer |

## Allocation Methods

1. **Equal Weight**: `100 / N` for each eligible strategy
2. **Risk-Adjusted (Sharpe-proportional)**: `sharpe_i / Σ(max(sharpe, 0))` — non-positive Sharpe → 0%, all-zero fallback → equal weight
3. **Inverse Volatility**: `1/vol_i / Σ(1/vol_j)` — zero vol handled gracefully
4. **User-Specified Weights**: Caller provides weights; constraint solver enforces bounds and renormalizes

## Constraint Solver

- **Clamp**: Each raw weight is clamped to `[min_weight_pct, max_weight_pct]`
- **Redistribute**: Residual to reach exactly 100% is distributed round-robin, smallest-first when adding, largest-first when subtracting
- **Infeasibility Detection**: `max_pct * N < 100` or `min_pct * N > 100` → `ValueError` caught and returned as warning with empty assignments

## Eligibility Criteria

A strategy is eligible when ALL hold:
1. Not on exclusion list
2. Has risk data (`annualized_volatility_pct` is not None OR `max_drawdown_pct > 0`)
3. Has ≥ `min_observations` (default 2) trades
4. Has return data (`total_return_pct` is not None)

## Correlation

- Pairwise Pearson on overlapping daily return series
- Symmetric matrix with diagonal = 1.0
- `insufficient_pairs` list for pairs with < `min_observations` overlapping points
- `INSUFFICIENT_DATA` status when any pair is insufficient; `SINGLE_STRATEGY` when ≤1 strategy

## Determinism Guarantees

- All financial values use `Decimal` end-to-end, serialized as strings
- Strategies sorted by `strategy_id` before processing
- No timestamps, random values, or dict-ordering dependencies in output
- Identical inputs produce byte-equivalent responses

## Files Added/Modified

### New Files
- `app/services/portfolio/optimization.py` — Pure optimization engine
- `app/services/portfolio/optimization_service.py` — DB-facing orchestration service
- `app/schemas/optimization.py` — Pydantic schemas
- `tests/phase14_helpers.py` — Shared test helpers
- `tests/test_phase14_pure_engine.py` — 62 pure engine tests
- `tests/test_phase14_determinism.py` — Determinism tests
- `tests/test_phase14_api.py` — 38 API integration tests
- `tests/test_phase14_service.py` — 14 service-layer DB integration tests

### Modified Files
- `app/api/v1/portfolios.py` — Added `POST /{portfolio_id}/optimize` endpoint
- `app/api/dependencies.py` — Added `get_strategy_optimization_service` DI dependency
- `app/services/backtest/service.py` — Extended `list_backtests` with optional `strategy_name` filter
- `app/services/portfolio/__init__.py` — Exported `StrategyOptimizationService`
- `app/schemas/__init__.py` — Exported new schema classes

## Test Results

```
1233 passed, 12 warnings, 0 failures
```

- Baseline (pre-Phase 14): 1115 passed, 12 warnings, 0 failures
- Phase 14 tests: 118 new tests (62 pure engine + 2 determinism + 38 API + 14 service-layer + 2 DB-backed correlation)
- No regressions

## Paper Trading Disclaimer

All Phase 14 functionality is simulation-only. The optimizer returns a capital-allocation plan; it does not place orders or interact with brokers. Each strategy's allocated capital can be handed to Phase 13's `PortfolioAllocationEngine` for position sizing and risk checks.

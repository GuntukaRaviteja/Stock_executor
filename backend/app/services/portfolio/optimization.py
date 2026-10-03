"""Phase 14 — strategy-level capital allocation optimizer.

A **pure** module. It contains no session, no clock, no network and no SQL. Given a set of
pre-computed strategy performance records and an optional correlation matrix it returns a
deterministic capital-allocation plan: how much capital each strategy should receive and why.

This is the orchestration layer *above* the Phase 13 portfolio engine. Phase 14 does not
re-implement position sizing, portfolio risk checks, allocation or backtesting — it calls those
Phase 13 components. Phase 14's only job is to decide how much capital to hand to each strategy
and then to let Phase 13 turn that capital into a sized, risk-checked portfolio. The two layers
compose:

    Phase 14 optimizer                  (this module)
      -> strategy weights + capital      (the answer)
      -> Phase 13 PortfolioAllocationEngine
           -> PositionSizingEngine + risk limits -> final orders

Determinism
-----------
Identical inputs always produce byte-equivalent output: no wall-clock reads, no ``random``,
no ``dict`` iteration order, no set iteration order. All money is :class:`decimal.Decimal` and
all ordering is on stable sort keys.

No-look-ahead
-------------
The optimizer only consumes what a portfolio manager at time *T* could have known: historical
strategy performance and an ex-post correlation estimate. It does not peek at any strategy's
future returns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple

from app.services.backtest.models import quantize_money, quantize_percent


#: Absolute zero used for sign comparisons.
ZERO = Decimal("0")
#: One hundred, the scale of every percentage in this module.
_HUNDRED = Decimal("100")
#: Smallest weight that still carries real capital after quantisation to percent.
_MIN_WEIGHT_PCT = Decimal("0.0001")


class AllocationMethod(str, Enum):
    """How strategy capital is distributed."""

    EQUAL_WEIGHT = "equal_weight"
    RISK_ADJUSTED = "risk_adjusted"
    INVERSE_VOLATILITY = "inverse_volatility"
    USER_WEIGHTS = "user_weights"


class StrategyEligibility(str, Enum):
    """Whether a strategy is eligible to receive allocation."""

    ELIGIBLE = "eligible"
    INSUFFICIENT_DATA = "insufficient_data"
    EXCLUDED = "excluded"


class CorrelationStatus(str, Enum):
    """Whether the correlation matrix has enough observations."""

    SUFFICIENT = "sufficient"
    INSUFFICIENT_DATA = "insufficient_data"
    SINGLE_STRATEGY = "single_strategy"


@dataclass(frozen=True)
class StrategyPerformance:
    """Pre-computed performance summary for one strategy over a historical window.

    ``strategy_id`` is a stable string (e.g. ``"phase7_technical"``) that identifies the
    strategy across runs. ``symbol`` carries the ticker of the backtest the performance was
    derived from, when the aggregation is per-stock — but the optimizer treats this as a
    read-only label, so it can be ``None`` for a fully aggregated strategy curve.
    """

    strategy_id: str
    strategy_name: str
    total_return_pct: Decimal
    sharpe_ratio: Decimal
    max_drawdown_pct: Decimal
    annualized_volatility_pct: Decimal
    number_of_trades: int = 0
    win_rate_pct: Optional[Decimal] = None
    profit_factor: Optional[Decimal] = None
    #: Start and end of the historical window the metrics were computed over.
    window_start: Optional[date] = None
    window_end: Optional[date] = None


@dataclass(frozen=True)
class StrategyAssignment:
    """The final verdict for one strategy: how much capital it receives and why.

    ``capital`` is ``weight * total_capital``. ``risk_budget`` is the allocation's notional
    exposure budget (``capital * weight``), reported so a caller can see both the raw capital
    and the relative size. ``eligibility`` and ``reasons`` explain why a strategy received the
    capital or was skipped.
    """

    strategy_id: str
    strategy_name: str
    weight_pct: Decimal
    capital: Decimal
    risk_budget: Decimal
    eligibility: StrategyEligibility
    reasons: tuple = ()

    @property
    def is_active(self) -> bool:
        """True when this assignment actually receives capital."""
        return self.eligibility is StrategyEligibility.ELIGIBLE and self.weight_pct > ZERO


@dataclass(frozen=True)
class CorrelationMatrix:
    """Pairwise Pearson correlation between strategy equity-curve returns.

    ``matrix`` maps ``(strategy_a, strategy_b)`` to the correlation coefficient. The matrix is
    symmetric and the diagonal is ``1.0``. ``status`` reports whether enough overlapping
    observations existed to compute the values.
    """

    matrix: Dict[Tuple[str, str], Decimal] = field(default_factory=dict)
    status: CorrelationStatus = CorrelationStatus.SINGLE_STRATEGY
    observation_count: int = 0
    min_observations: int = 0
    insufficient_pairs: tuple = ()

    def get(self, a: str, b: str) -> Optional[Decimal]:
        """Symmetric lookup; returns ``None`` when the pair is missing."""
        if a == b:
            return Decimal("1")
        direct = self.matrix.get((a, b))
        if direct is not None:
            return direct
        return self.matrix.get((b, a))


@dataclass(frozen=True)
class OptimizationRequest:
    """Inputs for one strategy-level capital allocation run.

    ``total_capital`` is the full pool to distribute. ``method`` selects the weighting rule.
    ``user_weights`` supplies weights when ``method`` is ``USER_WEIGHTS``. ``min_weight_pct``
    and ``max_weight_pct`` bound every strategy's allocation before redistribution. Strategies
    are listed in ``strategies``; the order is informational — the engine always produces a
    deterministic result regardless of input order.
    """

    total_capital: Decimal
    method: AllocationMethod = AllocationMethod.EQUAL_WEIGHT
    strategies: Sequence[StrategyPerformance] = ()
    user_weights: Optional[Dict[str, Decimal]] = None
    min_weight_pct: Decimal = Decimal("0")
    max_weight_pct: Decimal = Decimal("100")
    correlation: Optional[CorrelationMatrix] = None
    risk_free_rate_pct: Decimal = Decimal("0")


@dataclass(frozen=True)
class OptimizationResult:
    """The full output of one strategy-level allocation run."""

    total_capital: Decimal
    method: AllocationMethod
    correlation_status: CorrelationStatus
    assignments: tuple
    #: ``(strategy_id, weight_pct)`` pairs that were clamped, with the raw weight before clamping.
    clamped_weights: tuple
    #: Any constraint adjustments made so the sum of weights is exactly 100%, with the
    #: strategy that absorbed the residual.
    redistribution: tuple
    effective_min_weight_pct: Decimal
    effective_max_weight_pct: Decimal
    correlation: Optional[CorrelationMatrix] = None
    warnings: tuple = ()


# ----------------------------------------------------------------------
# Correlation
# ----------------------------------------------------------------------


def pearson_correlation(x: Sequence[Decimal], y: Sequence[Decimal]) -> Optional[Decimal]:
    """Pearson correlation coefficient between two equal-length return series.

    Returns ``None`` when either series has fewer than two observations, or when the standard
    deviation of either is zero (a flat series is perfectly correlated with itself, but
    uncorrelated with anything else — reporting ``0`` would be misleading).
    """
    if len(x) < 2 or len(y) < 2 or len(x) != len(y):
        return None
    n = Decimal(len(x))
    mean_x = sum(x, ZERO) / n
    mean_y = sum(y, ZERO) / n
    numerator = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
    denom_x_sq = sum((xi - mean_x) ** 2 for xi in x)
    denom_y_sq = sum((yi - mean_y) ** 2 for yi in y)
    denom = (denom_x_sq * denom_y_sq).sqrt()
    if denom == ZERO:
        return None
    return (numerator / denom).quantize(Decimal("0.0001"))


def compute_correlation_matrix(
    strategy_returns: Dict[str, List[Decimal]],
    min_observations: int = 2,
) -> CorrelationMatrix:
    """Build a pairwise Pearson correlation matrix from per-strategy return series.

    ``strategy_returns`` maps strategy_id to its list of daily returns. Two strategies are
    only correlated over their *overlapping* dates — but since the caller already aligned the
    series (daily returns on the same calendar), alignment is positional. A pair with fewer
    than ``min_observations`` overlapping points is left absent from the matrix and recorded
    in ``insufficient_pairs``.

    The matrix is symmetric and the diagonal is ``1.0``. With zero or one strategy the status
    is ``SINGLE_STRATEGY`` and the matrix is empty.
    """
    ids = sorted(strategy_returns.keys())
    if len(ids) <= 1:
        return CorrelationMatrix(
            matrix={},
            status=CorrelationStatus.SINGLE_STRATEGY,
            observation_count=len(strategy_returns[ids[0]]) if ids else 0,
            min_observations=min_observations,
            insufficient_pairs=(),
        )

    matrix: Dict[Tuple[str, str], Decimal] = {}
    insufficient: List[Tuple[str, str]] = []
    all_pair_obs: List[int] = []

    for i, a in enumerate(ids):
        matrix[(a, a)] = Decimal("1")
        for b in ids[i + 1 :]:
            returns_a = strategy_returns[a]
            returns_b = strategy_returns[b]
            overlap = min(len(returns_a), len(returns_b))
            if overlap < min_observations:
                insufficient.append((a, b))
                continue
            paired_a = returns_a[-overlap:]
            paired_b = returns_b[-overlap:]
            corr = pearson_correlation(paired_a, paired_b)
            if corr is None:
                insufficient.append((a, b))
            else:
                matrix[(a, b)] = corr
                all_pair_obs.append(overlap)

    status = CorrelationStatus.INSUFFICIENT_DATA if insufficient else CorrelationStatus.SUFFICIENT
    return CorrelationMatrix(
        matrix=matrix,
        status=status,
        observation_count=min(all_pair_obs) if all_pair_obs else 0,
        min_observations=min_observations,
        insufficient_pairs=tuple(insufficient),
    )


# ----------------------------------------------------------------------
# Weighting methods (pure)
# ----------------------------------------------------------------------


def _safe_volatility(perf: StrategyPerformance) -> Decimal:
    """Volatility, falling back to drawdown magnitude when volatility is unavailable."""
    vol = perf.annualized_volatility_pct
    if vol is not None and vol > ZERO:
        return vol
    if perf.max_drawdown_pct > ZERO:
        return perf.max_drawdown_pct
    return _MIN_WEIGHT_PCT


def _risk_adjusted_raw_weight(perf: StrategyPerformance) -> Decimal:
    """``|sharpe|`` as a share of total |sharpe| — risk-adjusted (return/risk) weighting.

    A negative Sharpe is treated as zero weight: a strategy with negative risk-adjusted return
    should not receive capital under this method. Strategies with no Sharpe data fall back to
    equal weight among the eligible strategies.
    """
    sharpe = perf.sharpe_ratio
    if sharpe is not None and sharpe > ZERO:
        return sharpe
    return ZERO


def _inverse_vol_raw_weight(perf: StrategyPerformance) -> Decimal:
    """Raw inverse-volatility weight: ``1 / volatility``."""
    vol = _safe_volatility(perf)
    if vol <= ZERO:
        return ZERO
    return Decimal("1") / vol


def equal_weight_raw_weights(performances: Sequence[StrategyPerformance]) -> Dict[str, Decimal]:
    """Each strategy gets ``1/N`` — the simplest allocation."""
    n = len(performances)
    if n == 0:
        return {}
    weight = _HUNDRED / Decimal(n)
    return {perf.strategy_id: weight for perf in performances}


def risk_adjusted_raw_weights(performances: Sequence[StrategyPerformance]) -> Dict[str, Decimal]:
    """Sharpe-proportional weights, normalised to 100%.

    Strategies with non-positive Sharpe receive zero. If every eligible strategy has zero or
    undefined Sharpe, this degrades to equal weight so the caller always gets a usable plan.
    """
    scored = {perf.strategy_id: _risk_adjusted_raw_weight(perf) for perf in performances}
    total = sum(scored.values(), ZERO)
    if total <= ZERO:
        return equal_weight_raw_weights(performances)
    return {sid: (raw / total * _HUNDRED).quantize(Decimal("0.0001")) for sid, raw in scored.items()}


def inverse_volatility_raw_weights(performances: Sequence[StrategyPerformance]) -> Dict[str, Decimal]:
    """Inverse-volatility weights, normalised to 100%.

    Lower volatility → higher weight. Falls back to equal weight when total inverse-vol is zero.
    """
    weights = {perf.strategy_id: _inverse_vol_raw_weight(perf) for perf in performances}
    total = sum(weights.values(), ZERO)
    if total <= ZERO:
        return equal_weight_raw_weights(performances)
    return {sid: (raw / total * _HUNDRED).quantize(Decimal("0.0001")) for sid, raw in weights.items()}


def user_weights_raw_weights(
    performances: Sequence[StrategyPerformance],
    user_weights: Optional[Dict[str, Decimal]],
) -> Dict[str, Decimal]:
    """Use the caller-supplied weights directly.

    Strategies not named in ``user_weights`` receive zero. Unknown strategies
    that appear in ``user_weights`` but are not in the performance list are
    silently dropped.
    """
    by_id = {perf.strategy_id: perf for perf in performances}
    if user_weights is None:
        return equal_weight_raw_weights(performances)
    result: Dict[str, Decimal] = {}
    for sid in by_id:
        result[sid] = user_weights.get(sid, ZERO)
    return result


def raw_weights_for(
    method: AllocationMethod,
    performances: Sequence[StrategyPerformance],
    user_weights: Optional[Dict[str, Decimal]],
) -> Dict[str, Decimal]:
    """Dispatch to the correct weighting function for ``method``."""
    if method is AllocationMethod.USER_WEIGHTS:
        return user_weights_raw_weights(performances, user_weights)
    if method is AllocationMethod.RISK_ADJUSTED:
        return risk_adjusted_raw_weights(performances)
    if method is AllocationMethod.INVERSE_VOLATILITY:
        return inverse_volatility_raw_weights(performances)
    return equal_weight_raw_weights(performances)


# ----------------------------------------------------------------------
# Constraint solver
# ----------------------------------------------------------------------


def _clamp_weights(
    weights: Dict[str, Decimal],
    min_pct: Decimal,
    max_pct: Decimal,
) -> Tuple[Dict[str, Decimal], List[Tuple[str, Decimal, Decimal]]]:
    """Clamp each weight into ``[min_pct, max_pct]``.

    Returns the clamped dict and a list of ``(id, raw, clamped)`` for every weight that was
    actually moved, so the result records what changed and by how much.
    """
    clamped: Dict[str, Decimal] = {}
    changes: List[Tuple[str, Decimal, Decimal]] = []
    for sid in sorted(weights.keys()):
        raw = weights[sid]
        if raw < min_pct:
            clamped[sid] = min_pct
            if raw != min_pct:
                changes.append((sid, raw, min_pct))
        elif raw > max_pct:
            clamped[sid] = max_pct
            if raw != max_pct:
                changes.append((sid, raw, max_pct))
        else:
            clamped[sid] = raw
    return clamped, changes


def _redistribute_to_100(
    weights: Dict[str, Decimal],
    min_pct: Decimal,
    max_pct: Decimal,
) -> Tuple[Dict[str, Decimal], List[Tuple[str, Decimal, Decimal]]]:
    """Deterministically adjust clamped weights so they sum to exactly 100%.

    Respects ``min_pct`` and ``max_pct`` throughout: the residual is distributed round-robin
    across strategies that still have room, smallest first when adding, largest first when
    subtracting. Each strategy is adjusted at most once per pass, and passes repeat until
    the sum is within one quantisation unit of 100 (or no further progress is possible).

    The returned redistribution list records ``(id, from, to)`` for each adjustment.
    """
    if not weights:
        return {}, []

    target = Decimal("100")
    work = dict(weights)
    adjustments: List[Tuple[str, Decimal, Decimal]] = []
    tick = Decimal("0.0001")

    for _ in range(len(weights) * 100 + 10):
        current_sum = sum(work.values(), ZERO)
        delta = target - current_sum
        if abs(delta) < tick:
            break

        if delta > ZERO:
            # Need to ADD: find the strategy with the smallest current weight
            # that still has room below max_pct. Tie-break by id ascending.
            candidates = sorted(
                (sid for sid in work if work[sid] < max_pct - tick),
                key=lambda sid: (work[sid], sid),
            )
            if not candidates:
                break
            sid = candidates[0]
            room = max_pct - work[sid]
            give = min(delta, room)
            before = work[sid]
            work[sid] = quantize_percent(before + give)
            if work[sid] != before:
                adjustments.append((sid, before, work[sid]))
        else:
            # Need to SUBTRACT: find the strategy with the largest current weight
            # that still has room above min_pct. Tie-break by id ascending.
            candidates = sorted(
                (sid for sid in work if work[sid] > min_pct + tick),
                key=lambda sid: (-work[sid], sid),
            )
            if not candidates:
                break
            sid = candidates[0]
            room = work[sid] - min_pct
            give = min(-delta, room)
            before = work[sid]
            work[sid] = quantize_percent(before - give)
            if work[sid] != before:
                adjustments.append((sid, before, work[sid]))

    # Final safety net: if still off by a tiny amount, absorb into the last strategy alphabetically.
    current_sum = sum(work.values(), ZERO)
    delta = target - current_sum
    if abs(delta) >= tick and len(work) > 0:
        sid = sorted(work.keys())[-1]
        before = work[sid]
        work[sid] = quantize_percent(before + delta)
        if work[sid] != before:
            adjustments.append((sid, before, work[sid]))

    return work, adjustments


def solve_constraints(
    raw_weights: Dict[str, Decimal],
    min_pct: Decimal,
    max_pct: Decimal,
    n_strategies: Optional[int] = None,
) -> Tuple[Dict[str, Decimal], List[Tuple[str, Decimal, Decimal]], List[Tuple[str, Decimal, Decimal]]]:
    """Apply the deterministic constraint solver to raw weights.

    Steps:
    1. Sort by strategy_id (deterministic input order).
    2. Clamp each weight to ``[min_pct, max_pct]``.
    3. Redistribute the residual so the sum is exactly 100%, respecting min/max bounds.

    Raises ``ValueError`` when the constraints are infeasible — i.e. ``max_pct * N < 100``
    or ``min_pct * N > 100`` for ``N`` strategies — because no valid allocation exists.

    Returns ``(final_weights, clamped_changes, redistribution_changes)``.
    """
    n = n_strategies if n_strategies is not None else len(raw_weights)
    if n == 0:
        return {}, [], []

    if max_pct * n < _HUNDRED:
        raise ValueError(
            f"Cannot reach 100% with {n} strategies at a {max_pct}% cap (max total {max_pct * n}%)"
        )
    if min_pct * n > _HUNDRED:
        raise ValueError(
            f"Cannot drop to 100% with {n} strategies at a {min_pct}% floor (min total {min_pct * n}%)"
        )

    sorted_ids = sorted(raw_weights.keys())
    ordered = {sid: raw_weights[sid] for sid in sorted_ids}

    clamped, clamped_changes = _clamp_weights(ordered, min_pct, max_pct)
    final, redist_changes = _redistribute_to_100(clamped, min_pct, max_pct)

    return final, clamped_changes, redist_changes


# ----------------------------------------------------------------------
# Eligibility
# ----------------------------------------------------------------------


def evaluate_eligibility(
    perf: StrategyPerformance,
    min_observations: int = 2,
    excluded: Optional[Sequence[str]] = None,
) -> Tuple[StrategyEligibility, List[str]]:
    """Decide whether a strategy is eligible for capital and why."""
    if excluded and perf.strategy_id in excluded:
        return StrategyEligibility.EXCLUDED, [f"Strategy '{perf.strategy_id}' is on the exclusion list"]

    if perf.annualized_volatility_pct is None and perf.max_drawdown_pct <= ZERO:
        return (
            StrategyEligibility.INSUFFICIENT_DATA,
            [f"Strategy '{perf.strategy_id}' has no volatility or drawdown data; risk cannot be measured"],
        )

    if perf.number_of_trades < min_observations:
        return (
            StrategyEligibility.INSUFFICIENT_DATA,
            [f"Strategy '{perf.strategy_id}' has only {perf.number_of_trades} trade(s); need at least {min_observations}"],
        )

    if perf.total_return_pct is None:
        return (
            StrategyEligibility.INSUFFICIENT_DATA,
            [f"Strategy '{perf.strategy_id}' has no return data"],
        )

    return StrategyEligibility.ELIGIBLE, []


# ----------------------------------------------------------------------
# Main optimization
# ----------------------------------------------------------------------


class StrategyOptimizer:
    """Deterministic strategy-level capital allocator.

    Given pre-computed :class:`StrategyPerformance` records, produces a deterministic
    :class:`OptimizationResult` that splits ``total_capital`` across the eligible strategies.

    The optimizer does **not** call Phase 13 itself — it returns the plan. The caller
    (typically :class:`~app.services.portfolio.optimization_service.StrategyOptimizationService`)
    is responsible for handing each strategy its capital and invoking the Phase 13
    :class:`~app.services.portfolio.allocation_engine.PortfolioAllocationEngine`.
    """

    def __init__(self, min_observations: int = 2):
        self.min_observations = min_observations

    def optimize(self, request: OptimizationRequest) -> OptimizationResult:
        """Run one optimization and return the capital-allocation plan."""
        performances = list(request.strategies)
        total_capital = Decimal(request.total_capital)
        min_pct = Decimal(request.min_weight_pct)
        max_pct = Decimal(request.max_weight_pct)

        # Evaluate eligibility for every strategy
        excluded = set(request.user_weights.keys()) if request.user_weights else set()
        # Actually, excluded strategies are those explicitly marked — but in the pure engine
        # we use the eligibility function. The service layer can mark strategies as excluded.
        excluded = None

        eligible: List[StrategyPerformance] = []
        ineligible: List[Tuple[StrategyPerformance, StrategyEligibility, tuple]] = []
        for perf in sorted(performances, key=lambda p: p.strategy_id):
            if not perf.strategy_id:
                continue
            elig, reasons = evaluate_eligibility(perf, self.min_observations, excluded)
            if elig is StrategyEligibility.ELIGIBLE:
                eligible.append(perf)
            else:
                ineligible.append((perf, elig, tuple(reasons)))

        if not eligible:
            assignments = tuple(
                StrategyAssignment(
                    strategy_id=perf.strategy_id,
                    strategy_name=perf.strategy_name,
                    weight_pct=ZERO,
                    capital=ZERO,
                    risk_budget=ZERO,
                    eligibility=elig,
                    reasons=reasons,
                )
                for perf, elig, reasons in ineligible
            )
            corr_status = (
                request.correlation.status
                if request.correlation is not None
                else CorrelationStatus.SINGLE_STRATEGY
            )
            warnings: List[str] = ["No strategies met the eligibility criteria"]
            return OptimizationResult(
                total_capital=quantize_money(total_capital),
                method=request.method,
                correlation_status=corr_status,
                correlation=request.correlation,
                assignments=assignments,
                clamped_weights=(),
                redistribution=(),
                effective_min_weight_pct=min_pct,
                effective_max_weight_pct=max_pct,
                warnings=tuple(warnings),
            )

        # Compute raw weights
        raw = raw_weights_for(request.method, eligible, request.user_weights)

        # Solve constraints — may raise ValueError if min/max bounds are infeasible
        try:
            final_weights, clamped_changes, redist_changes = solve_constraints(
                raw, min_pct, max_pct, n_strategies=len(eligible)
            )
        except ValueError as exc:
            corr_status = (
                request.correlation.status
                if request.correlation is not None
                else CorrelationStatus.SINGLE_STRATEGY
            )
            return OptimizationResult(
                total_capital=quantize_money(total_capital),
                method=request.method,
                correlation_status=corr_status,
                correlation=request.correlation,
                assignments=tuple(),
                clamped_weights=(),
                redistribution=(),
                effective_min_weight_pct=min_pct,
                effective_max_weight_pct=max_pct,
                warnings=(str(exc), "Constraints are infeasible for the number of eligible strategies"),
            )

        # Build assignments for eligible strategies
        sorted_eligible_ids = sorted(final_weights.keys())
        assignments: List[StrategyAssignment] = []
        for sid in sorted_eligible_ids:
            perf = next(p for p in eligible if p.strategy_id == sid)
            weight = final_weights[sid]
            capital = quantize_money(weight / _HUNDRED * total_capital)
            risk_budget = quantize_money(weight / _HUNDRED * total_capital)
            reasons: List[str] = []
            if perf.max_drawdown_pct > ZERO:
                reasons.append(
                    f"Max drawdown {perf.max_drawdown_pct}% over "
                    f"{perf.window_start}..{perf.window_end}"
                )
            if perf.sharpe_ratio is not None:
                reasons.append(f"Sharpe ratio {perf.sharpe_ratio}")
            if perf.annualized_volatility_pct is not None:
                reasons.append(f"Annualized vol {perf.annualized_volatility_pct}%")
            if perf.win_rate_pct is not None:
                reasons.append(f"Win rate {perf.win_rate_pct}%")
            assignments.append(
                StrategyAssignment(
                    strategy_id=perf.strategy_id,
                    strategy_name=perf.strategy_name,
                    weight_pct=quantize_percent(weight),
                    capital=capital,
                    risk_budget=risk_budget,
                    eligibility=StrategyEligibility.ELIGIBLE,
                    reasons=tuple(reasons),
                )
            )

        # Build assignments for ineligible strategies
        for perf, elig, reasons in ineligible:
            assignments.append(
                StrategyAssignment(
                    strategy_id=perf.strategy_id,
                    strategy_name=perf.strategy_name,
                    weight_pct=ZERO,
                    capital=ZERO,
                    risk_budget=ZERO,
                    eligibility=elig,
                    reasons=reasons,
                )
            )

        # Sort all assignments by strategy_id for deterministic output
        assignments = tuple(sorted(assignments, key=lambda a: a.strategy_id))

        # Build clamped changes with readable info
        clamped_list = tuple(
            (sid, raw_weight, clamped_weight)
            for sid, raw_weight, clamped_weight in clamped_changes
        )
        redist_list = tuple(
            (sid, old_weight, new_weight)
            for sid, old_weight, new_weight in redist_changes
        )

        corr_status = (
            request.correlation.status
            if request.correlation is not None
            else CorrelationStatus.SINGLE_STRATEGY
        )

        return OptimizationResult(
            total_capital=quantize_money(total_capital),
            method=request.method,
            correlation_status=corr_status,
            correlation=request.correlation,
            assignments=assignments,
            clamped_weights=clamped_list,
            redistribution=redist_list,
            effective_min_weight_pct=min_pct,
            effective_max_weight_pct=max_pct,
        )

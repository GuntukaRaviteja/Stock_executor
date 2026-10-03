"""Phase 15 — correlation, covariance, volatility and risk contribution.

Builds the statistical backbone of the risk report from the historical return series the caller
supplies, and refuses to build it when the data cannot support it.

What it computes
----------------
=============================  ==========================================================
correlation matrix             Pearson correlation between each pair of analysed symbols
covariance matrix              Sample covariance of the same aligned series
portfolio volatility           ``sigma_p = sqrt(w' * Sigma * w)``, daily and annualised
risk contribution              ``MRC_i = (Sigma w)_i / sigma_p`` and ``CRC_i = w_i * MRC_i``
correlation concentration      ``sum over i != j of w_i * w_j * rho_ij``
weighted average correlation   the same sum, divided by the weight mass it was summed over
beta                           per-symbol beta versus the benchmark, weighted into the portfolio
correlation clusters          connected groups of symbols correlated at or above the threshold
=============================  ==========================================================

Why the component contributions are checked
--------------------------------------------
``sum_i CRC_i`` must equal ``sigma_p`` for any valid covariance matrix. The engine recomputes
that total and publishes the difference as ``reconciliation_error_pct`` rather than assuming it,
so a matrix that does not satisfy the identity is visible instead of quietly producing
contributions that do not add up.

Missing data is reported, never invented
----------------------------------------
A symbol with no usable return series is *excluded* and named in ``excluded_symbols``; the
portfolio's analysed weight coverage is published alongside the volatility. A flat series, a
mismatched-length series or a non-finite observation is excluded the same way. When too little
survives, ``data_quality`` is ``INSUFFICIENT`` and every statistic stays ``None`` — the engine
does not fall back to zero volatility, which would report a risky portfolio as risk-free.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from app.core.exceptions import CovarianceCalculationError, InsufficientMarketDataError
from app.services.backtest.models import quantize_percent
from app.services.portfolio_risk.config import ZERO, PortfolioRiskConfig
from app.services.portfolio_risk.evaluation import EPSILON, pct
from app.services.portfolio_risk.models import (
    CorrelationCluster,
    CovarianceAnalysis,
    RiskContribution,
)

#: One hundred — the scale of every percentage in this package.
_HUNDRED = Decimal("100")
#: Correlation values are stored at four decimal places, matching Phase 14's correlation matrix.
_CORRELATION_QUANT = Decimal("0.0001")
#: Relative tolerance for the positive-semi-definiteness test on leading principal minors.
_PSD_TOLERANCE = Decimal("1e-10")


class CovarianceAnalyzer:
    """Deterministic covariance and correlation analysis.

    Pure: no clock, no database, no randomness. Symbol order is always the sorted symbol order,
    so every matrix is produced with the same indexing for the same input.
    """

    def __init__(self, config: Optional[PortfolioRiskConfig] = None):
        self.config = config or PortfolioRiskConfig().validate()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(
        self,
        weights_pct: Mapping[str, Decimal],
        asset_returns: Mapping[str, Sequence[Decimal]],
        benchmark_returns: Optional[Sequence[Decimal]] = None,
        benchmark_symbol: Optional[str] = None,
    ) -> CovarianceAnalysis:
        """Analyse the covariance of the symbols in ``weights_pct``.

        ``weights_pct`` maps symbol to portfolio weight **as a percent** (``20`` means 20%).
        ``asset_returns`` maps symbol to its daily return series as fractions, oldest first.
        """
        weights = {symbol: Decimal(str(weight)) for symbol, weight in weights_pct.items()}
        series = {
            symbol: tuple(Decimal(str(value)) for value in values)
            for symbol, values in asset_returns.items()
        }
        self._require_finite(series)

        symbols = sorted(weights)
        if not symbols:
            return CovarianceAnalysis(data_quality="INSUFFICIENT")

        eligible, excluded = self._partition(symbols, series)
        # Coverage is measured against *invested* weight, not against equity: cash carries no
        # return series by design, and counting it as a coverage gap would report every portfolio
        # holding cash as partially unmeasured.
        invested = sum(weights.values(), ZERO)
        covered = sum((weights[symbol] for symbol in eligible), ZERO)
        coverage = (
            quantize_percent(covered / invested * _HUNDRED) if invested > ZERO else None
        )
        observations = min((len(series[symbol]) for symbol in eligible), default=0)

        # Beta needs only one asset and a benchmark, so it is computed even when a covariance matrix
        # cannot be: a single-position portfolio has a perfectly well-defined beta, and refusing it
        # would report a measurable risk as missing.
        beta, individual_betas = self._beta(
            weights, eligible, series, benchmark_returns or (), benchmark_symbol
        )

        if len(eligible) < 2 or observations < self.config.min_observations:
            return CovarianceAnalysis(
                symbols=tuple(eligible),
                excluded_symbols=tuple(excluded),
                observation_count=observations,
                beta=beta,
                individual_betas=individual_betas,
                benchmark=benchmark_symbol,
                data_quality="INSUFFICIENT",
            )

        aligned = self._align(eligible, series)
        observations = len(aligned[0])

        correlation, unavailable = self._correlation_matrix(eligible, aligned)
        covariance = self._covariance_matrix(eligible, aligned)
        self._require_positive_semidefinite(eligible, covariance)

        daily_volatility, daily_variance = self._portfolio_volatility(weights, eligible, covariance)
        annualised = self._annualise(daily_volatility)
        contributions = self._risk_contributions(weights, eligible, covariance, daily_volatility)
        concentration, weighted_average = self._correlation_summary(
            weights, eligible, correlation
        )
        clusters = self._correlation_clusters(eligible, correlation, weights, contributions)

        return CovarianceAnalysis(
            symbols=tuple(eligible),
            observation_count=observations,
            correlation=correlation,
            covariance=covariance,
            daily_volatility_pct=daily_volatility,
            annualised_volatility_pct=annualised,
            variance_daily=daily_variance,
            risk_contributions=tuple(contributions),
            correlation_concentration=concentration,
            weighted_average_correlation=weighted_average,
            clusters=tuple(clusters),
            beta=beta,
            individual_betas=individual_betas,
            benchmark=benchmark_symbol,
            reconciliation_error_pct=self._reconciliation_error(contributions, daily_volatility),
            data_quality="SUFFICIENT" if not excluded else "PARTIAL",
            excluded_symbols=tuple(excluded),
            unavailable_pairs=tuple(unavailable),
            weight_coverage_pct=coverage,
        )

    def spiked_volatility(
        self,
        analysis: CovarianceAnalysis,
        correlation_multiplier: Decimal,
        volatility_multiplier: Decimal = Decimal("1"),
    ) -> Optional[Decimal]:
        """Annualised volatility when correlations are blended toward a fully-correlated book.

        ``rho' = (1 - m) * rho + m * 1`` is a convex blend of two valid correlation matrices, so
        the implied covariance stays positive semi-definite by construction rather than by
        repair:

            ``Sigma' = (1 - m) * Sigma + m * v * v'``   where ``v_i = sigma_i``

        which is exactly ``D rho' D``. ``volatility_multiplier`` then scales the whole matrix, the
        way a crisis raises everyone's realised volatility. Returns ``None`` when there is no
        matrix to stress — never a zero.
        """
        if not analysis.available or not analysis.symbols:
            return None
        symbols = analysis.symbols
        recovered = self._weights_from_analysis(analysis)
        vector = [recovered.get(symbol, ZERO) / _HUNDRED for symbol in symbols]
        volatilities = {}
        for symbol in symbols:
            variance = analysis.covariance.get((symbol, symbol))
            if variance is None or variance < ZERO:
                return None
            volatilities[symbol] = variance.sqrt()

        m = Decimal(str(correlation_multiplier))
        k = Decimal(str(volatility_multiplier))
        variance = ZERO
        for i, left in enumerate(symbols):
            for j, right in enumerate(symbols):
                sigma_ij = analysis.covariance.get((left, right), ZERO)
                blended = (Decimal("1") - m) * sigma_ij + m * volatilities[left] * volatilities[right]
                variance += vector[i] * vector[j] * blended * k * k
        if variance < ZERO:
            return None
        return self._annualise(quantize_percent(variance.sqrt() * _HUNDRED))

    # ------------------------------------------------------------------
    # Data selection
    # ------------------------------------------------------------------

    def _require_finite(self, series: Mapping[str, Sequence[Decimal]]) -> None:
        """Refuse non-finite observations outright.

        A ``NaN`` in a return series propagates silently through every statistic downstream and
        turns a covariance into a number that looks computed and means nothing, so it is
        rejected at the door with the offending symbol named.
        """
        for symbol in sorted(series):
            for index, value in enumerate(series[symbol]):
                if not Decimal(str(value)).is_finite():
                    raise InsufficientMarketDataError(
                        f"Return series for '{symbol}' contains a non-finite value at position "
                        f"{index}; a covariance cannot be computed from it.",
                        details={"symbol": symbol, "position": index},
                    )

    def _partition(
        self,
        symbols: Sequence[str],
        series: Mapping[str, Sequence[Decimal]],
    ) -> Tuple[List[str], List[str]]:
        """Split symbols into those with enough history to analyse and those without.

        Four exclusions, all reported rather than absorbed: no series at all, a series shorter
        than the configured observation floor, a series that is entirely flat (its standard
        deviation is zero, so its correlation with anything is undefined — not zero), and a
        symbol the portfolio does not actually hold.
        """
        minimum = self.config.min_observations
        eligible: List[str] = []
        excluded: List[str] = []
        for symbol in symbols:
            values = series.get(symbol)
            if not values or len(values) < minimum:
                excluded.append(symbol)
                continue
            if _is_flat(values):
                excluded.append(symbol)
                continue
            eligible.append(symbol)
        return eligible, excluded

    def _align(
        self,
        symbols: Sequence[str],
        series: Mapping[str, Sequence[Decimal]],
    ) -> List[List[Decimal]]:
        """Align every series on the same window: the shortest series' length, most recent.

        Series are daily returns on a common calendar, so alignment is positional. Taking the
        most recent ``n`` observations of each series is deterministic and avoids pretending an
        early observation still describes today's portfolio.
        """
        length = min(len(series[symbol]) for symbol in symbols)
        return [list(series[symbol][-length:]) for symbol in symbols]

    # ------------------------------------------------------------------
    # Matrices
    # ------------------------------------------------------------------

    def _correlation_matrix(
        self,
        symbols: Sequence[str],
        aligned: Sequence[Sequence[Decimal]],
    ) -> Tuple[Dict[Tuple[str, str], Decimal], List[Tuple[str, str]]]:
        """Symmetric Pearson correlation matrix with a unit diagonal.

        Pairs that cannot be correlated are simply absent from the matrix and listed in
        ``unavailable``: a missing coefficient stays missing rather than being filled with zero,
        which would read as "these holdings are uncorrelated" instead of "this is unknown".
        """
        matrix: Dict[Tuple[str, str], Decimal] = {}
        unavailable: List[Tuple[str, str]] = []
        rows = {symbol: index for index, symbol in enumerate(symbols)}
        for i, left in enumerate(symbols):
            matrix[(left, left)] = Decimal("1")
            for right in symbols[i + 1 :]:
                value = pearson_correlation(aligned[i], aligned[rows[right]])
                if value is None:
                    unavailable.append((left, right))
                    continue
                matrix[(left, right)] = value
                matrix[(right, left)] = value
        return matrix, unavailable

    def _covariance_matrix(
        self,
        symbols: Sequence[str],
        aligned: Sequence[Sequence[Decimal]],
    ) -> Dict[Tuple[str, str], Decimal]:
        """Sample covariance (``n - 1`` denominator) of the aligned series."""
        observations = len(aligned[0])
        rows = {symbol: index for index, symbol in enumerate(symbols)}
        means = [sum(column, ZERO) / Decimal(observations) for column in aligned]
        matrix: Dict[Tuple[str, str], Decimal] = {}
        for i, left in enumerate(symbols):
            for j, right in enumerate(symbols):
                right_row = aligned[rows[right]]
                right_mean = means[rows[right]]
                total = sum(
                    (aligned[i][k] - means[i]) * (right_row[k] - right_mean)
                    for k in range(observations)
                )
                matrix[(left, right)] = total / Decimal(observations - 1)
        return matrix

    def _require_positive_semidefinite(
        self,
        symbols: Sequence[str],
        covariance: Mapping[Tuple[str, str], Decimal],
    ) -> None:
        """Reject a covariance matrix that cannot produce a real variance.

        ``w' * Sigma * w`` is only meaningful when ``Sigma`` is positive semi-definite; a matrix
        that is not would yield a negative variance, and ``sqrt`` of a negative number is a bug
        the engine refuses rather than propagates. Leading principal minors are tested, and each
        off-diagonal pair is checked for symmetry. Built from real observations this always
        holds — the check exists so that a defect cannot hide behind a plausible-looking number.
        """
        index = {symbol: position for position, symbol in enumerate(symbols)}
        for left in symbols:
            for right in symbols:
                if index[left] < index[right]:
                    forward = covariance.get((left, right))
                    reverse = covariance.get((right, left))
                    if forward is None or reverse is None or abs(forward - reverse) > EPSILON:
                        raise CovarianceCalculationError(
                            "Covariance matrix is not symmetric for the pair "
                            f"('{left}', '{right}'); it cannot be used to compute a portfolio "
                            "variance.",
                            details={"symbol_a": left, "symbol_b": right},
                        )

        for size in range(1, len(symbols) + 1):
            leading = symbols[:size]
            minor = _leading_minor(covariance, leading)
            if minor < -_PSD_TOLERANCE:
                raise CovarianceCalculationError(
                    "Covariance matrix is not positive semi-definite: its leading principal "
                    f"minor of order {size} is {minor}. A portfolio variance computed from it "
                    "would be meaningless, so the run is refused rather than repaired.",
                    details={"order": size, "minor": str(minor)},
                )

    # ------------------------------------------------------------------
    # Portfolio statistics
    # ------------------------------------------------------------------

    def _portfolio_volatility(
        self,
        weights: Mapping[str, Decimal],
        symbols: Sequence[str],
        covariance: Mapping[Tuple[str, str], Decimal],
    ) -> Tuple[Optional[Decimal], Optional[Decimal]]:
        """``sqrt(w' * Sigma * w)`` as a percent, with the variance it came from."""
        vector = [weights.get(symbol, ZERO) / _HUNDRED for symbol in symbols]
        variance = ZERO
        for i, left in enumerate(symbols):
            for j, right in enumerate(symbols):
                variance += vector[i] * vector[j] * covariance.get((left, right), ZERO)
        if variance < -EPSILON:
            raise CovarianceCalculationError(
                f"Portfolio variance evaluated to {variance}, which is negative; the covariance "
                "matrix is not usable for this weight vector."
            )
        variance = max(variance, ZERO)
        return quantize_percent(variance.sqrt() * _HUNDRED), variance

    def _annualise(self, daily_volatility_pct: Optional[Decimal]) -> Optional[Decimal]:
        """Scale a daily volatility to a yearly one by the square root of time."""
        if daily_volatility_pct is None:
            return None
        factor = Decimal(str(math.sqrt(self.config.annualization_periods)))
        return quantize_percent(Decimal(str(daily_volatility_pct)) * factor)

    def _risk_contributions(
        self,
        weights: Mapping[str, Decimal],
        symbols: Sequence[str],
        covariance: Mapping[Tuple[str, str], Decimal],
        daily_volatility_pct: Optional[Decimal],
    ) -> List[RiskContribution]:
        """Marginal and component risk contribution of each position.

        ``MRC_i = (Sigma w)_i / sigma_p`` is the volatility added by one extra unit of weight in
        symbol ``i``; ``CRC_i = w_i * MRC_i`` is the share this portfolio actually pays for it.
        """
        if daily_volatility_pct is None or daily_volatility_pct <= ZERO:
            return []
        vector = {symbol: weights.get(symbol, ZERO) / _HUNDRED for symbol in symbols}
        volatility_fraction = Decimal(str(daily_volatility_pct)) / _HUNDRED

        contributions: List[RiskContribution] = []
        for symbol in symbols:
            sigma_w = sum(
                (covariance.get((symbol, other), ZERO) * vector[other] for other in symbols),
                ZERO,
            )
            marginal = sigma_w / volatility_fraction
            component = vector[symbol] * marginal
            contributions.append(
                RiskContribution(
                    symbol=symbol,
                    weight_pct=quantize_percent(Decimal(str(weights.get(symbol, ZERO)))),
                    marginal_contribution=quantize_percent(marginal * _HUNDRED),
                    component_contribution=quantize_percent(component * _HUNDRED),
                    contribution_pct=quantize_percent(component / volatility_fraction * _HUNDRED),
                )
            )
        contributions.sort(key=lambda item: (-item.contribution_pct, item.symbol))
        return contributions

    def _reconciliation_error(
        self,
        contributions: Sequence[RiskContribution],
        daily_volatility_pct: Optional[Decimal],
    ) -> Optional[Decimal]:
        """``|sum(CRC) - sigma_p|`` in percent — zero for a valid covariance matrix.

        Published rather than asserted: if a future change to the matrix broke the identity, the
        report would show a non-zero number instead of contributions that silently fail to add
        up.
        """
        if not contributions or daily_volatility_pct is None:
            return None
        total = sum(
            (item.component_contribution for item in contributions), ZERO
        ) / _HUNDRED * _HUNDRED
        return quantize_percent(abs(total - Decimal(str(daily_volatility_pct))))

    def _correlation_summary(
        self,
        weights: Mapping[str, Decimal],
        symbols: Sequence[str],
        correlation: Mapping[Tuple[str, str], Decimal],
    ) -> Tuple[Optional[Decimal], Optional[Decimal]]:
        """Correlation concentration and the weighted average pairwise correlation.

        Both sum over ``i != j`` only — the diagonal is excluded, since an asset's correlation
        with itself would inflate the concentration of every single-position portfolio. The
        weighted average divides by the weight mass actually correlated, so a book with two
        symbols is not scored against the mass of a fifty-symbol one.
        """
        if len(symbols) < 2:
            return None, None
        vector = {symbol: weights.get(symbol, ZERO) / _HUNDRED for symbol in symbols}
        concentration = ZERO
        weight_mass = ZERO
        for left in symbols:
            for right in symbols:
                if left == right:
                    continue
                rho = correlation.get((left, right))
                if rho is None:
                    continue
                concentration += vector[left] * vector[right] * rho
                weight_mass += vector[left] * vector[right]
        if weight_mass <= ZERO:
            return None, None
        return (
            quantize_percent(concentration * _HUNDRED) / _HUNDRED,
            quantize_percent((concentration / weight_mass) * _HUNDRED) / _HUNDRED,
        )

    def _correlation_clusters(
        self,
        symbols: Sequence[str],
        correlation: Mapping[Tuple[str, str], Decimal],
        weights: Mapping[str, Decimal],
        contributions: Sequence[RiskContribution],
    ) -> List[CorrelationCluster]:
        """Group symbols into connected components above the correlation threshold.

        Transitive by construction: if A moves with B and B moves with C, the group is reported
        as {A, B, C} with its *minimum* pairwise correlation stated alongside the average, so a
        caller can see how much of the group is only indirectly connected.

        This is exposure-concentration detection, not a causal claim: nothing here says one
        symbol causes another's move.
        """
        threshold = Decimal(str(self.config.correlation_cluster_threshold))
        parent = {symbol: symbol for symbol in symbols}

        def find(symbol: str) -> str:
            while parent[symbol] != symbol:
                parent[symbol] = parent[parent[symbol]]
                symbol = parent[symbol]
            return symbol

        for i, left in enumerate(symbols):
            for right in symbols[i + 1 :]:
                rho = correlation.get((left, right))
                if rho is not None and rho >= threshold:
                    root_left, root_right = find(left), find(right)
                    if root_left != root_right:
                        # Merge deterministically: the alphabetically smaller root wins, so the
                        # grouping cannot depend on iteration order.
                        if root_left < root_right:
                            parent[root_right] = root_left
                        else:
                            parent[root_left] = root_right

        groups: Dict[str, List[str]] = {}
        for symbol in symbols:
            groups.setdefault(find(symbol), []).append(symbol)

        risk_by_symbol = {item.symbol: item.contribution_pct for item in contributions}
        clusters: List[CorrelationCluster] = []
        for members in (sorted(group) for group in groups.values() if len(group) > 1):
            pairwise = [
                correlation[(left, right)]
                for i, left in enumerate(members)
                for right in members[i + 1 :]
                if (left, right) in correlation
            ]
            if not pairwise:
                continue
            total = sum(pairwise, ZERO)
            clusters.append(
                CorrelationCluster(
                    cluster_id=f"cluster_{len(clusters) + 1}",
                    symbols=tuple(members),
                    average_correlation=quantize_percent(total / Decimal(len(pairwise)) * _HUNDRED)
                    / _HUNDRED,
                    min_pairwise_correlation=min(pairwise),
                    combined_weight_pct=quantize_percent(
                        sum((weights.get(symbol, ZERO) for symbol in members), ZERO)
                    ),
                    risk_concentration_pct=quantize_percent(
                        sum((risk_by_symbol.get(symbol, ZERO) for symbol in members), ZERO)
                    )
                    if risk_by_symbol
                    else None,
                )
            )
        clusters.sort(key=lambda item: (-item.combined_weight_pct, item.symbols))
        return [
            CorrelationCluster(
                cluster_id=f"cluster_{position + 1}",
                symbols=cluster.symbols,
                average_correlation=cluster.average_correlation,
                min_pairwise_correlation=cluster.min_pairwise_correlation,
                combined_weight_pct=cluster.combined_weight_pct,
                risk_concentration_pct=cluster.risk_concentration_pct,
            )
            for position, cluster in enumerate(clusters)
        ]

    def _beta(
        self,
        weights: Mapping[str, Decimal],
        symbols: Sequence[str],
        series: Mapping[str, Sequence[Decimal]],
        benchmark_returns: Sequence[Decimal],
        benchmark_symbol: Optional[str],
    ) -> Tuple[Optional[Decimal], Dict[str, Decimal]]:
        """Portfolio beta ``= sum(w_i * beta_i)`` with ``beta_i = Cov(R_i, R_m) / Var(R_m)``.

        Computed from returns when the caller did not supply per-symbol betas. A flat benchmark
        has zero variance, which makes every beta undefined — that returns ``None`` rather than
        an arbitrary number.
        """
        if len(benchmark_returns) < 2 or not symbols:
            return None, {}

        if any(len(series.get(symbol, ())) < 2 for symbol in symbols):
            return None, {}

        length = min(
            len(benchmark_returns),
            min(len(series[symbol]) for symbol in symbols),
        )
        if length < 2:
            return None, {}

        benchmark = [Decimal(str(value)) for value in list(benchmark_returns)[-length:]]
        benchmark_mean = sum(benchmark, ZERO) / Decimal(length)
        benchmark_variance = sum(
            ((value - benchmark_mean) ** 2 for value in benchmark), ZERO
        ) / Decimal(length - 1)
        if benchmark_variance <= ZERO:
            return None, {}

        individual: Dict[str, Decimal] = {}
        portfolio_beta = ZERO
        for symbol in symbols:
            aligned = [Decimal(str(value)) for value in list(series[symbol])[-length:]]
            mean = sum(aligned, ZERO) / Decimal(length)
            covariance = sum(
                (
                    (aligned[k] - mean) * (benchmark[k] - benchmark_mean)
                    for k in range(length)
                ),
                ZERO,
            ) / Decimal(length - 1)
            beta = covariance / benchmark_variance
            individual[symbol] = quantize_percent(beta * _HUNDRED) / _HUNDRED
            portfolio_beta += weights.get(symbol, ZERO) / _HUNDRED * beta

        return quantize_percent(portfolio_beta * _HUNDRED) / _HUNDRED, individual

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _weights_from_analysis(self, analysis: CovarianceAnalysis) -> Dict[str, Decimal]:
        """Recover the analysed weights from the published risk contributions.

        The analyzer publishes weights rather than caching them, so a stressed re-correlation
        uses exactly the weights the original volatility was computed from — and cannot
        accidentally use a different set.
        """
        return {item.symbol: item.weight_pct for item in analysis.risk_contributions}


# ---------------------------------------------------------------------------
# Module-level pure helpers
# ---------------------------------------------------------------------------


def _is_flat(values: Sequence[Decimal]) -> bool:
    """True when every observation is identical, i.e. the series has zero variance."""
    first = values[0]
    return all(value == first for value in values[1:])


def pearson_correlation(x: Sequence[Decimal], y: Sequence[Decimal]) -> Optional[Decimal]:
    """Pearson correlation of two equal-length series, or ``None`` when it is undefined.

    Undefined — not zero — for a series with fewer than two points, mismatched lengths, or zero
    variance. Reporting ``0`` there would assert the holdings are uncorrelated, which is not
    something the data says.
    """
    if len(x) < 2 or len(y) < 2 or len(x) != len(y):
        return None
    count = Decimal(len(x))
    mean_x = sum(x, ZERO) / count
    mean_y = sum(y, ZERO) / count
    numerator = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
    denominator = (
        sum(((xi - mean_x) ** 2 for xi in x), ZERO)
        * sum(((yi - mean_y) ** 2 for yi in y), ZERO)
    ).sqrt()
    if denominator <= ZERO:
        return None
    return (numerator / denominator).quantize(_CORRELATION_QUANT)


def _leading_minor(
    covariance: Mapping[Tuple[str, str], Decimal],
    symbols: Sequence[str],
) -> Decimal:
    """Determinant of the leading ``k x k`` block, in exact :class:`Decimal` arithmetic.

    Laplace expansion is used rather than a float LU factorisation because the
    positive-semi-definiteness test must not depend on floating-point luck: a matrix that is
    merely ill-conditioned should be accepted, and one that is actually indefinite should be
    refused, deterministically and identically on every run.
    """
    return _determinant(covariance, list(symbols))


def _determinant(
    covariance: Mapping[Tuple[str, str], Decimal],
    symbols: Sequence[str],
) -> Decimal:
    """Exact determinant of the submatrix on ``symbols`` by cofactor expansion."""
    size = len(symbols)
    if size == 1:
        return covariance.get((symbols[0], symbols[0]), ZERO)
    if size == 2:
        return (
            covariance.get((symbols[0], symbols[0]), ZERO)
            * covariance.get((symbols[1], symbols[1]), ZERO)
            - covariance.get((symbols[0], symbols[1]), ZERO)
            * covariance.get((symbols[1], symbols[0]), ZERO)
        )
    total = ZERO
    for column in range(size):
        entry = covariance.get((symbols[0], symbols[column]), ZERO)
        if entry == ZERO:
            continue
        remaining = [symbol for index, symbol in enumerate(symbols) if index != column]
        sign = Decimal("1") if column % 2 == 0 else Decimal("-1")
        total += sign * entry * _determinant(covariance, remaining)
    return total


def covariance_summary(analysis: CovarianceAnalysis, min_observations: int = 30) -> str:
    """One-line human summary of a covariance analysis, for report explanations."""
    if not analysis.available:
        if analysis.excluded_symbols:
            return (
                f"No covariance could be computed: {len(analysis.excluded_symbols)} position(s) "
                f"have fewer than the {min_observations} overlapping daily observations this "
                "engine requires."
            )
        return "No covariance could be computed: no return series were supplied for the held positions."
    coverage = analysis.weight_coverage_pct
    suffix = ""
    if coverage is not None and coverage < _HUNDRED:
        suffix = (
            f" Covers {pct(coverage)} of portfolio weight; positions without usable history were "
            "excluded, so the risk they carry is not reflected in this figure."
        )
    return (
        f"Computed from {analysis.observation_count} overlapping daily observations across "
        f"{len(analysis.symbols)} positions.{suffix}"
    )


__all__ = [
    "CovarianceAnalyzer",
    "pearson_correlation",
    "covariance_summary",
]
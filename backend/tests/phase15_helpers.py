"""Shared helpers for the Phase 15 portfolio risk tests.

Deterministic and offline: no provider is contacted, no network call is made, and every expected
figure is derived from the rule under test rather than copied from a run.

The return builders here produce **deterministic but non-degenerate** series: repeated cycles with
different periods, so correlations between two series are real and reproducible instead of an
artefact of a random draw. A seeded ``random`` generator would also be reproducible, but a cyclic
series makes the expected correlation in a test obvious by inspection.
"""

from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple

from app.schemas.portfolio_risk import (
    PortfolioRiskConfigSchema,
    PortfolioRiskPositionSchema,
    PortfolioRiskRequestSchema,
)
from app.services.backtest.models import quantize_percent
from app.services.portfolio_risk.config import ZERO, PortfolioRiskConfig
from app.services.portfolio_risk.models import RiskPosition, RiskSnapshot
from app.services.portfolio_risk.risk_metrics import sample_std_dev

#: Observations used whenever a series must clear the default 30-observation floor.
LONG_SERIES = 60
#: Observations used to test the "too short to measure" path.
SHORT_SERIES = 5

ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# Configurations
# ---------------------------------------------------------------------------


def config(**overrides) -> PortfolioRiskConfig:
    """The default policy with the given limits replaced, validated."""
    if not overrides:
        return PortfolioRiskConfig().validate()
    return PortfolioRiskConfig().validate().replace(**overrides)


def wide_policy(**overrides) -> PortfolioRiskConfig:
    """A policy that no realistic portfolio breaches — the "nothing to correct" baseline."""
    defaults = {
        "max_single_asset_weight": Decimal("100"),
        "max_sector_weight": Decimal("100"),
        "max_strategy_weight": Decimal("100"),
        "max_top_n_concentration": Decimal("100"),
        "max_portfolio_volatility": Decimal("100"),
        "max_portfolio_beta": Decimal("100"),
        "max_drawdown": Decimal("100"),
        "max_correlation_concentration": Decimal("100"),
        "max_stress_loss": Decimal("100"),
        "top_n_count": 50,
        "mandatory_limits": ("max_single_asset_weight",),
    }
    defaults.update(overrides)
    return config(**defaults)


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------


def position(
    symbol: str,
    quantity: int,
    price,
    *,
    strategy: Optional[str] = "MOM",
    sector: Optional[str] = "Technology",
    industry: Optional[str] = None,
    asset_class: Optional[str] = None,
    beta: Optional[Decimal] = None,
    avg_daily_traded_value: Optional[Decimal] = None,
    liquidity_score: Optional[Decimal] = None,
) -> RiskPosition:
    """One engine-facing position with explicit, deterministic fields."""
    return RiskPosition(
        symbol=symbol,
        quantity=quantity,
        price=Decimal(str(price)),
        strategy=strategy,
        sector=sector,
        industry=industry,
        asset_class=asset_class,
        beta=None if beta is None else Decimal(str(beta)),
        avg_daily_traded_value=(
            None if avg_daily_traded_value is None else Decimal(str(avg_daily_traded_value))
        ),
        liquidity_score=(
            None if liquidity_score is None else Decimal(str(liquidity_score))
        ),
    )


def schema_position(symbol: str, quantity: int, price, **kwargs) -> PortfolioRiskPositionSchema:
    """The same position as an API request entry."""
    return PortfolioRiskPositionSchema(
        symbol=symbol,
        quantity=quantity,
        price=Decimal(str(price)),
        **kwargs,
    )


def market_value(positions: Sequence[RiskPosition]) -> Decimal:
    """Total market value of a position list."""
    return sum((item.market_value for item in positions), ZERO)


# ---------------------------------------------------------------------------
# Deterministic return series
# ---------------------------------------------------------------------------


def cyclic(amplitude, period: int, count: int = LONG_SERIES, offset: int = 0) -> List[Decimal]:
    """A repeating ramp of ``count`` returns with the given period and amplitude.

    Deterministic and periodic: two series with the same period are perfectly correlated and two
    with different periods are not, which is what the correlation, cluster and covariance tests
    need to assert on.
    """
    return [Decimal(str(amplitude)) * Decimal((index + offset) % period) for index in range(count)]


def perfect_pair(count: int = LONG_SERIES, scale: str = "0.01") -> Tuple[List[Decimal], List[Decimal]]:
    """Two series with correlation exactly 1 and identical variance.

    The second is the first plus a constant, so it is an exact affine image of it: the correlation
    is exactly 1 and, because a constant shift moves the mean but not the deviations, the variance
    is identical. Scaling the second series also correlates perfectly, but it quadruples its
    variance, which quietly invalidates any hand-checked portfolio variance.
    """
    base = [Decimal(scale) * Decimal(index + 1) for index in range(count)]
    return base, [value + Decimal("0.005") for value in base]


def independent_pair(
    count: int = LONG_SERIES, scale: str = "0.01"
) -> Tuple[List[Decimal], List[Decimal]]:
    """Two series whose sample correlation is exactly zero by construction.

    The first alternates on a period of two observations and the second on a period of four, so
    over each four-step cycle every positive product is cancelled by a negative one and the
    correlation is exactly zero. This stands in for "independent" without pulling a seeded RNG
    into the suite: a seeded generator is still a hidden input that silently changes the expected
    numbers if it is ever swapped, while this construction cannot drift.
    """
    size = Decimal(scale)
    return (
        [size if index % 2 == 0 else -size for index in range(count)],
        [size if index % 4 < 2 else -size for index in range(count)],
    )


def rescale_to_volatility(
    series: Sequence[Decimal], target: Decimal
) -> List[Decimal]:
    """Scale ``series`` by a constant so its sample standard deviation equals ``target``.

    Two books can only be compared on correlation if their individual volatilities match; without
    this the "diversified" book is simply the quieter one and the comparison proves nothing.
    """
    current = sample_std_dev(list(series))
    if current <= ZERO:
        raise ValueError("a flat series has no volatility to rescale")
    factor = Decimal(str(target)) / current
    return [quantize_percent(value * factor * Decimal("100")) / Decimal("100") for value in series]


def oscillating(count: int = LONG_SERIES, scale: str = "0.01") -> List[Decimal]:
    """A series that gains, then loses — the shape a drawdown needs.

    The second half is negative, so compounding it pulls the equity curve back below its running
    peak. An earlier version of this helper only made the returns *smaller* in the second half,
    which left every return positive: the equity never fell, and a drawdown test written against
    it would have passed while checking nothing.
    """
    step = Decimal(scale)
    half = max(1, count // 2)
    values: List[Decimal] = [step * Decimal(index + 1) for index in range(half)]
    values.extend(-step * Decimal(count - index) for index in range(half, count))
    return values


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


def snapshot(
    positions: Sequence[RiskPosition],
    *,
    portfolio_id: Optional[int] = None,
    cash=None,
    asset_returns: Optional[Dict[str, Sequence[Decimal]]] = None,
    portfolio_returns: Optional[Sequence[Decimal]] = None,
    equity_curve: Optional[Sequence[Decimal]] = None,
    benchmark_symbol: Optional[str] = None,
    benchmark_returns: Optional[Sequence[Decimal]] = None,
) -> RiskSnapshot:
    """A snapshot whose equity reconciles with its cash and positions.

    ``portfolio_value`` defaults to cash plus market value, because the engine refuses an equity
    that does not reconcile — a helper that produced one would fail every test for the wrong
    reason.
    """
    invested = market_value(positions)
    resolved_cash = Decimal(str(cash)) if cash is not None else ZERO
    return RiskSnapshot(
        portfolio_id=portfolio_id,
        portfolio_value=invested + resolved_cash,
        cash=resolved_cash,
        positions=tuple(positions),
        asset_returns={
            symbol: tuple(Decimal(str(value)) for value in values)
            for symbol, values in (asset_returns or {}).items()
        },
        portfolio_returns=tuple(
            Decimal(str(value)) for value in (portfolio_returns or [])
        ),
        equity_curve=tuple(Decimal(str(value)) for value in (equity_curve or [])),
        benchmark_symbol=benchmark_symbol,
        benchmark_returns=tuple(
            Decimal(str(value)) for value in (benchmark_returns or [])
        ),
    )


def diversified_snapshot(**overrides) -> RiskSnapshot:
    """A four-position, two-sector, two-strategy book that needs no correction.

    Used as the baseline for assertions about "a healthy portfolio is approved", so that any test
    failure points at the rule under test rather than at a portfolio that happened to breach
    something unrelated.
    """
    positions = [
        position("AAA", 50, 100, strategy="MOM", sector="Technology", beta="1.2"),
        position("BBB", 40, 125, strategy="MOM", sector="Technology", beta="1.1"),
        position("CCC", 30, 100, strategy="VAL", sector="Healthcare", beta="0.8"),
        position("DDD", 24, 125, strategy="VAL", sector="Healthcare", beta="0.7"),
    ]
    returns = {
        "AAA": cyclic("0.010", 7),
        "BBB": cyclic("0.008", 7),
        "CCC": cyclic("0.006", 5),
        "DDD": cyclic("0.004", 5),
    }
    defaults = {
        "positions": positions,
        "portfolio_id": 1,
        "cash": "4000",
        "asset_returns": returns,
        "portfolio_returns": cyclic("0.003", 9),
        "benchmark_symbol": "SPY",
        "benchmark_returns": cyclic("0.002", 7),
    }
    defaults.update(overrides)
    return snapshot(**defaults)


# ---------------------------------------------------------------------------
# API request bodies
# ---------------------------------------------------------------------------


def request_body(
    positions: Sequence[RiskPosition],
    *,
    portfolio_id: Optional[int] = None,
    cash: str = "0",
    asset_returns: Optional[Dict[str, Sequence[Decimal]]] = None,
    portfolio_returns: Optional[Sequence[Decimal]] = None,
    equity_curve: Optional[Sequence[Decimal]] = None,
    benchmark_symbol: Optional[str] = None,
    benchmark_returns: Optional[Sequence[Decimal]] = None,
    risk_config: Optional[PortfolioRiskConfigSchema] = None,
) -> PortfolioRiskRequestSchema:
    """A valid request body for the API, with equity reconciled against its positions."""
    entries = [
        {
            "symbol": item.symbol,
            "quantity": item.quantity,
            "price": str(item.price),
            "strategy": item.strategy,
            "sector": item.sector,
            "industry": item.industry,
            "asset_class": item.asset_class,
            "beta": None if item.beta is None else str(item.beta),
            "avg_daily_traded_value": (
                None if item.avg_daily_traded_value is None else str(item.avg_daily_traded_value)
            ),
            "liquidity_score": (
                None if item.liquidity_score is None else str(item.liquidity_score)
            ),
        }
        for item in positions
    ]
    resolved_cash = Decimal(str(cash))
    return PortfolioRiskRequestSchema(
        portfolio_id=portfolio_id,
        portfolio_value=market_value(positions) + resolved_cash,
        cash=resolved_cash,
        positions=entries,
        asset_returns={
            symbol: [str(value) for value in values]
            for symbol, values in (asset_returns or {}).items()
        },
        portfolio_returns=(
            None if portfolio_returns is None else [str(value) for value in portfolio_returns]
        ),
        equity_curve=(
            None if equity_curve is None else [str(value) for value in equity_curve]
        ),
        benchmark_symbol=benchmark_symbol,
        benchmark_returns=(
            None if benchmark_returns is None else [str(value) for value in benchmark_returns]
        ),
        risk_config=risk_config,
    )


def body_json(request: PortfolioRiskRequestSchema) -> dict:
    """The JSON-ready form of a request, as the API test client needs it."""
    return request.model_dump(mode="json", exclude_none=True)


__all__ = [
    "LONG_SERIES",
    "SHORT_SERIES",
    "ZERO",
    "config",
    "wide_policy",
    "position",
    "schema_position",
    "market_value",
    "cyclic",
    "perfect_pair",
    "independent_pair",
    "oscillating",
    "snapshot",
    "diversified_snapshot",
    "request_body",
    "body_json",
]
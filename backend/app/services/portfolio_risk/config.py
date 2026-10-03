"""Phase 15 — centralised portfolio risk configuration.

Every portfolio-level limit, stress magnitude and evaluation threshold used by the Phase 15
engine is declared here. Nothing in the analyzers, the stress tester, the controller or the
engine reads a literal threshold: they all take a :class:`PortfolioRiskConfig` and ask it. That
is what makes the limits *policy parameters* rather than constants buried in arithmetic — a
deployment sets its own mandate in :mod:`app.core.config` and every rule follows.

Two conventions matter when reading this file:

``percent`` vs ``ratio``
    Percentages are **percent** (``20`` means 20%). The only fractions here are quantities that
    genuinely are ratios: the correlation-cluster threshold and the VaR confidence level.

``None`` means "no rule"
    An unset limit **disables** its rule, which is deliberately different from setting it to
    zero. ``max_sector_weight=None`` means "do not police sector concentration", not "no sector
    may exceed 0%".

The defaults below are the shipped starting point of this engine. They are not universal
investment limits and carry no claim to be optimal; they are defaults, overridable per request.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Optional, Tuple

from app.core.exceptions import InvalidRiskConfigurationError

#: Absolute zero, reused for sign comparisons.
ZERO = Decimal("0")
#: One hundred — the scale of every percentage in this package.
_HUNDRED = Decimal("100")
#: Numerical tolerance for floating-point comparisons. Never compare risk figures with ``==``.
EPSILON = Decimal("1e-8")


def _opt_decimal(value) -> Optional[Decimal]:
    """``None`` for an unset setting, :class:`Decimal` otherwise."""
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _opt_int(value) -> Optional[int]:
    """``None`` for an unset setting, ``int`` otherwise."""
    if value is None or value == "":
        return None
    return int(value)


@dataclass(frozen=True)
class PortfolioRiskConfig:
    """One portfolio-level risk policy.

    A frozen dataclass so a policy cannot be mutated halfway through an analysis: the report
    states the policy it was produced under, and that statement has to stay true. Per-request
    overrides go through :meth:`replace`, which yields a new policy rather than an edit.

    ``validate()`` refuses a policy the engine cannot act on rather than repairing it. A risk
    limit silently clamped is a risk decision made by code the caller never read.
    """

    # ─── Concentration limits ─────────────────────────────────────────────
    #: Maximum share of equity a single instrument may represent.
    max_single_asset_weight: Decimal = Decimal("20")
    #: Maximum share of equity one sector may represent.
    max_sector_weight: Optional[Decimal] = Decimal("35")
    #: Maximum share of equity one strategy may represent.
    max_strategy_weight: Optional[Decimal] = Decimal("50")
    #: Maximum share of equity one asset class may represent.
    max_asset_class_weight: Optional[Decimal] = None
    #: Maximum combined weight of the ``top_n_count`` largest positions.
    max_top_n_concentration: Optional[Decimal] = Decimal("60")
    #: How many largest positions the top-N rule considers.
    top_n_count: int = 10

    # ─── Portfolio risk metrics ───────────────────────────────────────────
    #: Maximum annualised portfolio volatility.
    max_portfolio_volatility: Optional[Decimal] = Decimal("30")
    #: Maximum portfolio beta versus the benchmark.
    max_portfolio_beta: Optional[Decimal] = Decimal("1.50")
    #: Maximum historical peak-to-trough drawdown magnitude.
    max_drawdown: Optional[Decimal] = Decimal("25")
    #: Maximum weighted average pairwise correlation across the book.
    max_correlation_concentration: Optional[Decimal] = Decimal("60")

    # ─── Stress scenario magnitudes (positive percentages) ───────────────
    market_crash_shock: Decimal = Decimal("20")
    severe_crash_shock: Decimal = Decimal("30")
    sector_shock: Decimal = Decimal("25")
    high_beta_shock: Decimal = Decimal("10")
    #: Extra loss per unit of beta above 1.0, in percentage points.
    high_beta_tilt: Decimal = Decimal("10")
    strategy_shock: Decimal = Decimal("20")
    #: Extra haircut applied to illiquid positions, in percentage points.
    liquidity_haircut: Decimal = Decimal("5")
    #: Share of each off-diagonal correlation added by the correlation-spike scenario.
    correlation_spike_multiplier: Decimal = Decimal("0.30")
    #: Volatility scaling applied alongside the correlation spike.
    correlation_spike_vol_multiplier: Decimal = Decimal("1.20")

    # ─── Stress limits ────────────────────────────────────────────────────
    #: Maximum estimated loss under any single scenario.
    max_stress_loss: Optional[Decimal] = Decimal("20")
    #: Run no stress scenarios at all when true.
    disable_stress_tests: bool = False

    # ─── Liquidity ────────────────────────────────────────────────────────
    minimum_liquidity_score: Optional[Decimal] = None
    #: Maximum position value / average daily traded value before a position is flagged.
    max_participation_ratio: Optional[Decimal] = Decimal("0.20")

    # ─── Position counts ──────────────────────────────────────────────────
    minimum_position_count: Optional[int] = None
    maximum_position_count: Optional[int] = None

    # ─── Evaluation policy ────────────────────────────────────────────────
    #: Utilization, as a percent of a limit, at which a metric becomes WARNING.
    warning_utilization_pct: Decimal = Decimal("80")
    #: Periods per year used to annualise daily volatility.
    annualization_periods: int = 252
    #: Minimum overlapping observations before covariance-based metrics are computed.
    min_observations: int = 30
    #: Minimum observations before historical VaR / Expected Shortfall are computed.
    min_var_observations: int = 10
    #: Correlation at or above which two positions join one risk cluster.
    correlation_cluster_threshold: Decimal = Decimal("0.75")
    #: Ceiling on adjustment passes; each pass either fixes a rule or stops the loop.
    max_adjustment_passes: int = 12

    # ─── VaR / Expected Shortfall ────────────────────────────────────────
    var_confidence: Decimal = Decimal("0.95")
    var_horizon_days: int = 1

    # ─── Benchmark ────────────────────────────────────────────────────────
    benchmark_symbol: Optional[str] = None
    #: Sector shocked by the sector scenario; the largest sector is used when unset.
    stress_sector: Optional[str] = None

    # ─── Behaviour ────────────────────────────────────────────────────────
    enable_adjustment: bool = True

    # ─── Risk budgets ─────────────────────────────────────────────────────
    risk_budget_asset_concentration: Optional[Decimal] = None
    risk_budget_sector_concentration: Optional[Decimal] = None
    risk_budget_strategy_concentration: Optional[Decimal] = None
    risk_budget_volatility: Optional[Decimal] = None
    risk_budget_beta: Optional[Decimal] = None
    risk_budget_drawdown: Optional[Decimal] = None
    risk_budget_var: Optional[Decimal] = None
    risk_budget_stress: Optional[Decimal] = None

    # ─── Mandatory vs adjustable ──────────────────────────────────────────
    #: Limit names whose surviving breach rejects the portfolio. Every other limit is
    #: adjustable: a breach is corrected by deterministic risk reduction, or the portfolio is
    #: rejected if no correction exists.
    mandatory_limits: Tuple[str, ...] = field(
        default_factory=lambda: (
            "max_single_asset_weight",
            "max_sector_weight",
            "max_strategy_weight",
            "max_portfolio_volatility",
            "max_drawdown",
            "max_stress_loss",
        )
    )

    # ─── Validation ───────────────────────────────────────────────────────

    def validate(self) -> "PortfolioRiskConfig":
        """Raise :class:`InvalidRiskConfigurationError` unless this policy is actionable."""
        for name in (
            "max_single_asset_weight",
            "max_sector_weight",
            "max_strategy_weight",
            "max_asset_class_weight",
            "max_top_n_concentration",
            "max_portfolio_volatility",
            "max_portfolio_beta",
            "max_drawdown",
            "max_correlation_concentration",
            "max_stress_loss",
            "minimum_liquidity_score",
            "market_crash_shock",
            "severe_crash_shock",
            "sector_shock",
            "high_beta_shock",
            "strategy_shock",
        ):
            self._require_percent(name)

        for name in ("high_beta_tilt", "liquidity_haircut", "warning_utilization_pct"):
            value = getattr(self, name)
            if value is None or not (ZERO <= value <= _HUNDRED):
                raise InvalidRiskConfigurationError(f"{name} must be in [0, 100]")

        participation = self.max_participation_ratio
        if participation is not None and not (ZERO < participation <= _HUNDRED):
            raise InvalidRiskConfigurationError("max_participation_ratio must be in (0, 100]")

        if not (ZERO <= self.correlation_cluster_threshold <= Decimal("1")):
            raise InvalidRiskConfigurationError("correlation_cluster_threshold must be in [0, 1]")

        if not (ZERO < self.var_confidence < Decimal("1")):
            raise InvalidRiskConfigurationError("var_confidence must be in (0, 1)")

        spike = self.correlation_spike_multiplier
        if not (ZERO <= spike <= Decimal("1")):
            raise InvalidRiskConfigurationError("correlation_spike_multiplier must be in [0, 1]")

        vol_spike = self.correlation_spike_vol_multiplier
        if not (ZERO < vol_spike <= Decimal("5")):
            raise InvalidRiskConfigurationError("correlation_spike_vol_multiplier must be in (0, 5]")

        for name in (
            "top_n_count",
            "min_observations",
            "min_var_observations",
            "var_horizon_days",
            "annualization_periods",
            "max_adjustment_passes",
        ):
            value = getattr(self, name)
            if value is None or value < 1:
                raise InvalidRiskConfigurationError(f"{name} must be at least 1")

        for name in ("minimum_position_count", "maximum_position_count"):
            value = getattr(self, name)
            if value is not None and value < 1:
                raise InvalidRiskConfigurationError(f"{name} must be at least 1 when set")

        if (
            self.minimum_position_count is not None
            and self.maximum_position_count is not None
            and self.minimum_position_count > self.maximum_position_count
        ):
            raise InvalidRiskConfigurationError(
                "minimum_position_count must not exceed maximum_position_count"
            )

        if not self.mandatory_limits:
            raise InvalidRiskConfigurationError(
                "mandatory_limits must name at least one limit; an empty set would let every "
                "breach be adjusted away"
            )
        for limit_name in self.mandatory_limits:
            if not hasattr(self, limit_name):
                raise InvalidRiskConfigurationError(
                    f"mandatory_limits references unknown limit: {limit_name}"
                )

        return self

    def _require_percent(self, name: str) -> None:
        """A limit expressed as a percent must be ``None`` or inside ``(0, 100]``."""
        value = getattr(self, name)
        if value is not None and not (ZERO < value <= _HUNDRED):
            raise InvalidRiskConfigurationError(f"{name} must be in (0, 100] when set")

    # ─── Accessors ────────────────────────────────────────────────────────

    def replace(self, **changes) -> "PortfolioRiskConfig":
        """A copy with the given fields replaced — the only supported way to override a limit."""
        unknown = set(changes) - {f for f in self.__dataclass_fields__}
        if unknown:
            raise InvalidRiskConfigurationError(
                f"Unknown portfolio risk limit(s): {', '.join(sorted(unknown))}"
            )
        return replace(self, **changes)

    def is_mandatory(self, limit_name: str) -> bool:
        """True when a surviving breach of ``limit_name`` rejects the portfolio."""
        return limit_name in self.mandatory_limits

    def get_limit(self, limit_name: str) -> Optional[Decimal]:
        """The configured value of ``limit_name``, or ``None`` when the rule is disabled."""
        return getattr(self, limit_name, None)

    def utilization(self, value: Optional[Decimal], limit: Optional[Decimal]) -> Optional[Decimal]:
        """``value / limit`` as a percent, or ``None`` when it cannot be computed.

        ``None`` — never zero — is returned for a missing value or a missing limit, because a
        metric with no limit has no utilization and a metric with no value has no risk to
        report.
        """
        if value is None or limit is None or limit <= ZERO:
            return None
        from app.services.backtest.models import quantize_percent

        return quantize_percent(value / limit * _HUNDRED)


def default_portfolio_risk_config() -> PortfolioRiskConfig:
    """Build the default policy from application settings.

    Imported lazily so the settings module is not read at import time by the pure engine.
    """
    from app.core.config import settings

    return PortfolioRiskConfig(
        max_single_asset_weight=Decimal(str(settings.PORTFOLIO_RISK_MAX_SINGLE_ASSET_WEIGHT)),
        max_sector_weight=_opt_decimal(settings.PORTFOLIO_RISK_MAX_SECTOR_WEIGHT),
        max_strategy_weight=_opt_decimal(settings.PORTFOLIO_RISK_MAX_STRATEGY_WEIGHT),
        max_asset_class_weight=_opt_decimal(settings.PORTFOLIO_RISK_MAX_ASSET_CLASS_WEIGHT),
        max_top_n_concentration=_opt_decimal(settings.PORTFOLIO_RISK_MAX_TOP_N_CONCENTRATION),
        top_n_count=int(settings.PORTFOLIO_RISK_TOP_N_COUNT),
        max_portfolio_volatility=_opt_decimal(settings.PORTFOLIO_RISK_MAX_VOLATILITY),
        max_portfolio_beta=_opt_decimal(settings.PORTFOLIO_RISK_MAX_BETA),
        max_drawdown=_opt_decimal(settings.PORTFOLIO_RISK_MAX_DRAWDOWN),
        max_correlation_concentration=_opt_decimal(settings.PORTFOLIO_RISK_MAX_CORR_CONCENTRATION),
        market_crash_shock=Decimal(str(settings.PORTFOLIO_RISK_MARKET_CRASH_SHOCK)),
        severe_crash_shock=Decimal(str(settings.PORTFOLIO_RISK_SEVERE_CRASH_SHOCK)),
        sector_shock=Decimal(str(settings.PORTFOLIO_RISK_SECTOR_SHOCK)),
        high_beta_shock=Decimal(str(settings.PORTFOLIO_RISK_HIGH_BETA_SHOCK)),
        high_beta_tilt=Decimal(str(settings.PORTFOLIO_RISK_HIGH_BETA_TILT)),
        strategy_shock=Decimal(str(settings.PORTFOLIO_RISK_STRATEGY_SHOCK)),
        liquidity_haircut=Decimal(str(settings.PORTFOLIO_RISK_LIQUIDITY_HAIRCUT)),
        correlation_spike_multiplier=Decimal(
            str(settings.PORTFOLIO_RISK_CORRELATION_SPIKE_MULTIPLIER)
        ),
        correlation_spike_vol_multiplier=Decimal(
            str(settings.PORTFOLIO_RISK_CORRELATION_SPIKE_VOL_MULTIPLIER)
        ),
        max_stress_loss=_opt_decimal(settings.PORTFOLIO_RISK_MAX_STRESS_LOSS),
        minimum_liquidity_score=_opt_decimal(settings.PORTFOLIO_RISK_MIN_LIQUIDITY_SCORE),
        max_participation_ratio=_opt_decimal(settings.PORTFOLIO_RISK_MAX_PARTICIPATION_RATIO),
        minimum_position_count=_opt_int(settings.PORTFOLIO_RISK_MIN_POSITION_COUNT),
        maximum_position_count=_opt_int(settings.PORTFOLIO_RISK_MAX_POSITION_COUNT),
        warning_utilization_pct=Decimal(str(settings.PORTFOLIO_RISK_WARNING_UTILIZATION)),
        annualization_periods=int(settings.PORTFOLIO_RISK_ANNUALIZATION_PERIODS),
        min_observations=int(settings.PORTFOLIO_RISK_MIN_OBSERVATIONS),
        min_var_observations=int(settings.PORTFOLIO_RISK_MIN_VAR_OBSERVATIONS),
        correlation_cluster_threshold=Decimal(
            str(settings.PORTFOLIO_RISK_CORR_CLUSTER_THRESHOLD)
        ),
        benchmark_symbol=settings.PORTFOLIO_RISK_BENCHMARK_SYMBOL,
        var_confidence=Decimal(str(settings.PORTFOLIO_RISK_VAR_CONFIDENCE)),
        var_horizon_days=int(settings.PORTFOLIO_RISK_VAR_HORIZON_DAYS),
        risk_budget_asset_concentration=_opt_decimal(
            settings.PORTFOLIO_RISK_BUDGET_ASSET_CONC
        ),
        risk_budget_sector_concentration=_opt_decimal(
            settings.PORTFOLIO_RISK_BUDGET_SECTOR_CONC
        ),
        risk_budget_strategy_concentration=_opt_decimal(
            settings.PORTFOLIO_RISK_BUDGET_STRATEGY_CONC
        ),
        risk_budget_volatility=_opt_decimal(settings.PORTFOLIO_RISK_BUDGET_VOLATILITY),
        risk_budget_beta=_opt_decimal(settings.PORTFOLIO_RISK_BUDGET_BETA),
        risk_budget_drawdown=_opt_decimal(settings.PORTFOLIO_RISK_BUDGET_DRAWDOWN),
        risk_budget_var=_opt_decimal(settings.PORTFOLIO_RISK_BUDGET_VAR),
        risk_budget_stress=_opt_decimal(settings.PORTFOLIO_RISK_BUDGET_STRESS),
        mandatory_limits=tuple(
            name.strip()
            for name in str(settings.PORTFOLIO_RISK_MANDATORY_LIMITS).split(",")
            if name.strip()
        ),
        max_adjustment_passes=int(settings.PORTFOLIO_RISK_MAX_ADJUSTMENT_PASSES),
        enable_adjustment=bool(settings.PORTFOLIO_RISK_ENABLE_ADJUSTMENT),
        disable_stress_tests=bool(settings.PORTFOLIO_RISK_DISABLE_STRESS_TESTS),
    ).validate()


#: Module-level policy built from the application settings, matching the singleton pattern used
#: by every other service in this project. Callers needing a different mandate call
#: :meth:`PortfolioRiskConfig.replace` instead of mutating this.
portfolio_risk_config = default_portfolio_risk_config()
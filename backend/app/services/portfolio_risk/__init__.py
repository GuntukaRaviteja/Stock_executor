"""Phase 15 — portfolio-level risk management, stress testing and exposure control.

This package sits **above** Phase 14's strategy capital allocation and answers a different
question from every layer below it. Phase 13 owns strategy-level portfolio construction, Phase 14
owns how much capital each strategy receives; Phase 15 asks whether the resulting portfolio is
acceptable, and if not, what to do about it.

::

    Market Data
      -> Analytics -> Signals -> Backtesting
      -> Strategy Portfolios
      -> Phase 13 Portfolio Engine      strategy-level sizing, allocation and risk checks
      -> Phase 14 Multi-Strategy Optimizer   how much capital each strategy gets
      -> Phase 15 Portfolio Risk Engine      this package
      -> Risk-Adjusted Portfolio
      -> Execution / Downstream Consumer

Modules
-------
``config``              the centralised, per-request overridable risk policy
``models``              frozen engine-facing dataclasses (inputs, measurements, report)
``evaluation``          one shared definition of utilization, status and tolerance
``exposure_analyzer``   asset / sector / strategy / asset-class exposure and concentration
``covariance_analyzer`` correlation, covariance, volatility, risk contribution, clusters
``risk_metrics``        volatility, beta, drawdown, VaR, Expected Shortfall
``liquidity_analyzer``  participation ratios and liquidity score
``stress_tester``       the seven deterministic stress scenarios
``risk_controller``     violation detection, deterministic adjustment, final decision
``risk_engine``         the orchestrator that produces the canonical report

Every engine is pure — no clock, no database, no randomness — so identical inputs always produce
an identical report. The one DB-facing adapter is
:mod:`app.services.portfolio_risk.risk_service`, which reads a persisted portfolio and converts
engine records to the API schemas in :mod:`app.schemas.portfolio_risk`.

Estimation, not measurement
---------------------------
Volatility, beta, drawdown, VaR, Expected Shortfall and every stress outcome are estimates
computed from the historical sample supplied. Data that was not supplied is reported as
``DATA_INSUFFICIENT`` with a ``None`` value, never as zero. Stress scenarios are deterministic
simulations on today's holdings, not forecasts.
"""

from app.services.portfolio_risk.config import (
    PortfolioRiskConfig,
    default_portfolio_risk_config,
    portfolio_risk_config,
)
from app.services.portfolio_risk.covariance_analyzer import (
    CovarianceAnalyzer,
    covariance_summary,
    pearson_correlation,
)
from app.services.portfolio_risk.evaluation import (
    assess,
    exceeds,
    excess_pct,
    status_for,
    utilization_pct,
)
from app.services.portfolio_risk.exposure_analyzer import ExposureAnalyzer, validate_positions
from app.services.portfolio_risk.liquidity_analyzer import LiquidityAnalyzer
from app.services.portfolio_risk.models import (
    AdjustedPortfolio,
    AssetExposure,
    ConcentrationMetric,
    CorrelationCluster,
    CovarianceAnalysis,
    ExposureSummary,
    GroupExposure,
    LiquidityAnalysis,
    PortfolioExposure,
    PortfolioRiskAssessment,
    RiskAdjustment,
    RiskBudgetResult,
    RiskContribution,
    RiskMetricResult,
    RiskPosition,
    RiskSnapshot,
    RiskViolation,
    StressOutcome,
    StressScenarioSpec,
)
from app.services.portfolio_risk.risk_controller import (
    AdjustmentOutcome,
    EvaluationBundle,
    RiskAdjustmentEngine,
    RiskController,
)
from app.services.portfolio_risk.risk_engine import PortfolioRiskEngine, portfolio_risk_engine
from app.services.portfolio_risk.risk_metrics import (
    RiskMetricsEngine,
    equity_curve_from_returns,
    quantile,
    sample_std_dev,
)
from app.services.portfolio_risk.risk_service import (
    PortfolioRiskService,
    adjustment_response,
    analysis_response,
    config_from_request,
    portfolio_risk_service,
    report_to_schema,
    snapshot_from_request,
)
from app.services.portfolio_risk.stress_tester import StressTester

__all__ = [
    # Configuration
    "PortfolioRiskConfig",
    "default_portfolio_risk_config",
    "portfolio_risk_config",
    # Shared evaluation
    "assess",
    "exceeds",
    "excess_pct",
    "status_for",
    "utilization_pct",
    # Models
    "AdjustedPortfolio",
    "AssetExposure",
    "ConcentrationMetric",
    "CorrelationCluster",
    "CovarianceAnalysis",
    "ExposureSummary",
    "GroupExposure",
    "LiquidityAnalysis",
    "PortfolioExposure",
    "PortfolioRiskAssessment",
    "RiskAdjustment",
    "RiskBudgetResult",
    "RiskContribution",
    "RiskMetricResult",
    "RiskPosition",
    "RiskSnapshot",
    "RiskViolation",
    "StressOutcome",
    "StressScenarioSpec",
    "AdjustmentOutcome",
    "EvaluationBundle",
    # Engines
    "CovarianceAnalyzer",
    "covariance_summary",
    "pearson_correlation",
    "ExposureAnalyzer",
    "validate_positions",
    "LiquidityAnalyzer",
    "RiskAdjustmentEngine",
    "RiskController",
    "RiskMetricsEngine",
    "equity_curve_from_returns",
    "quantile",
    "sample_std_dev",
    "StressTester",
    "PortfolioRiskEngine",
    "portfolio_risk_engine",
    "PortfolioRiskService",
    "portfolio_risk_service",
    "adjustment_response",
    "analysis_response",
    "config_from_request",
    "report_to_schema",
    "snapshot_from_request",
]
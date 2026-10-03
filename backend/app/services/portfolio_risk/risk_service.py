"""Phase 15 — the DB-facing adapter and the conversion to the API schemas.

This is the **only** Phase 15 module that touches the database, and it contains no risk
arithmetic. Its jobs are:

1. translate an :class:`~app.schemas.portfolio_risk.PortfolioRiskConfigSchema` into a validated
   :class:`~app.services.portfolio_risk.config.PortfolioRiskConfig` — an unknown limit is an error,
   never a silently ignored field;
2. translate a request schema into the engine's :class:`RiskSnapshot`;
3. translate the engine's report into
   :class:`~app.schemas.portfolio_risk.PortfolioRiskReportSchema`;
4. optionally build a request from a **persisted Phase 9 paper portfolio**, reading its positions,
   cash, sector classification and traded volume from the existing tables.

Nothing here computes a risk figure. That split keeps the money rules unit-testable without a
session, and keeps this layer a thin, obvious translation between stored rows and engine inputs.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from app.core.exceptions import InsufficientMarketDataError, InvalidPortfolioStateError
from app.core.logging import logger
from app.repositories.price_repository import PriceRepository
from app.schemas.portfolio_risk import (
    AssetClassExposureSchema,
    AssetExposureSchema,
    ConcentrationMetricSchema,
    CorrelationClusterSchema,
    LiquidityAnalysisSchema,
    PortfolioExposureSchema,
    PortfolioRiskAdjustmentResponseSchema,
    PortfolioRiskAnalysisResponseSchema,
    PortfolioRiskConfigSchema,
    PortfolioRiskDecision,
    PortfolioRiskDecisionDetailSchema,
    PortfolioRiskReportSchema,
    PortfolioRiskRequestSchema,
    RiskAdjustedPortfolioSchema,
    RiskAdjustedPositionSchema,
    RiskAdjustmentSchema,
    RiskBudgetSchema,
    RiskContributionSchema,
    RiskMetricSchema,
    RiskStatus,
    RiskViolationSchema,
    SectorExposureSchema,
    StrategyExposureSchema,
    StressResultSchema,
    StressScenarioType,
)
from app.services.backtest.models import quantize_money, quantize_percent
from app.services.portfolio_risk.config import PortfolioRiskConfig, portfolio_risk_config
from app.services.portfolio_risk.evaluation import pct
from app.services.portfolio_risk.models import (
    AdjustedPortfolio,
    AssetExposure,
    ConcentrationMetric,
    CorrelationCluster,
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
)
from app.services.portfolio_risk.risk_engine import PortfolioRiskEngine, portfolio_risk_engine

#: One hundred — the scale of every percentage in this package.
_HUNDRED = Decimal("100")
#: Days of history requested per symbol when the caller does not name a window.
DEFAULT_LOOKBACK_DAYS = 365
#: How many recent bars are averaged into a position's average daily traded value.
VOLUME_WINDOW = 30

#: Request-config fields that map straight onto a config field of the same name.
_DIRECT_CONFIG_FIELDS = (
    "max_single_asset_weight",
    "max_sector_weight",
    "max_strategy_weight",
    "max_asset_class_weight",
    "max_top_n_concentration",
    "top_n_count",
    "max_portfolio_volatility",
    "max_portfolio_beta",
    "max_drawdown",
    "max_correlation_concentration",
    "max_stress_loss",
    "minimum_liquidity_score",
    "max_participation_ratio",
    "minimum_position_count",
    "maximum_position_count",
    "correlation_cluster_threshold",
    "var_confidence",
    "var_horizon_days",
    "benchmark_symbol",
    "warning_utilization_pct",
    "min_observations",
    "min_var_observations",
    "market_crash_shock",
    "severe_crash_shock",
    "sector_shock",
    "high_beta_shock",
    "high_beta_tilt",
    "strategy_shock",
    "liquidity_haircut",
    "correlation_spike_multiplier",
    "correlation_spike_vol_multiplier",
    "stress_sector",
    "risk_budget_asset_concentration",
    "risk_budget_sector_concentration",
    "risk_budget_strategy_concentration",
    "risk_budget_volatility",
    "risk_budget_beta",
    "risk_budget_drawdown",
    "risk_budget_var",
    "risk_budget_stress",
    "mandatory_limits",
    "disable_stress_tests",
    "enable_adjustment",
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def config_from_request(
    overrides: Optional[PortfolioRiskConfigSchema],
    base: Optional[PortfolioRiskConfig] = None,
) -> PortfolioRiskConfig:
    """Merge a per-request override over the application defaults and validate the result.

    An explicit ``null`` in the request is a real instruction — it disables that rule — and is
    applied. A field the schema does not know is rejected by the schema itself, so there is no
    path here that silently discards a limit the caller believed was in force.
    """
    policy = base or portfolio_risk_config
    if overrides is None:
        return policy

    changes: Dict[str, object] = {}
    # ``exclude_unset`` is what makes "an explicit null disables the rule" work. Dumping every
    # field would report each *omitted* limit as an explicit null and silently strip the whole
    # policy down to nothing the caller never asked to change.
    data = overrides.model_dump(exclude_unset=True)
    for field_name in _DIRECT_CONFIG_FIELDS:
        if field_name not in data:
            continue
        value = data[field_name]
        if field_name == "mandatory_limits":
            if value is not None:
                changes[field_name] = tuple(value)
            continue
        changes[field_name] = value

    if not changes:
        return policy
    return policy.replace(**changes).validate()


# ---------------------------------------------------------------------------
# Input translation
# ---------------------------------------------------------------------------


def snapshot_from_request(
    request: PortfolioRiskRequestSchema,
    config: Optional[PortfolioRiskConfig] = None,
) -> RiskSnapshot:
    """Build the engine's immutable input from a validated request."""
    return RiskSnapshot(
        portfolio_id=request.portfolio_id,
        portfolio_value=Decimal(str(request.portfolio_value)),
        cash=Decimal(str(request.cash)),
        positions=tuple(
            RiskPosition(
                symbol=position.symbol,
                quantity=position.quantity,
                price=Decimal(str(position.price)),
                strategy=position.strategy,
                sector=position.sector,
                industry=position.industry,
                asset_class=position.asset_class,
                beta=None if position.beta is None else Decimal(str(position.beta)),
                avg_daily_traded_value=(
                    None
                    if position.avg_daily_traded_value is None
                    else Decimal(str(position.avg_daily_traded_value))
                ),
                liquidity_score=(
                    None
                    if position.liquidity_score is None
                    else Decimal(str(position.liquidity_score))
                ),
            )
            for position in request.positions
        ),
        asset_returns={
            symbol: tuple(Decimal(str(value)) for value in values)
            for symbol, values in (request.asset_returns or {}).items()
        },
        portfolio_returns=tuple(
            Decimal(str(value)) for value in (request.portfolio_returns or [])
        ),
        equity_curve=tuple(Decimal(str(value)) for value in (request.equity_curve or [])),
        benchmark_symbol=request.benchmark_symbol,
        benchmark_returns=tuple(
            Decimal(str(value)) for value in (request.benchmark_returns or [])
        ),
    )


# ---------------------------------------------------------------------------
# Output translation
# ---------------------------------------------------------------------------


def report_to_schema(
    assessment: PortfolioRiskAssessment,
    timestamp: Optional[datetime] = None,
) -> PortfolioRiskReportSchema:
    """Convert the engine's assessment into the canonical API report.

    Money and percentages are already quantised inside the engine, so nothing is re-rounded here:
    re-quantising would make the response differ from the figures the report explains.
    """
    return PortfolioRiskReportSchema(
        portfolio_id=assessment.portfolio_id,
        timestamp=timestamp,
        portfolio_value=assessment.portfolio_value,
        decision=PortfolioRiskDecision(assessment.decision.value),
        decision_explanation=assessment.decision_explanation,
        exposure=_exposure_to_schema(assessment.exposure),
        concentration=[
            ConcentrationMetricSchema(
                metric=metric.metric,
                value_pct=metric.value_pct,
                limit_pct=metric.limit_pct,
                utilization_pct=metric.utilization_pct,
                status=RiskStatus(metric.status.value),
                subject=metric.subject,
                details=_jsonable(dict(metric.details)),
            )
            for metric in assessment.concentration
        ],
        risk_metrics={
            name: RiskMetricSchema(
                metric=result.metric,
                value=result.value,
                limit=result.limit,
                utilization_pct=result.utilization_pct,
                status=RiskStatus(result.status.value),
                limit_name=result.limit_name,
                details=_jsonable(dict(result.details)),
                explanation=result.explanation,
            )
            for name, result in sorted(assessment.risk_metrics.items())
        },
        risk_budgets=[
            RiskBudgetSchema(
                budget=budget.budget,
                metric=budget.metric,
                value=budget.value,
                limit=budget.limit,
                utilization_pct=budget.utilization_pct,
                remaining=budget.remaining,
                status=RiskStatus(budget.status.value),
                explanation=budget.explanation,
            )
            for budget in assessment.risk_budgets
        ],
        risk_contributions=[
            RiskContributionSchema(
                symbol=item.symbol,
                weight_pct=item.weight_pct,
                marginal_contribution=item.marginal_contribution,
                component_contribution=item.component_contribution,
                contribution_pct=item.contribution_pct,
            )
            for item in assessment.risk_contributions
        ],
        correlation_clusters=[
            CorrelationClusterSchema(
                cluster_id=cluster.cluster_id,
                symbols=list(cluster.symbols),
                average_correlation=cluster.average_correlation,
                min_pairwise_correlation=cluster.min_pairwise_correlation,
                combined_weight_pct=cluster.combined_weight_pct,
                risk_concentration_pct=cluster.risk_concentration_pct,
            )
            for cluster in assessment.correlation_clusters
        ],
        correlation=_correlation_summary(assessment),
        stress_tests=[
            StressResultSchema(
                scenario_id=outcome.scenario_id,
                name=outcome.name,
                description=outcome.description,
                scenario_type=StressScenarioType(outcome.scenario_type.value),
                status=RiskStatus(outcome.status.value),
                estimated_portfolio_return_pct=outcome.estimated_portfolio_return_pct,
                estimated_portfolio_loss_pct=outcome.estimated_portfolio_loss_pct,
                worst_position_loss_pct=outcome.worst_position_loss_pct,
                worst_position_symbol=outcome.worst_position_symbol,
                risk_limit_pct=outcome.risk_limit_pct,
                utilization_pct=outcome.utilization_pct,
                assumptions=list(outcome.assumptions),
                asset_shocks=_jsonable(dict(outcome.asset_shocks)),
                sector_shocks=_jsonable(dict(outcome.sector_shocks)),
                strategy_shocks=_jsonable(dict(outcome.strategy_shocks)),
                position_impacts=_jsonable(dict(outcome.position_impacts)),
                data_quality=_jsonable(dict(outcome.data_quality)),
                explanation=outcome.explanation,
            )
            for outcome in assessment.stress_tests
        ],
        liquidity=_liquidity_to_schema(assessment.liquidity),
        violations=[_violation_to_schema(item) for item in assessment.violations],
        remaining_violations=[
            _violation_to_schema(item) for item in assessment.remaining_violations
        ],
        adjustments=[
            RiskAdjustmentSchema(
                action=adjustment.action,
                symbol=adjustment.symbol,
                strategy=adjustment.strategy,
                quantity_sold=adjustment.quantity_sold,
                previous_quantity=adjustment.previous_quantity,
                new_quantity=adjustment.new_quantity,
                previous_weight_pct=adjustment.previous_weight_pct,
                new_weight_pct=adjustment.new_weight_pct,
                market_value_sold=adjustment.market_value_sold,
                violated_constraint=adjustment.violated_constraint,
                priority=adjustment.priority,
                expected_risk_improvement=adjustment.expected_risk_improvement,
                reason=adjustment.reason,
            )
            for adjustment in assessment.adjustments
        ],
        final_portfolio=(
            None
            if assessment.final_portfolio is None
            else _adjusted_portfolio_to_schema(assessment.final_portfolio)
        ),
        data_quality=_jsonable(dict(assessment.data_quality)),
        explanation=list(assessment.explanation),
        decision_details=[
            PortfolioRiskDecisionDetailSchema(
                code=str(detail.get("code", "DECISION_DETAIL")),
                explanation=str(detail.get("explanation", "")),
                details=_jsonable(dict(detail.get("details") or {})),
            )
            for detail in assessment.decision_details
        ],
        config_applied=_jsonable(dict(assessment.config_applied)),
    )


def analysis_response(
    assessment: PortfolioRiskAssessment,
    timestamp: Optional[datetime] = None,
) -> PortfolioRiskAnalysisResponseSchema:
    """The ``/risk/analyze`` response envelope."""
    return PortfolioRiskAnalysisResponseSchema(
        data=report_to_schema(assessment, timestamp)
    )


def adjustment_response(
    assessment: PortfolioRiskAssessment,
    original: AdjustedPortfolio,
    timestamp: Optional[datetime] = None,
) -> PortfolioRiskAdjustmentResponseSchema:
    """The ``/risk/adjust`` response: before, after, what changed and why."""
    after = assessment.final_portfolio or original
    report = report_to_schema(assessment, timestamp)
    return PortfolioRiskAdjustmentResponseSchema(
        original_portfolio=_adjusted_portfolio_to_schema(original),
        adjusted_portfolio=_adjusted_portfolio_to_schema(after),
        risk_before={
            name: metric for name, metric in report.risk_metrics.items()
        },
        risk_after={
            name: metric for name, metric in report.risk_metrics.items()
        },
        adjustments=report.adjustments,
        violations_before=report.violations,
        violations_after=report.remaining_violations,
        decision=report.decision,
        explanation=report.decision_explanation,
        data_quality=report.data_quality,
        meta={
            "adjustment_count": len(report.adjustments),
            "timestamp": timestamp.isoformat() if timestamp else None,
            "portfolio_value": str(report.portfolio_value),
            "reconciliation_error_pct": report.data_quality.get(
                "covariance_reconciliation_error_pct"
            ),
        },
    )


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------


def _adjusted_portfolio_to_schema(portfolio: AdjustedPortfolio) -> RiskAdjustedPortfolioSchema:
    return RiskAdjustedPortfolioSchema(
        portfolio_id=portfolio.portfolio_id,
        portfolio_value=portfolio.portfolio_value,
        cash=portfolio.cash,
        positions=[
            RiskAdjustedPositionSchema(
                symbol=position.symbol,
                quantity=position.quantity,
                price=position.price,
                market_value=quantize_money(position.market_value),
                weight_pct=quantize_percent(
                    position.market_value / portfolio.portfolio_value * _HUNDRED
                ),
                strategy=position.strategy,
                sector=position.sector,
            )
            for position in sorted(portfolio.positions, key=lambda item: item.symbol)
        ],
        cash_pct=quantize_percent(portfolio.cash / portfolio.portfolio_value * _HUNDRED),
        invested_pct=quantize_percent(
            sum((position.market_value for position in portfolio.positions), Decimal("0"))
            / portfolio.portfolio_value
            * _HUNDRED
        ),
        exposure=_exposure_to_schema(portfolio.exposure),
    )


def _exposure_to_schema(exposure: PortfolioExposure) -> PortfolioExposureSchema:
    return PortfolioExposureSchema(
        assets=[
            AssetExposureSchema(
                symbol=asset.symbol,
                quantity=asset.quantity,
                market_value=asset.market_value,
                weight_pct=asset.weight_pct,
                limit_pct=asset.limit_pct,
                utilization_pct=asset.utilization_pct,
                status=RiskStatus(asset.status.value),
                strategies=list(asset.strategies),
                sector=asset.sector,
                industry=asset.industry,
                asset_class=asset.asset_class,
                beta=asset.beta,
                avg_daily_traded_value=asset.avg_daily_traded_value,
                participation_ratio=asset.participation_ratio,
                liquidity_score=asset.liquidity_score,
            )
            for asset in exposure.assets
        ],
        strategies=[
            _group_to_schema(group, StrategyExposureSchema) for group in exposure.strategies
        ],
        sectors=[_group_to_schema(group, SectorExposureSchema) for group in exposure.sectors],
        asset_classes=[
            _group_to_schema(group, AssetClassExposureSchema) for group in exposure.asset_classes
        ],
        summary={
            "total_positions": exposure.summary.total_positions,
            "total_lots": exposure.summary.total_lots,
            "total_market_value": exposure.summary.total_market_value,
            "cash": exposure.summary.cash,
            "cash_pct": exposure.summary.cash_pct,
            "invested_pct": exposure.summary.invested_pct,
            "largest_position_pct": exposure.summary.largest_position_pct,
            "largest_position_symbol": exposure.summary.largest_position_symbol,
            "top_3_concentration_pct": exposure.summary.top_3_concentration_pct,
            "top_5_concentration_pct": exposure.summary.top_5_concentration_pct,
            "top_10_concentration_pct": exposure.summary.top_10_concentration_pct,
            "top_n_concentration_pct": exposure.summary.top_n_concentration_pct,
            "top_n_count": exposure.summary.top_n_count,
            "unclassified_sector_pct": exposure.summary.unclassified_sector_pct,
        },
    )


def _group_to_schema(group: GroupExposure, model):
    """Map one aggregated group onto its schema, ignoring fields the model does not declare."""
    fields = {
        "name": group.name,
        "weight_pct": group.weight_pct,
        "limit_pct": group.limit_pct,
        "utilization_pct": group.utilization_pct,
        "status": RiskStatus(group.status.value),
        "market_value": group.market_value,
        "symbols": list(group.symbols),
    }
    if model is StrategyExposureSchema:
        fields["strategy"] = group.name
        fields.pop("name")
        fields["position_count"] = group.position_count
    elif model is SectorExposureSchema:
        fields["sector"] = group.name
        fields.pop("name")
        fields["position_count"] = group.position_count
        fields["limit_applied"] = group.limit_applied
    elif model is AssetClassExposureSchema:
        fields["asset_class"] = group.name
        fields.pop("name")
    return model(**fields)


def _liquidity_to_schema(liquidity: LiquidityAnalysis) -> LiquidityAnalysisSchema:
    return LiquidityAnalysisSchema(
        status=RiskStatus(liquidity.status.value),
        positions_with_data=liquidity.positions_with_data,
        positions_analysed=liquidity.positions_analysed,
        overall_score=liquidity.overall_score,
        limit_pct=liquidity.limit_pct,
        utilization_pct=liquidity.utilization_pct,
        max_participation_ratio=liquidity.max_participation_ratio,
        max_participation_limit=liquidity.max_participation_limit,
        worst_participation_symbol=liquidity.worst_participation_symbol,
        estimated_liquidation_days=liquidity.estimated_liquidation_days,
        flagged_positions=[_jsonable(dict(item)) for item in liquidity.flagged_positions],
        explanation=liquidity.explanation,
    )


def _violation_to_schema(violation: RiskViolation) -> RiskViolationSchema:
    return RiskViolationSchema(
        rule=violation.rule,
        limit_name=violation.limit_name,
        subject=violation.subject,
        limit=violation.limit,
        observed=violation.observed,
        utilization_pct=violation.utilization_pct,
        excess_pct=violation.excess_pct,
        is_mandatory=violation.is_mandatory,
        status=RiskStatus(violation.status.value),
        explanation=violation.explanation,
        details=_jsonable(dict(violation.details)),
    )


def _correlation_summary(assessment: PortfolioRiskAssessment) -> Dict[str, object]:
    """The covariance picture, flattened for the API: symbols, coverage and the matrix."""
    covariance = assessment.covariance
    return _jsonable(
        {
            "data_quality": covariance.data_quality,
            "observation_count": covariance.observation_count,
            "symbols": list(covariance.symbols),
            "excluded_symbols": list(covariance.excluded_symbols),
            "unavailable_pairs": [list(pair) for pair in covariance.unavailable_pairs],
            "weight_coverage_pct": covariance.weight_coverage_pct,
            "daily_volatility_pct": covariance.daily_volatility_pct,
            "annualised_volatility_pct": covariance.annualised_volatility_pct,
            "correlation_concentration": covariance.correlation_concentration,
            "weighted_average_correlation": covariance.weighted_average_correlation,
            "reconciliation_error_pct": covariance.reconciliation_error_pct,
            "benchmark": covariance.benchmark,
            "beta": covariance.beta,
            "individual_betas": dict(covariance.individual_betas),
            "matrix": [
                {"symbol_a": left, "symbol_b": right, "correlation": value}
                for (left, right), value in sorted(covariance.correlation.items())
                if left != right
            ],
        }
    )


def _jsonable(payload: Dict[str, object]) -> Dict[str, object]:
    """Make engine details JSON-safe: Decimals to strings, enums to values, tuples to lists."""
    result: Dict[str, object] = {}
    for key, value in payload.items():
        result[str(key)] = _jsonable_value(value)
    return result


def _jsonable_value(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable_value(item) for item in value]
    if hasattr(value, "value"):
        return value.value
    return value


# ---------------------------------------------------------------------------
# DB-facing adapter
# ---------------------------------------------------------------------------


class PortfolioRiskService:
    """Phase 15 orchestration over the pure engine, with an optional database adapter.

    Two shapes of request are supported: an explicitly supplied portfolio (the normal case — the
    output of Phase 14 is turned into a request body), and a **persisted Phase 9 paper portfolio**,
    whose positions, cash, sectors and traded volume are read from the existing tables.

    A persisted portfolio has one position per instrument, so the strategy label comes from the
    most recent buy order's source. That is the only attribution the stored schema supports; it is
    used because it exists, and reported as ``MANUAL`` when there is no such order rather than
    invented.
    """

    def __init__(
        self,
        engine: Optional[PortfolioRiskEngine] = None,
        price_repo: Optional[PriceRepository] = None,
    ):
        self.engine = engine or portfolio_risk_engine
        self.price_repo = price_repo or PriceRepository()

    # -- supplied portfolio ------------------------------------------------

    def analyze(
        self,
        request: PortfolioRiskRequestSchema,
        timestamp: Optional[datetime] = None,
    ) -> PortfolioRiskAssessment:
        """Evaluate a submitted portfolio."""
        config = config_from_request(request.risk_config)
        snapshot = snapshot_from_request(request, config)
        assessment = self.engine_for(config).analyze(snapshot)
        logger.info(
            "PortfolioRiskService: analyzed portfolio_id=%s decision=%s violations=%d "
            "adjustments=%d",
            request.portfolio_id,
            assessment.decision.value,
            len(assessment.violations),
            len(assessment.adjustments),
        )
        return assessment

    # -- persisted portfolio -----------------------------------------------

    def analyze_persisted(
        self,
        db: Session,
        portfolio,
        overrides: Optional[PortfolioRiskConfigSchema] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        timestamp: Optional[datetime] = None,
    ) -> PortfolioRiskAssessment:
        """Build a request from a stored paper portfolio and analyze it."""
        request = self.build_request_from_portfolio(
            db,
            portfolio,
            overrides=overrides,
            start_date=start_date,
            end_date=end_date,
        )
        return self.analyze(request, timestamp)

    def build_request_from_portfolio(
        self,
        db: Session,
        portfolio,
        overrides: Optional[PortfolioRiskConfigSchema] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> PortfolioRiskRequestSchema:
        """Read a stored portfolio into a Phase 15 request.

        Only stored data is used: current prices come from the position rows when the paper engine
        has marked them, otherwise from the latest price; traded value is the mean of the most
        recent ``VOLUME_WINDOW`` bars' close-times-volume; sector comes from the company's profile.
        Nothing is defaulted to a plausible-looking value — a position with no volume simply has no
        ``avg_daily_traded_value``, and the report says liquidity data is unavailable.
        """
        from app.services.portfolio.service import paper_trading_service

        positions = paper_trading_service.get_positions(db, portfolio.id)
        if not positions:
            raise InvalidPortfolioStateError(
                f"Portfolio {portfolio.id} holds no open positions, so there is nothing to "
                "measure. A portfolio-level risk report needs at least one holding."
            )

        symbols = {position.stock_id for position in positions}
        symbols_by_id = _stock_symbols(db, symbols)
        sectors = _sector_map(db, symbols)
        traded_value = self._traded_value_map(db, symbols, end_date)
        returns = self._return_map(db, symbols, start_date, end_date)
        sources = self._order_source_map(db, portfolio.id, symbols)

        entries: List[dict] = []
        market_value = Decimal("0")
        for position in positions:
            symbol = symbols_by_id.get(position.stock_id) or f"stock_{position.stock_id}"
            price = _resolve_price(position)
            if price is None:
                raise InsufficientMarketDataError(
                    f"Position {symbol} in portfolio {portfolio.id} has neither a marked current "
                    "price nor a stored close, so it cannot be valued. No price is assumed."
                )
            market_value += Decimal(position.quantity) * Decimal(str(price))
            entries.append(
                {
                    "symbol": symbol,
                    "quantity": int(position.quantity),
                    "price": Decimal(str(price)),
                    "strategy": sources.get(position.stock_id, "MANUAL"),
                    "sector": sectors.get(position.stock_id),
                    "avg_daily_traded_value": traded_value.get(position.stock_id),
                }
            )

        cash = Decimal(str(portfolio.current_cash))
        return PortfolioRiskRequestSchema(
            portfolio_id=portfolio.id,
            portfolio_value=quantize_money(market_value + cash),
            cash=quantize_money(cash),
            positions=entries,
            asset_returns=returns,
            risk_config=overrides,
        )

    # -- internals ---------------------------------------------------------

    def engine_for(self, config: PortfolioRiskConfig) -> PortfolioRiskEngine:
        """The default engine when the policy is the shipped one, otherwise a bound engine.

        Reusing the singleton for the default policy keeps the common case free of construction
        cost; a customised policy gets its own engine so the two can never share mutable state.
        """
        if config == portfolio_risk_config:
            return self.engine
        return PortfolioRiskEngine(config)

    def _order_source_map(
        self,
        db: Session,
        portfolio_id: int,
        stock_ids: Sequence[int],
    ) -> Dict[int, str]:
        """``{stock_id: strategy label}`` for the orders that opened the stored positions.

        Stored orders record the strategy that placed them only as free-form metadata. When a
        producer wrote a strategy name into ``metadata_json`` it is used; otherwise a lot placed
        by a strategy event is labelled ``STRATEGY`` and a manual one ``MANUAL``. Naming a
        specific strategy that was never stored would be inventing the grouping the report is
        supposed to measure, and ``MANUAL`` for everything would understate how much of the book
        is strategy-driven, so the two cases are kept apart.
        """
        from app.models.portfolio import PortfolioOrder

        wanted = set(stock_ids)
        orders = (
            db.query(PortfolioOrder)
            .filter(PortfolioOrder.portfolio_id == portfolio_id)
            .filter(PortfolioOrder.stock_id.in_(list(wanted)))
            .order_by(PortfolioOrder.id)
            .all()
        )

        result: Dict[int, str] = {}
        for order in orders:
            label = None
            if order.metadata_json:
                try:
                    stored = json.loads(order.metadata_json)
                except (TypeError, ValueError):
                    stored = {}
                if isinstance(stored, dict):
                    for key in ("strategy", "strategy_name", "source"):
                        value = stored.get(key)
                        if isinstance(value, str) and value.strip():
                            label = value.strip()
                            break
            if label is None:
                label = "STRATEGY" if order.strategy_event_id else "MANUAL"
            result[order.stock_id] = label
        return result

    def _traded_value_map(
        self,
        db: Session,
        stock_ids: Sequence[int],
        end_date: Optional[date],
    ) -> Dict[int, Decimal]:
        """Average daily traded value per stock: mean(close x volume) over recent bars."""
        result: Dict[int, Decimal] = {}
        for stock_id in sorted(stock_ids):
            rows = self.price_repo.get_history_asc(
                db,
                stock_id,
                end_date=end_date,
                limit=VOLUME_WINDOW,
            )
            values = [
                Decimal(str(row.close_price)) * Decimal(int(row.volume or 0))
                for row in rows
                if row.close_price is not None and row.volume
            ]
            if values:
                result[stock_id] = quantize_money(sum(values, Decimal("0")) / Decimal(len(values)))
        return result

    def _return_map(
        self,
        db: Session,
        stock_ids: Sequence[int],
        start_date: Optional[date],
        end_date: Optional[date],
    ) -> Dict[str, List[Decimal]]:
        """Daily return fractions per symbol, from stored closes.

        Returns are only produced from two consecutive bars with usable closes; a gap produces no
        observation rather than a zero return, because a zero would say "the price did not move"
        about a day the database simply does not have.
        """
        window_start = start_date or (end_date or date.today()) - timedelta(days=DEFAULT_LOOKBACK_DAYS)
        result: Dict[str, List[Decimal]] = {}
        symbols_by_id = _stock_symbols(db, stock_ids)

        for stock_id in sorted(stock_ids):
            rows = self.price_repo.get_history_asc(
                db, stock_id, start_date=window_start, end_date=end_date
            )
            closes = [Decimal(str(row.close_price)) for row in rows if row.close_price]
            returns: List[Decimal] = []
            for previous, current in zip(closes, closes[1:]):
                if previous > 0:
                    returns.append((current - previous) / previous)
            if returns:
                result[symbols_by_id.get(stock_id, str(stock_id))] = returns
        return result


# ---------------------------------------------------------------------------
# Small DB helpers
# ---------------------------------------------------------------------------


def _stock_symbols(db: Session, stock_ids: Sequence[int]) -> Dict[int, str]:
    """``{stock_id: symbol}`` for the requested stocks."""
    from app.models.stock import Stock

    return {
        stock.id: stock.symbol
        for stock in db.query(Stock).filter(Stock.id.in_(list(stock_ids))).all()
    }


def _sector_map(db: Session, stock_ids: Sequence[int]) -> Dict[int, Optional[str]]:
    """``{stock_id: sector}`` from the company profile; absent when no profile exists."""
    from app.models.company import Company

    return {
        company.stock_id: company.sector
        for company in db.query(Company).filter(Company.stock_id.in_(list(stock_ids))).all()
    }


def _resolve_price(position) -> Optional[Decimal]:
    """The price to value a stored position at: its marked price, else its entry price."""
    if position.current_price is not None and Decimal(str(position.current_price)) > 0:
        return Decimal(str(position.current_price))
    if (
        position.market_value is not None
        and Decimal(str(position.market_value)) > 0
        and position.quantity
    ):
        return Decimal(str(position.market_value)) / Decimal(int(position.quantity))
    if position.average_entry_price is not None:
        return Decimal(str(position.average_entry_price))
    return None


def _order_source_map(
    db: Session,
    portfolio_id: int,
    stock_ids: Sequence[int],
) -> Dict[int, str]:
    """``{stock_id: order source}`` from the most recent executed buy order per stock.

    The stored schema keeps one position per instrument and no strategy column on it, so the most
    recent buy's source is the only attribution available. It is reported as stored, and a stock
    with no buy order falls back to ``MANUAL`` rather than to a guess.
    """
    from app.models.portfolio import PortfolioOrder

    orders = (
        db.query(PortfolioOrder)
        .filter(
            PortfolioOrder.portfolio_id == portfolio_id,
            PortfolioOrder.stock_id.in_(list(stock_ids)),
            PortfolioOrder.status == "FILLED",
            PortfolioOrder.side == "BUY",
        )
        .order_by(PortfolioOrder.executed_at.asc(), PortfolioOrder.id.asc())
        .all()
    )
    result: Dict[int, str] = {}
    for order in orders:
        result[order.stock_id] = order.source or "MANUAL"
    return result


#: Module-level service singleton, matching the pattern of every other service in this project.
portfolio_risk_service = PortfolioRiskService()


__all__ = [
    "PortfolioRiskService",
    "portfolio_risk_service",
    "analysis_response",
    "adjustment_response",
    "config_from_request",
    "report_to_schema",
    "snapshot_from_request",
]
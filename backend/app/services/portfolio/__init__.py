"""Phase 10/11 — reusable portfolio, risk-management, construction and paper-trading engine.

**Simulation only.** Every portfolio, order and trade in this package is virtual. Nothing
here connects to a brokerage, stores credentials, or sends an order to an exchange.

Flow::

    Market Data
      -> Analytics (Phase 6)
      -> SignalEngine (Phase 7)  /  Screener (Phase 7)      [which stocks qualify]
      -> Phase 8 strategy rules
      -> OrderIntent
      -> RiskManager (limits) + OrderExecutor (validation, fill, P&L)
      -> position / cash / P&L update (one transaction)
      -> PortfolioValuationService -> PortfolioPerformanceService -> equity curve

Phase 11 adds the portfolio *decision* layer above that, which answers "how much should I hold
in each of these" rather than "does this order pass the pre-trade limits"::

    Screener / explicit candidates                [Phase 7, reused unchanged]
      -> Candidate resolution (price, sector)
      -> allocation.py  (target weights, stops, drift)
      -> risk_engine.py (portfolio-level risk report + recommendations)
      -> construction.py (explainable BUY / SELL / HOLD / REDUCE decisions)
      -> rebalancing.py  (target-weight trades, threshold-suppressed)
      -> PaperTradingService (simulated application: orders, cash, ledger, snapshots)

Layer boundaries
----------------
Routers never calculate; services hold the business rules; repositories only persist; and
every monetary figure is a :class:`decimal.Decimal` rounded once through the shared Phase 8
quantisation helpers. The money and decision rules themselves live in pure modules
(:mod:`~app.services.portfolio.calculator`, :mod:`~app.services.portfolio.allocation`,
:mod:`~app.services.portfolio.construction`, :mod:`~app.services.portfolio.rebalancing`,
:mod:`~app.services.portfolio.risk`, :mod:`~app.services.portfolio.risk_engine`,
:mod:`~app.services.portfolio.execution`) so each is unit-testable without FastAPI or a
database.

Modules
-------
``calculator``             pure money math: P&L, equity, exposure, cash movements
``allocation``             pure allocation math: target weights, stops, drift, risk budget
``models``                 engine-facing dataclasses and configuration
``defaults``               centralised defaults from application settings
``risk``                   ``RiskManager``: the deterministic pre-trade limit rules
``risk_service``           pre-trade risk checks and effective-limit reporting
``risk_engine``            portfolio-level risk report and risk-driven recommendations
``position_sizing_service`` fixed-capital / percentage / risk / equal-weight / cap sizing
``execution``              execution-price policy (OPEN or CLOSE) and the look-ahead guard
``executor``               ``OrderExecutor``: validation order, fills, rejections
``valuation_service``      mark-to-market view: cash, market value, equity, exposure
``performance_service``    activity, win rate, profit factor, drawdown, volatility, Sharpe
``strategy_bridge``        Phase 7 signals + Phase 8 rules -> paper order intent
``construction``           pure explainable portfolio decisions and target weights
``construction_service``   state resolution, screener integration, decision application
``rebalancing``            pure rebalancing plans against target weights
``paper_engine``           sequential, look-ahead-free replay of stored history
``service``                ``PaperTradingService``: orchestration, transactions, persistence

Phase 13 adds the reusable **portfolio & risk management engine** above all of that. It answers
*how much capital is available, how big should each position be, what happens when several
signals arrive at once, and is the portfolio currently outside its limits* — as pure
calculations over one policy object::

    PortfolioRiskPolicy (one object: every limit + the cost model)
      -> sizing.py            risk budget vs position weight vs cash  -> whole shares
      -> allocation_engine.py rank, allocate sequentially, re-check limits -> decisions
      -> risk_state.py        exposure, cash weight, weights, drawdown -> risk flags
      -> portfolio_backtest.py several stocks, one portfolio, chronological -> metrics
      -> allocation_service.py the only Phase 13 code that reads the database

Every one of those engines is pure, so the money rules are unit-testable without FastAPI or a
fixture, and ``allocation_service`` is a thin translation between stored rows and engine inputs.
"""

from app.services.portfolio.allocation import (
    allocate_equal_weights,
    budget_for_weight,
    capped_target_pct,
    equal_weight_target_pct,
    gain_pct,
    loss_pct,
    normalise_weights,
    position_loss_breached,
    risk_amount,
    risk_based_quantity,
    risk_budget,
    stop_loss_breached,
    stop_price,
    take_profit_price,
    take_profit_triggered,
    target_quantity_for_weight,
    weight_difference_pct,
)
from app.services.portfolio.allocation_engine import (
    ALLOCATION_METHODS,
    STOP_CAPITAL_EXHAUSTED,
    STOP_EXPOSURE_REACHED,
    STOP_POSITION_LIMIT_REACHED,
    PortfolioAllocationEngine,
)
from app.services.portfolio.allocation_service import (
    PortfolioAllocationService,
    portfolio_allocation_service,
)
from app.services.portfolio.calculator import (
    allocated_entry_fees,
    allocation_pct,
    apply_buy,
    apply_deposit,
    apply_sell,
    apply_slippage,
    apply_withdrawal,
    calculate_fee,
    cost_basis,
    drawdown_pct,
    equity,
    exposure_pct,
    gross_value,
    invested_capital,
    market_value,
    quantize_price,
    return_pct,
    risk_per_share,
    signed_drawdown_pct,
    size_quantity_by_percentage,
    size_quantity_by_risk,
    size_quantity_fixed_capital,
    size_quantity_from_cash,
    total_pnl,
    unrealized_pnl,
    value_position,
    weighted_average_entry_price,
)
from app.services.portfolio.construction import (
    build_decisions,
    build_target_weights,
    effective_position_count,
)
from app.services.portfolio.construction_service import (
    PortfolioConstructionService,
    portfolio_construction_service,
)
from app.services.portfolio.defaults import (
    default_allocation_config,
    default_portfolio_risk_policy,
    default_position_size_pct,
    default_risk_limits,
    default_trading_cost_config,
)
from app.services.portfolio.execution import (
    ExecutionReference,
    default_policy,
    reference_price,
    resolve_execution_price,
)
from app.services.portfolio.executor import OrderExecutor
from app.services.portfolio.models import (
    CONSTRAINT_CASH,
    CONSTRAINT_POSITION_WEIGHT,
    CONSTRAINT_RISK,
    CONSTRAINT_UNAFFORDABLE,
    AllocationCandidate,
    AllocationConfig,
    AllocationDecision,
    AllocationResult,
    Candidate,
    CashMovement,
    DrawdownPoint,
    ExecutionPlan,
    OrderIntent,
    PaperRunOrder,
    PaperRunResult,
    PortfolioAccount,
    PortfolioDecision,
    PortfolioDecisions,
    PortfolioPerformance,
    PortfolioRiskPolicy,
    PortfolioRiskReport,
    PortfolioRiskState,
    PortfolioSnapshotValue,
    PortfolioValuation,
    PositionExposure,
    PositionRisk,
    PositionSizePlan,
    PositionState,
    PositionValuation,
    RebalanceItem,
    RebalancePlan,
    Rejection,
    RiskAssessment,
    RiskFlagDetail,
    RiskLimits,
    RiskRecommendation,
    RiskViolation,
    SizingBreakdown,
    StrategyOrderPlan,
    TradingCostConfig,
    allocation_config_from_dict,
    allocation_config_to_dict,
    as_allocation_config,
    as_risk_policy,
    policy_from_allocation,
)
from app.services.portfolio.paper_engine import PaperTradingEngine, paper_trading_engine
from app.services.portfolio.performance_service import (
    PortfolioPerformanceService,
    cagr_pct,
    downside_deviation,
    profit_factor,
    sortino_ratio,
    win_rate_pct,
)
from app.services.portfolio.portfolio_backtest import (
    BacktestStock,
    PortfolioBacktestEngine,
    PortfolioBacktestResult,
    PortfolioEquityPoint,
)
from app.services.portfolio.position_sizing_service import (
    PositionSizingService,
    position_sizing_service,
)
from app.services.portfolio.rebalancing import (
    derive_target_weights,
    plan_rebalance,
    side_for,
)
from app.services.portfolio.risk import RiskManager, insufficient_cash_rejection, peak_equity
from app.services.portfolio.risk_engine import PortfolioRiskEngine
from app.services.portfolio.risk_service import RiskCheckService, risk_check_service
from app.services.portfolio.risk_state import (
    PortfolioRiskStateEngine,
    portfolio_risk_state_engine,
    portfolio_return_pct,
    worst_drawdown,
)
from app.services.portfolio.service import (
    OrderExecutionResult,
    PaperTradingService,
    StrategyExecutionResult,
    build_account,
    paper_trading_service,
)
from app.services.portfolio.sizing import (
    METHOD_MAX_POSITION_CAP,
    METHOD_RISK_AND_WEIGHT,
    PositionSizingEngine,
    max_position_value_of,
    position_sizing_engine,
    quantity_by_capital,
    quantity_by_risk,
    risk_budget_of,
    risk_per_share_long,
)
from app.services.portfolio.strategy_bridge import PaperStrategyBridge, build_strategy_event_id
from app.services.portfolio.optimization import (
    AllocationMethod,
    CorrelationMatrix,
    CorrelationStatus,
    OptimizationRequest,
    OptimizationResult,
    StrategyAssignment,
    StrategyEligibility,
    StrategyOptimizer,
    StrategyPerformance,
    compute_correlation_matrix,
    evaluate_eligibility,
    pearson_correlation,
    raw_weights_for,
    solve_constraints,
)
from app.services.portfolio.optimization_service import (
    StrategyOptimizationService,
    strategy_optimization_service,
)
from app.services.portfolio.valuation_service import PortfolioValuationService

__all__ = [
    "OrderExecutor",
    "RiskManager",
    "RiskCheckService",
    "risk_check_service",
    "PortfolioRiskEngine",
    "PortfolioRiskStateEngine",
    "portfolio_risk_state_engine",
    "PortfolioAllocationEngine",
    "PortfolioAllocationService",
    "portfolio_allocation_service",
    "PortfolioBacktestEngine",
    "PortfolioBacktestResult",
    "PortfolioEquityPoint",
    "BacktestStock",
    "PositionSizingEngine",
    "position_sizing_engine",
    "PortfolioConstructionService",
    "portfolio_construction_service",
    "PositionSizingService",
    "position_sizing_service",
    "PortfolioValuationService",
    "PortfolioPerformanceService",
    "PaperStrategyBridge",
    "PaperTradingEngine",
    "paper_trading_engine",
    "PaperTradingService",
    "paper_trading_service",
    "OrderExecutionResult",
    "StrategyExecutionResult",
    "build_account",
    "AllocationConfig",
    "PortfolioRiskPolicy",
    "PortfolioRiskState",
    "PositionExposure",
    "RiskFlagDetail",
    "SizingBreakdown",
    "AllocationCandidate",
    "AllocationDecision",
    "AllocationResult",
    "CONSTRAINT_RISK",
    "CONSTRAINT_POSITION_WEIGHT",
    "CONSTRAINT_CASH",
    "CONSTRAINT_UNAFFORDABLE",
    "ALLOCATION_METHODS",
    "STOP_CAPITAL_EXHAUSTED",
    "STOP_EXPOSURE_REACHED",
    "STOP_POSITION_LIMIT_REACHED",
    "METHOD_RISK_AND_WEIGHT",
    "METHOD_MAX_POSITION_CAP",
    "risk_budget_of",
    "risk_per_share_long",
    "max_position_value_of",
    "quantity_by_risk",
    "quantity_by_capital",
    "worst_drawdown",
    "portfolio_return_pct",
    "as_risk_policy",
    "policy_from_allocation",
    "default_portfolio_risk_policy",
    "TradingCostConfig",
    "RiskLimits",
    "RiskAssessment",
    "RiskViolation",
    "RiskRecommendation",
    "OrderIntent",
    "ExecutionPlan",
    "ExecutionReference",
    "Rejection",
    "PortfolioAccount",
    "PositionState",
    "PositionValuation",
    "PositionRisk",
    "PositionSizePlan",
    "PortfolioValuation",
    "PortfolioRiskReport",
    "PortfolioSnapshotValue",
    "PortfolioPerformance",
    "PaperRunOrder",
    "PaperRunResult",
    "DrawdownPoint",
    "StrategyOrderPlan",
    "Candidate",
    "CashMovement",
    "PortfolioDecision",
    "PortfolioDecisions",
    "RebalanceItem",
    "RebalancePlan",
    "build_strategy_event_id",
    "build_decisions",
    "build_target_weights",
    "effective_position_count",
    "plan_rebalance",
    "derive_target_weights",
    "side_for",
    "insufficient_cash_rejection",
    "peak_equity",
    "as_allocation_config",
    "allocation_config_to_dict",
    "allocation_config_from_dict",
    "default_trading_cost_config",
    "default_risk_limits",
    "default_allocation_config",
    "default_position_size_pct",
    "default_policy",
    "resolve_execution_price",
    "reference_price",
    "calculate_fee",
    "apply_slippage",
    "apply_buy",
    "apply_sell",
    "apply_deposit",
    "apply_withdrawal",
    "allocated_entry_fees",
    "weighted_average_entry_price",
    "gross_value",
    "market_value",
    "cost_basis",
    "unrealized_pnl",
    "equity",
    "total_pnl",
    "return_pct",
    "allocation_pct",
    "exposure_pct",
    "invested_capital",
    "drawdown_pct",
    "signed_drawdown_pct",
    "risk_per_share",
    "size_quantity_fixed_capital",
    "size_quantity_by_percentage",
    "size_quantity_by_risk",
    "size_quantity_from_cash",
    "win_rate_pct",
    "profit_factor",
    "sortino_ratio",
    "downside_deviation",
    "cagr_pct",
    "quantize_price",
    "value_position",
    "equal_weight_target_pct",
    "capped_target_pct",
    "allocate_equal_weights",
    "normalise_weights",
    "budget_for_weight",
    "risk_budget",
    "risk_amount",
    "risk_based_quantity",
    "target_quantity_for_weight",
    "weight_difference_pct",
    "stop_price",
    "take_profit_price",
    "gain_pct",
    "loss_pct",
    "stop_loss_breached",
    "take_profit_triggered",
    "position_loss_breached",
    # Phase 14: strategy capital allocation
    "AllocationMethod",
    "CorrelationMatrix",
    "CorrelationStatus",
    "OptimizationRequest",
    "OptimizationResult",
    "StrategyAssignment",
    "StrategyEligibility",
    "StrategyOptimizer",
    "StrategyPerformance",
    "compute_correlation_matrix",
    "evaluate_eligibility",
    "pearson_correlation",
    "raw_weights_for",
    "solve_constraints",
    "StrategyOptimizationService",
    "strategy_optimization_service",
]
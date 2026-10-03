"""Phase 9–13 API endpoints for virtual paper portfolios, simulated orders, construction and risk.

These endpoints never place a real order. Each one delegates to
:class:`~app.services.portfolio.service.PaperTradingService` (for state, orders and the cash
ledger), :class:`~app.services.portfolio.construction_service.PortfolioConstructionService`
(for allocation decisions and rebalancing), or
:class:`~app.services.portfolio.allocation_service.PortfolioAllocationService` (for the Phase 13
sizing, allocation, risk-state and portfolio-backtest workflow); the routers themselves contain no
business logic and no SQL.
"""

import json
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, Path, Query, status
from sqlalchemy.orm import Session

from app.api.dependencies import (
    get_db,
    get_paper_trading_engine,
    get_paper_trading_service,
    get_portfolio_allocation_service,
    get_portfolio_construction_service,
    get_portfolio_risk_service,
    get_strategy_optimization_service,
)
from app.core.exceptions import InvalidOrderError
from app.schemas.allocation import (
    CashMovementRequest,
    CashMovementResponse,
    PortfolioDecisionRequest,
    PortfolioDecisionResponse,
    PortfolioDecisionsResponse,
    PortfolioTransactionResponse,
    PortfolioTransactionsResponse,
    PortfolioTransactionTypeResponse,
    RebalanceItemResponse,
    RebalanceRequest,
    RebalanceResponse,
)
from app.schemas.portfolio import (
    DecisionAction,
    DrawdownPointResponse,
    OrderCreate,
    OrderExecutionResponse,
    OrderResponse,
    OrderSide,
    OrderStatus,
    PaperRunRequest,
    PaperRunOrderResponse,
    PaperRunResponse,
    PortfolioAllocationConfig,
    PortfolioCreate,
    PortfolioDrawdownResponse,
    PortfolioErrorResponse,
    PortfolioListResponse,
    PortfolioOrdersResponse,
    PortfolioPerformanceResponse,
    PortfolioPositionsResponse,
    PortfolioPriceInput,
    PortfolioResponse,
    PortfolioRiskResponse,
    PortfolioSnapshotCreate,
    PortfolioSnapshotResponse,
    PortfolioSnapshotsResponse,
    PortfolioStatus,
    PortfolioStatusUpdate,
    PortfolioSummary,
    PortfolioTradesResponse,
    PortfolioUpdate,
    PortfolioValuationRequest,
    PortfolioValuationResponse,
    PositionResponse,
    PositionRiskResponse,
    PositionSizingRequest,
    PositionSizingResponse,
    RejectionCode,
    RiskCheckRequest,
    RiskCheckResponse,
    RiskLimitSettings,
    RiskRecommendationResponse,
    RiskSeverity,
    RiskViolationResponse,
    StrategyOrderRequest,
    StrategyOrderResponse,
    TradeResponse,
    TradingCostSettings,
    TransactionType,
)
from app.schemas.risk import (
    AllocateRequest,
    AllocationDecisionResponse,
    AllocationRejectionResponse,
    AllocationResponse,
    BacktestEquityPointResponse,
    BacktestTradeResponse,
    BindingConstraint,
    PortfolioBacktestRequest,
    PortfolioBacktestResponse,
    PortfolioRiskPolicySchema,
    PortfolioRiskStateResponse,
    PositionExposureResponse,
    PositionSizeRequest,
    PositionSizeResponse,
    RiskFlagDetailResponse,
    SectorWeightResponse,
)
from app.schemas.portfolio_risk import (
    PortfolioRiskAdjustmentResponseSchema,
    PortfolioRiskAnalysisResponseSchema,
    PortfolioRiskOverridesSchema,
    PortfolioRiskRequestSchema,
)
from app.schemas.optimization import (
    AllocationMethod,
    OptimizeRequest,
    OptimizationResultSchema,
    StrategyAssignmentSchema,
    StrategyEligibility,
)
from app.services.backtest.models import quantize_money, quantize_percent
from app.services.portfolio.calculator import allocation_pct, quantize_price, signed_drawdown_pct
from app.services.portfolio.construction_service import PortfolioConstructionService
from app.services.portfolio.allocation_service import PortfolioAllocationService
from app.services.portfolio.execution import default_policy
from app.services.portfolio.models import (
    AllocationResult,
    PortfolioDecisions,
    PortfolioPerformance,
    PortfolioRiskPolicy,
    PortfolioRiskReport,
    PortfolioRiskState,
    PortfolioValuation,
    RebalancePlan,
    SizingBreakdown,
    policy_from_allocation,
)
from app.services.portfolio.optimization_service import StrategyOptimizationService
from app.services.portfolio_risk.exposure_analyzer import ExposureAnalyzer
from app.services.portfolio_risk.models import AdjustedPortfolio
from app.services.portfolio_risk.risk_service import (
    PortfolioRiskService,
    analysis_response,
    adjustment_response,
    snapshot_from_request,
)
from app.services.portfolio.paper_engine import PaperTradingEngine
from app.services.portfolio.service import (
    MAX_PAGE_LIMIT,
    OrderExecutionResult,
    PaperTradingService,
    StrategyExecutionResult,
    utcnow,
)

router = APIRouter(prefix="/portfolios", tags=["Paper Trading"])

_DISCLAIMER = (
    "Paper trading simulation only. No brokerage is contacted and no real order is ever "
    "placed."
)

_ERROR_RESPONSES = {
    404: {"model": PortfolioErrorResponse, "description": "Portfolio, stock or order not found"},
    409: {
        "model": PortfolioErrorResponse,
        "description": "Order or cash movement refused: rejected, duplicate, or unfunded",
    },
    422: {"model": PortfolioErrorResponse, "description": "Invalid request"},
}


# ----------------------------------------------------------------------
# Response builders (presentation only — no business logic)
# ----------------------------------------------------------------------


def _stock_symbols(db: Session, stock_ids) -> dict:
    """Map stock ids to symbols in a single lookup."""
    from app.models.stock import Stock

    unique_ids = {stock_id for stock_id in stock_ids if stock_id is not None}
    if not unique_ids:
        return {}
    rows = db.query(Stock.id, Stock.symbol).filter(Stock.id.in_(unique_ids)).all()
    return {row.id: row.symbol for row in rows}


def _position_response(db: Session, position, valuation: PortfolioValuation) -> PositionResponse:
    """Map one persisted position onto its response, with its share of portfolio equity."""
    symbols = _stock_symbols(db, [position.stock_id])
    return PositionResponse(
        position_id=position.id,
        portfolio_id=position.portfolio_id,
        stock_id=position.stock_id,
        symbol=symbols.get(position.stock_id),
        quantity=position.quantity,
        average_entry_price=position.average_entry_price,
        cost_basis=position.cost_basis,
        entry_fees_total=position.entry_fees_total,
        current_price=position.current_price,
        market_value=position.market_value,
        unrealized_pnl=position.unrealized_pnl,
        realized_pnl=position.realized_pnl,
        allocation_pct=allocation_pct(position.market_value or Decimal("0"), valuation.equity),
        opened_at=position.opened_at,
        updated_at=position.updated_at,
    )


# ----------------------------------------------------------------------
# Portfolio lifecycle
# ----------------------------------------------------------------------


@router.post(
    "",
    response_model=PortfolioResponse,
    summary="Create a paper portfolio",
    description=(
        "Creates a virtual portfolio with no positions. Cash and equity both start at "
        "`initial_capital`, and realised, unrealised and total P&L all start at 0.\n\n"
        f"{_DISCLAIMER}"
    ),
    status_code=status.HTTP_201_CREATED,
    responses=_ERROR_RESPONSES,
)
def create_portfolio(
    payload: PortfolioCreate,
    db: Session = Depends(get_db),
    service: PaperTradingService = Depends(get_paper_trading_service),
) -> PortfolioResponse:
    """Create a virtual paper portfolio."""
    portfolio = service.create_portfolio(db, payload)
    return _portfolio_summary(db, service, portfolio)


@router.get(
    "",
    response_model=PortfolioListResponse,
    summary="List paper portfolios",
    description="Returns virtual portfolios, newest first.",
    status_code=status.HTTP_200_OK,
)
def list_portfolios(
    skip: int = Query(0, ge=0, description="Records to skip"),
    limit: int = Query(50, ge=1, le=MAX_PAGE_LIMIT, description="Maximum records to return"),
    db: Session = Depends(get_db),
    service: PaperTradingService = Depends(get_paper_trading_service),
) -> PortfolioListResponse:
    """List portfolios."""
    portfolios = service.list_portfolios(db, skip=skip, limit=limit)
    data = [_portfolio_summary(db, service, portfolio) for portfolio in portfolios]
    return PortfolioListResponse(data=data, count=len(data))


@router.get(
    "/{portfolio_id}",
    response_model=PortfolioResponse,
    summary="Get a paper portfolio summary",
    description=(
        "Returns cash, market value, equity, realised and unrealised P&L, total P&L, "
        "return percentage and activity counts for one portfolio."
    ),
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def get_portfolio(
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    db: Session = Depends(get_db),
    service: PaperTradingService = Depends(get_paper_trading_service),
) -> PortfolioResponse:
    """Return the portfolio summary."""
    portfolio = service.get_portfolio(db, portfolio_id)
    return _portfolio_summary(db, service, portfolio)


@router.patch(
    "/{portfolio_id}/status",
    response_model=PortfolioResponse,
    summary="Change a portfolio status",
    description=(
        "Moves a portfolio between `ACTIVE`, `PAUSED` and `CLOSED`. Only `ACTIVE` "
        "portfolios accept orders; `PAUSED` and `CLOSED` reject every new order without "
        "changing cash, positions or P&L. `CLOSED` is terminal."
    ),
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def update_portfolio_status(
    payload: PortfolioStatusUpdate,
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    db: Session = Depends(get_db),
    service: PaperTradingService = Depends(get_paper_trading_service),
) -> PortfolioResponse:
    """Change the portfolio lifecycle state."""
    portfolio = service.update_status(db, portfolio_id, payload)
    return _portfolio_summary(db, service, portfolio)


# ----------------------------------------------------------------------
# Lifecycle shortcuts (Phase 12)
# ----------------------------------------------------------------------
# ``PATCH /{portfolio_id}/status`` remains the general transition endpoint. These two are the
# Phase 12 lifecycle actions named explicitly, for a caller (or a script) that only ever wants
# to pause or to close and has no reason to know the status enum.


@router.post(
    "/{portfolio_id}/pause",
    response_model=PortfolioResponse,
    summary="Pause a portfolio",
    description=(
        "Stops the portfolio accepting new orders while keeping every position, order, trade, "
        "snapshot and cash-ledger entry. Reading, valuing and snapshotting still work, so the "
        "portfolio's history stays intact and it can be resumed later. A `CLOSED` portfolio "
        "cannot be paused."
    ),
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def pause_portfolio(
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    db: Session = Depends(get_db),
    service: PaperTradingService = Depends(get_paper_trading_service),
) -> PortfolioResponse:
    """Pause a portfolio: no new orders, nothing deleted."""
    portfolio = service.pause_portfolio(db, portfolio_id)
    return _portfolio_summary(db, service, portfolio)


@router.post(
    "/{portfolio_id}/close",
    response_model=PortfolioResponse,
    summary="Close a portfolio permanently",
    description=(
        "Retires the portfolio permanently. This is terminal and only removes its ability to "
        "trade: positions, orders, trades, snapshots and the cash ledger are all retained, so "
        "everything the portfolio produced stays readable and auditable. Closing an already "
        "closed portfolio is a safe no-op."
    ),
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def close_portfolio(
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    db: Session = Depends(get_db),
    service: PaperTradingService = Depends(get_paper_trading_service),
) -> PortfolioResponse:
    """Close a portfolio, preserving its entire history."""
    portfolio = service.close_portfolio(db, portfolio_id)
    return _portfolio_summary(db, service, portfolio)


# ----------------------------------------------------------------------
# Phase 15: portfolio-level risk, stress testing and exposure control
# ----------------------------------------------------------------------
#
# These three routes are registered **before** the ``/{portfolio_id}`` routes on purpose. A literal
# path segment (``/risk/analyze``) has to be matched first, or FastAPI would try to parse "risk"
# as a portfolio id and answer with a 422 instead of running the analysis.

_RISK_SUMMARY = "Evaluate a portfolio against portfolio-level risk limits"

_RISK_DESCRIPTION = (
    "The Phase 15 portfolio risk engine. It sits above Phase 14's strategy allocation and asks a "
    "different question: is the portfolio Phase 14 produced acceptable?\n\n"
    "The report contains:\n\n"
    "- **exposure** — asset, sector, strategy and asset-class weights, with symbols held by several "
    "strategies aggregated into one portfolio-level exposure;\n"
    "- **concentration** — largest position, top-N concentration, and per-rule utilization;\n"
    "- **risk metrics** — annualised volatility, beta, maximum drawdown, historical VaR and "
    "Expected Shortfall, each with its limit and status;\n"
    "- **risk contributions** — marginal and component contribution of every position, with the "
    "reconciliation error against total portfolio volatility;\n"
    "- **correlation clusters** — groups of positions moving together above the configured "
    "threshold;\n"
    "- **stress tests** — seven deterministic scenarios (broad crash, severe crash, sector shock, "
    "high-beta shock, correlation spike, liquidity stress, strategy drawdown);\n"
    "- **liquidity** — participation ratio, liquidity score and estimated days to liquidate;\n"
    "- **violations, adjustments and the decision** — `APPROVED`, `ADJUSTED`, `REJECTED` or "
    "`DATA_INSUFFICIENT`, with a human-readable reason for each.\n\n"
    "Every figure is an **estimate from the historical sample supplied**. Data that was not supplied "
    "is reported as `DATA_INSUFFICIENT` with a null value, never as zero: missing data is not low "
    "risk. Stress scenarios are deterministic simulations of today's holdings, not forecasts. The "
    "limits are configurable policy parameters, not investment advice.\n\n"
    "Adjustments only ever **reduce**: shares are sold and the proceeds stay in cash, so portfolio "
    "equity and total leverage are unchanged.\n\n"
    f"{_DISCLAIMER}"
)


@router.post(
    "/risk/analyze",
    response_model=PortfolioRiskAnalysisResponseSchema,
    summary=_RISK_SUMMARY,
    description=_RISK_DESCRIPTION,
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def analyze_portfolio_risk(
    payload: PortfolioRiskRequestSchema,
    risk: PortfolioRiskService = Depends(get_portfolio_risk_service),
) -> PortfolioRiskAnalysisResponseSchema:
    """Evaluate a submitted portfolio and return the canonical portfolio risk report."""
    assessment = risk.analyze(payload, timestamp=utcnow())
    return analysis_response(assessment, timestamp=utcnow())


@router.post(
    "/risk/adjust",
    response_model=PortfolioRiskAdjustmentResponseSchema,
    summary="Run deterministic portfolio risk adjustment",
    description=(
        "Applies the deterministic reduction loop to a submitted portfolio and returns the "
        "portfolio before and after it, the adjustments performed with their reasons, and the "
        "resulting decision.\n\n"
        "Reductions are chosen by a fixed priority order — position, sector, strategy, asset "
        "class, top-N concentration, correlated cluster, risk contribution, stress loss, "
        "participation — and every action is re-evaluated against the whole measurement stack "
        "before the next one is considered. Shares are sold and the proceeds stay in cash.\n\n"
        "A breach that no reduction can fix — a historical drawdown, a VaR or Expected Shortfall "
        "quantile of the supplied sample, a minimum position count that conflicts with a "
        "concentration rule — is reported rather than traded around.\n\n"
        f"{_DISCLAIMER}"
    ),
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def adjust_portfolio_risk(
    payload: PortfolioRiskRequestSchema,
    risk: PortfolioRiskService = Depends(get_portfolio_risk_service),
) -> PortfolioRiskAdjustmentResponseSchema:
    """Reduce a portfolio until it complies, or report what could not be corrected."""
    assessment = risk.analyze(payload, timestamp=utcnow())
    snapshot = snapshot_from_request(payload)
    original = AdjustedPortfolio(
        portfolio_id=snapshot.portfolio_id,
        portfolio_value=snapshot.portfolio_value,
        cash=snapshot.cash,
        positions=snapshot.positions,
        exposure=ExposureAnalyzer().analyze(snapshot),
    )
    return adjustment_response(assessment, original, timestamp=utcnow())


@router.post(
    "/{portfolio_id}/risk/analyze",
    response_model=PortfolioRiskAnalysisResponseSchema,
    summary="Evaluate a stored paper portfolio against portfolio-level risk limits",
    description=(
        "Runs the Phase 15 risk engine against a persisted Phase 9 paper portfolio.\n\n"
        "The request is read from stored state: open positions, cash, sector classification from "
        "the company profile, and traded value from the mean close-times-volume of the most recent "
        "stored bars. A symbol held by several strategies is submitted once per strategy, so the "
        "engine can aggregate it to a single portfolio-level exposure.\n\n"
        "Only stored data is used. A position with no traded volume reports liquidity as "
        "unavailable rather than assuming it is liquid, and a position with no usable price is "
        "refused rather than valued at zero.\n\n"
        f"{_RISK_DESCRIPTION}"
    ),
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def analyze_stored_portfolio_risk(
    payload: Optional[PortfolioRiskOverridesSchema] = None,
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    db: Session = Depends(get_db),
    risk: PortfolioRiskService = Depends(get_portfolio_risk_service),
) -> PortfolioRiskAnalysisResponseSchema:
    """Evaluate a persisted paper portfolio."""
    from app.services.portfolio.service import paper_trading_service

    portfolio = paper_trading_service.get_portfolio(db, portfolio_id)
    overrides = payload.risk_config if payload is not None else None
    assessment = risk.analyze_persisted(
        db, portfolio, overrides=overrides, timestamp=utcnow()
    )
    return analysis_response(assessment, timestamp=utcnow())


# ----------------------------------------------------------------------
# Positions
# ----------------------------------------------------------------------


@router.get(
    "/{portfolio_id}/positions",
    response_model=PortfolioPositionsResponse,
    summary="Get open positions",
    description=(
        "Returns every open long position with its weighted average entry price, market "
        "value, unrealised P&L and share of portfolio equity."
    ),
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def get_positions(
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    stock_id: Optional[int] = Query(None, ge=1, description="Filter to one stock"),
    db: Session = Depends(get_db),
    service: PaperTradingService = Depends(get_paper_trading_service),
) -> PortfolioPositionsResponse:
    """Return the open positions of a portfolio."""
    positions = service.get_positions(db, portfolio_id)
    if stock_id is not None:
        positions = [position for position in positions if position.stock_id == stock_id]
    valuation = service.calculate_portfolio_value(db, portfolio_id)
    data = [_position_response(db, position, valuation) for position in positions]
    return PortfolioPositionsResponse(data=data, count=len(data), portfolio_id=portfolio_id)


# ----------------------------------------------------------------------
# Orders and trades
# ----------------------------------------------------------------------


@router.post(
    "/{portfolio_id}/orders",
    response_model=OrderExecutionResponse,
    summary="Place a simulated order",
    description=(
        "Validates, risk-checks and — if accepted — fills a simulated `MARKET` order "
        "immediately at the supplied price. A buy is funded from cash and updates the "
        "weighted average entry price; a sell reduces or closes the position and books "
        "realised P&L net of fees.\n\n"
        "A rejected order (inactive portfolio, insufficient cash, oversell, risk limit "
        "breach, invalid quantity or price) is stored as `REJECTED` with a reason and leaves "
        "cash, positions and P&L untouched.\n\n"
        "Supplying `idempotency_key` makes the submission idempotent: repeating the same "
        "key returns the original order instead of executing twice.\n\n"
        f"{_DISCLAIMER}"
    ),
    status_code=status.HTTP_201_CREATED,
    responses=_ERROR_RESPONSES,
)
def place_order(
    payload: OrderCreate,
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    db: Session = Depends(get_db),
    service: PaperTradingService = Depends(get_paper_trading_service),
) -> OrderExecutionResponse:
    """Submit a simulated market order."""
    result = service.place_order(db, portfolio_id, payload)
    return _order_execution_response(db, service, result)


@router.get(
    "/{portfolio_id}/orders",
    response_model=PortfolioOrdersResponse,
    summary="Get order history",
    description=(
        "Returns submitted orders, newest first, including rejected orders with their "
        "rejection code and reason."
    ),
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def get_orders(
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    status_filter: Optional[OrderStatus] = Query(
        None, alias="status", description="Filter by order status"
    ),
    stock_id: Optional[int] = Query(None, ge=1, description="Filter by stock"),
    skip: int = Query(0, ge=0, description="Records to skip"),
    limit: int = Query(100, ge=1, le=MAX_PAGE_LIMIT, description="Maximum records to return"),
    db: Session = Depends(get_db),
    service: PaperTradingService = Depends(get_paper_trading_service),
) -> PortfolioOrdersResponse:
    """Return the order history of a portfolio."""
    orders = service.get_orders(
        db, portfolio_id, skip=skip, limit=limit, status=status_filter, stock_id=stock_id
    )
    symbols = _stock_symbols(db, [order.stock_id for order in orders])
    data = [_order_response(order, symbols.get(order.stock_id)) for order in orders]
    return PortfolioOrdersResponse(
        data=data,
        count=len(data),
        # ``total`` must describe the same filtered set as ``data``, or a caller paging
        # through a filtered history is given the wrong page count.
        total=service.count_orders(db, portfolio_id, status=status_filter, stock_id=stock_id),
        portfolio_id=portfolio_id,
    )


@router.get(
    "/{portfolio_id}/trades",
    response_model=PortfolioTradesResponse,
    summary="Get trade history",
    description=(
        "Returns one immutable execution record per filled order, newest first, with gross "
        "value, fees, signed cash impact and realised P&L."
    ),
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def get_trades(
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    stock_id: Optional[int] = Query(None, ge=1, description="Filter by stock"),
    skip: int = Query(0, ge=0, description="Records to skip"),
    limit: int = Query(100, ge=1, le=MAX_PAGE_LIMIT, description="Maximum records to return"),
    db: Session = Depends(get_db),
    service: PaperTradingService = Depends(get_paper_trading_service),
) -> PortfolioTradesResponse:
    """Return the trade history of a portfolio."""
    trades = service.get_trades(db, portfolio_id, skip=skip, limit=limit, stock_id=stock_id)
    symbols = _stock_symbols(db, [trade.stock_id for trade in trades])
    data = [_trade_response(trade, symbols.get(trade.stock_id)) for trade in trades]
    return PortfolioTradesResponse(
        data=data,
        count=len(data),
        total=service.count_trades(db, portfolio_id, stock_id=stock_id),
        portfolio_id=portfolio_id,
    )


# ----------------------------------------------------------------------
# Valuation, snapshots and performance
# ----------------------------------------------------------------------


@router.post(
    "/{portfolio_id}/valuation",
    response_model=PortfolioValuationResponse,
    summary="Value a portfolio at current market prices",
    description=(
        "Marks every open position to the supplied prices and returns market value, "
        "unrealised P&L, equity and total P&L. **Valuation only:** no order, trade or cash "
        "movement is created."
    ),
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def value_portfolio(
    payload: PortfolioValuationRequest,
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    db: Session = Depends(get_db),
    service: PaperTradingService = Depends(get_paper_trading_service),
) -> PortfolioValuationResponse:
    """Re-value a portfolio at the supplied market prices."""
    prices = _price_map(payload.prices)
    as_of = payload.as_of or utcnow()
    valuation = service.update_market_prices(db, portfolio_id, prices, as_of=as_of)
    positions = service.get_positions(db, portfolio_id)
    return _valuation_response(db, valuation, positions, as_of)


@router.get(
    "/{portfolio_id}/valuation",
    response_model=PortfolioValuationResponse,
    summary="Value a portfolio at its stored market prices",
    description=(
        "Returns the current mark-to-market view — cash, market value, equity, cost basis, "
        "unrealised P&L, return % and exposure % — using the latest stored close for every "
        "open position. No prices are applied, so no position mark changes and no order, trade "
        "or cash movement is created. Use `POST /{portfolio_id}/valuation` to value at prices "
        "you supply."
    ),
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def get_valuation(
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    db: Session = Depends(get_db),
    service: PaperTradingService = Depends(get_paper_trading_service),
) -> PortfolioValuationResponse:
    """Return the portfolio's current valuation at its stored market prices."""
    as_of = utcnow()
    valuation = service.calculate_portfolio_value(db, portfolio_id)
    positions = service.get_positions(db, portfolio_id)
    return _valuation_response(db, valuation, positions, as_of)


@router.post(
    "/{portfolio_id}/snapshots",
    response_model=PortfolioSnapshotResponse,
    summary="Create a portfolio snapshot",
    description=(
        "Captures one point of the equity curve: cash, market value, equity, realised and "
        "unrealised P&L, total P&L and return percentage. Optional prices are applied first. "
        "Snapshots are created on request — there is no background scheduler."
    ),
    status_code=status.HTTP_201_CREATED,
    responses=_ERROR_RESPONSES,
)
def create_snapshot(
    payload: PortfolioSnapshotCreate,
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    db: Session = Depends(get_db),
    service: PaperTradingService = Depends(get_paper_trading_service),
) -> PortfolioSnapshotResponse:
    """Capture an equity-curve point."""
    prices = _price_map(payload.prices)
    snapshot = service.create_snapshot(db, portfolio_id, prices, as_of=payload.as_of)
    return _snapshot_response(snapshot)


@router.get(
    "/{portfolio_id}/snapshots",
    response_model=PortfolioSnapshotsResponse,
    summary="Get the portfolio equity curve",
    description="Returns recorded snapshots, oldest first, with pagination.",
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def get_snapshots(
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    skip: int = Query(0, ge=0, description="Records to skip"),
    limit: int = Query(100, ge=1, le=MAX_PAGE_LIMIT, description="Maximum records to return"),
    db: Session = Depends(get_db),
    service: PaperTradingService = Depends(get_paper_trading_service),
) -> PortfolioSnapshotsResponse:
    """Return the equity curve of a portfolio."""
    snapshots = service.get_snapshots(db, portfolio_id, skip=skip, limit=limit)
    data = [_snapshot_response(snapshot) for snapshot in snapshots]
    return PortfolioSnapshotsResponse(
        data=data,
        count=len(data),
        total=service.count_snapshots(db, portfolio_id),
        portfolio_id=portfolio_id,
    )


@router.get(
    "/{portfolio_id}/performance",
    response_model=PortfolioPerformanceResponse,
    summary="Get portfolio performance",
    description=(
        "Returns portfolio-level performance: cash, market value, equity, realised and "
        "unrealised P&L, total P&L, return percentage, fees, order/trade/snapshot counts, "
        "peak equity and drawdown, plus the current position allocation."
    ),
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def get_performance(
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    db: Session = Depends(get_db),
    service: PaperTradingService = Depends(get_paper_trading_service),
) -> PortfolioPerformanceResponse:
    """Return portfolio-level performance figures."""
    performance = service.get_performance(db, portfolio_id)
    positions = service.get_positions(db, portfolio_id)
    return _performance_response(db, service, performance, positions)


# ----------------------------------------------------------------------
# Strategy integration
# ----------------------------------------------------------------------


@router.post(
    "/{portfolio_id}/strategy-orders",
    response_model=StrategyOrderResponse,
    summary="Run the existing signal strategy for a portfolio",
    description=(
        "Evaluates the **existing** Phase 7 signal engine and the **existing** Phase 8 "
        "`phase7_technical` strategy rules for one stock, then places the resulting paper "
        "order through the same risk validation and execution path as a manual order.\n\n"
        "`enter` buys (sized as a percentage of available cash, or an explicit quantity), "
        "`exit` sells the whole open position, and `hold` places no order. No indicator or "
        "strategy rule is duplicated here — Phase 7/8 behaviour is reused unchanged, and a "
        "decision already processed for that signal date is not executed twice.\n\n"
        f"{_DISCLAIMER}"
    ),
    status_code=status.HTTP_201_CREATED,
    responses=_ERROR_RESPONSES,
)
def execute_strategy_order(
    payload: StrategyOrderRequest,
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    db: Session = Depends(get_db),
    service: PaperTradingService = Depends(get_paper_trading_service),
) -> StrategyOrderResponse:
    """Turn the existing strategy decision into a simulated paper order."""
    result = service.execute_strategy_order(db, portfolio_id, payload)
    return _strategy_response(db, service, result)


@router.patch(
    "/{portfolio_id}",
    response_model=PortfolioSummary,
    summary="Update a paper portfolio",
    description=(
        "Partially updates a portfolio's `name`, `description` or `status`. Only the fields "
        "supplied are changed; `CLOSED` remains terminal.\n\n"
        "Returns the portfolio summary including its exposure and invested capital, which are "
        "the figures the risk engine measures against."
    ),
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def update_portfolio(
    payload: PortfolioUpdate,
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    db: Session = Depends(get_db),
    service: PaperTradingService = Depends(get_paper_trading_service),
) -> PortfolioSummary:
    """Partially update a portfolio."""
    portfolio = service.update_portfolio(db, portfolio_id, payload)
    return _portfolio_summary_view(db, service, portfolio)


@router.get(
    "/{portfolio_id}/positions/{stock_id}",
    response_model=PositionResponse,
    summary="Get one open position",
    description=(
        "Returns a single open long position with its weighted average entry price, market "
        "value, unrealised P&L and share of portfolio equity. Returns 404 when the portfolio "
        "holds no position in that stock."
    ),
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def get_position(
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    stock_id: int = Path(..., ge=1, description="Stock ID"),
    db: Session = Depends(get_db),
    service: PaperTradingService = Depends(get_paper_trading_service),
) -> PositionResponse:
    """Return one open position."""
    position = service.get_position(db, portfolio_id, stock_id)
    valuation = service.calculate_portfolio_value(db, portfolio_id)
    return _position_response(db, position, valuation)


@router.get(
    "/{portfolio_id}/orders/{order_id}",
    response_model=OrderResponse,
    summary="Get one order",
    description=(
        "Returns a single order, filled, pending or rejected, with its rejection code and "
        "reason when it was not executed. Returns 404 for an unknown order or one that belongs "
        "to another portfolio."
    ),
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def get_order(
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    order_id: int = Path(..., ge=1, description="Order ID"),
    db: Session = Depends(get_db),
    service: PaperTradingService = Depends(get_paper_trading_service),
) -> OrderResponse:
    """Return one order."""
    order = service.get_order(db, portfolio_id, order_id)
    symbols = _stock_symbols(db, [order.stock_id])
    return _order_response(order, symbols.get(order.stock_id))


@router.post(
    "/{portfolio_id}/orders/{order_id}/execute",
    response_model=OrderExecutionResponse,
    summary="Execute a pending order",
    description=(
        "Fills an order that was recorded with `defer_execution: true`.\n\n"
        "The order is re-validated from scratch against current portfolio state, so it is only "
        "executed if it would still be accepted: a position may have closed, or cash may have "
        "been spent, since it was recorded. An order that is not `PENDING` is refused (422) "
        "rather than executed twice, and a rejection still leaves cash, positions and P&L "
        "untouched.\n\n"
        f"{_DISCLAIMER}"
    ),
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def execute_pending_order(
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    order_id: int = Path(..., ge=1, description="Order ID"),
    db: Session = Depends(get_db),
    service: PaperTradingService = Depends(get_paper_trading_service),
) -> OrderExecutionResponse:
    """Execute a previously recorded pending order."""
    result = service.execute_pending_order(db, portfolio_id, order_id)
    return _order_execution_response(db, service, result)


# ----------------------------------------------------------------------
# Risk, sizing and drawdown
# ----------------------------------------------------------------------


@router.post(
    "/{portfolio_id}/risk/check",
    response_model=RiskCheckResponse,
    summary="Risk-check a hypothetical trade",
    description=(
        "Evaluates a **hypothetical** order against every configured risk limit and returns "
        "each rule it would break. Nothing is persisted: cash, positions and P&L are "
        "untouched, so this is always safe to call before committing to an order.\n\n"
        "Supply `requested_price`, or set `use_market_price` to evaluate against the latest "
        "stored close."
    ),
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def check_risk(
    payload: RiskCheckRequest,
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    db: Session = Depends(get_db),
    service: PaperTradingService = Depends(get_paper_trading_service),
) -> RiskCheckResponse:
    """Check whether the risk engine would accept a proposed trade."""
    assessment = service.check_risk(db, portfolio_id, payload)
    return _risk_check_response(payload, assessment)


@router.get(
    "/{portfolio_id}/risk",
    response_model=PortfolioRiskResponse,
    summary="Get effective risk limits and current breaches",
    description=(
        "Returns the risk rules in force for this portfolio — with the application defaults "
        "resolved — plus the equity, cash, exposure, invested capital, peak equity and "
        "current drawdown they are measured against, and **any rule the portfolio is already "
        "breaching**."
    ),
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def get_risk(
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    db: Session = Depends(get_db),
    service: PaperTradingService = Depends(get_paper_trading_service),
) -> PortfolioRiskResponse:
    """Report the portfolio's risk posture."""
    portfolio = service.get_portfolio(db, portfolio_id)
    limits = service.risk_limits(portfolio)
    state = service.effective_risk(db, portfolio_id)
    valuation = state["valuation"]
    return PortfolioRiskResponse(
        portfolio_id=portfolio.id,
        status=PortfolioStatus(portfolio.status),
        equity=valuation.equity,
        cash=valuation.cash,
        cash_reserve_required=state["cash_reserve_required"],
        market_value=valuation.market_value,
        exposure_pct=state["exposure_pct"],
        invested_capital=valuation.invested_capital,
        position_count=len(valuation.positions),
        max_positions=limits.max_positions,
        peak_equity=state["peak_equity"],
        current_drawdown_pct=state["current_drawdown_pct"],
        drawdown_limit_pct=limits.max_drawdown_pct,
        drawdown_breached=state["drawdown_breached"],
        risk_limits=_risk_config(limits),
        violations=[_violation_response(item) for item in state["violations"]],
        as_of=utcnow(),
    )


@router.post(
    "/{portfolio_id}/position-size",
    response_model=PositionSizingResponse,
    summary="Compute a position size",
    description=(
        "Derives the quantity of an order from a capital budget, a portfolio percentage or a "
        "risk/stop-loss budget, and returns the arithmetic behind it. No order is placed.\n\n"
        "* `FIXED_CAPITAL` — `floor(capital / price)`\n"
        "* `PERCENTAGE` — `floor(equity * allocation_pct / price)`\n"
        "* `RISK_BASED` — `floor(equity * risk_pct / abs(entry - stop))`, capped at what the "
        "available cash can fund\n\n"
        "Quantities are always floored to whole shares and fees are included in the unit cost."
    ),
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def position_size(
    payload: PositionSizingRequest,
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    db: Session = Depends(get_db),
    service: PaperTradingService = Depends(get_paper_trading_service),
) -> PositionSizingResponse:
    """Compute the quantity an order would have."""
    portfolio = service.get_portfolio(db, portfolio_id)
    plan = service.size_position(db, portfolio_id, payload)
    return PositionSizingResponse(
        portfolio_id=portfolio.id,
        method=plan.method,
        stock_id=payload.stock_id,
        price=plan.price,
        quantity=plan.quantity,
        capital=plan.capital,
        budget=plan.budget,
        equity=plan.equity,
        risk_per_share=plan.risk_per_share,
        allowed_risk=plan.allowed_risk,
        reason=plan.reason,
    )


@router.get(
    "/{portfolio_id}/equity-curve",
    response_model=PortfolioSnapshotsResponse,
    summary="Get the portfolio equity curve",
    description=(
        "Returns the recorded equity curve, oldest first: cash, market value, equity, "
        "realised and unrealised P&L, return percentage, drawdown from the running peak and "
        "exposure at each point. `as_of` is today's date when no snapshot is recorded, so the "
        "newest point is always the latest known state."
    ),
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def get_equity_curve(
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    skip: int = Query(0, ge=0, description="Records to skip"),
    limit: int = Query(100, ge=1, le=MAX_PAGE_LIMIT, description="Maximum records to return"),
    start_date: Optional[datetime] = Query(
        None, description="Only points at or after this timestamp"
    ),
    end_date: Optional[datetime] = Query(
        None, description="Only points at or before this timestamp (a bare date includes the day)"
    ),
    db: Session = Depends(get_db),
    service: PaperTradingService = Depends(get_paper_trading_service),
) -> PortfolioSnapshotsResponse:
    """Return the equity curve of a portfolio."""
    snapshots = service.get_snapshots(
        db,
        portfolio_id,
        skip=skip,
        limit=limit,
        start_date=start_date,
        end_date=end_date,
    )
    data = [_snapshot_response(snapshot) for snapshot in snapshots]
    return PortfolioSnapshotsResponse(
        data=data,
        count=len(data),
        total=service.count_snapshots(
            db, portfolio_id, start_date=start_date, end_date=end_date
        ),
        portfolio_id=portfolio_id,
    )


@router.get(
    "/{portfolio_id}/drawdown",
    response_model=PortfolioDrawdownResponse,
    summary="Get the portfolio drawdown",
    description=(
        "Returns peak equity, current equity, the current and maximum drawdown, and the full "
        "drawdown curve.\n\n"
        "Percentages are reported in both conventions: `*_pct` is the **positive magnitude** "
        "of the decline, while `*_signed_pct` is `(current - peak) / peak * 100` and is "
        "therefore `0` at a peak and negative below it."
    ),
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def get_drawdown(
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    db: Session = Depends(get_db),
    service: PaperTradingService = Depends(get_paper_trading_service),
) -> PortfolioDrawdownResponse:
    """Return the drawdown curve and its extremes."""
    drawdown = service.get_drawdown(db, portfolio_id)
    points = [
        DrawdownPointResponse(
            timestamp=point.timestamp,
            equity=quantize_money(point.equity),
            peak_equity=quantize_money(point.peak_equity),
            drawdown_pct=quantize_percent(point.drawdown_pct),
            exposure_pct=(
                None if point.exposure_pct is None else quantize_percent(point.exposure_pct)
            ),
        )
        for point in drawdown["points"]
    ]
    current_equity = drawdown["current_equity"]
    peak_equity = drawdown["peak_equity"]
    return PortfolioDrawdownResponse(
        portfolio_id=portfolio_id,
        as_of=utcnow(),
        peak_equity=peak_equity,
        current_equity=current_equity,
        current_drawdown_pct=abs(signed_drawdown_pct(peak_equity, current_equity)),
        current_drawdown_signed_pct=signed_drawdown_pct(peak_equity, current_equity),
        max_drawdown=drawdown["max_drawdown"],
        max_drawdown_pct=drawdown["max_drawdown_pct"],
        max_drawdown_signed_pct=-drawdown["max_drawdown_pct"],
        peak_timestamp=_timestamp_of(drawdown["peak_point"]),
        trough_timestamp=_timestamp_of(drawdown["trough_point"]),
        points=points,
    )


# ----------------------------------------------------------------------
# Cash ledger (Phase 11)
# ----------------------------------------------------------------------


@router.post(
    "/{portfolio_id}/deposit",
    response_model=CashMovementResponse,
    summary="Deposit virtual capital",
    description=(
        "Adds capital to a paper portfolio. Cash and equity both rise by the amount; no order, "
        "position or realised P&L is created, and a `DEPOSIT` entry is appended to the immutable "
        "transaction ledger.\n\n"
        "**Deposits are not returns.** A deposit raises equity without raising invested "
        "capital, so it dilutes `return_pct` rather than creating performance — the ledger keeps "
        "the two apart, and `realized_pnl` stays 0.\n\n"
        f"{_DISCLAIMER}"
    ),
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def deposit_cash(
    payload: CashMovementRequest,
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    db: Session = Depends(get_db),
    service: PaperTradingService = Depends(get_paper_trading_service),
) -> CashMovementResponse:
    """Add virtual capital to a portfolio."""
    entry = service.deposit(db, portfolio_id, payload)
    return _cash_movement_response(db, service, portfolio_id, entry)


@router.post(
    "/{portfolio_id}/withdraw",
    response_model=CashMovementResponse,
    summary="Withdraw virtual capital",
    description=(
        "Takes capital out of a paper portfolio. Cash and equity both fall by the amount and a "
        "`WITHDRAWAL` entry is appended to the ledger.\n\n"
        "A withdrawal cannot leave cash negative: if it would, the request is refused with "
        "`409` and the portfolio is left untouched. Existing positions are **not** liquidated — "
        "the remaining cash can therefore drop below the configured reserve, which the risk "
        "report will then flag.\n\n"
        f"{_DISCLAIMER}"
    ),
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def withdraw_cash(
    payload: CashMovementRequest,
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    db: Session = Depends(get_db),
    service: PaperTradingService = Depends(get_paper_trading_service),
) -> CashMovementResponse:
    """Take virtual capital out of a portfolio."""
    entry = service.withdraw(db, portfolio_id, payload)
    return _cash_movement_response(db, service, portfolio_id, entry)


@router.get(
    "/{portfolio_id}/transactions",
    response_model=PortfolioTransactionsResponse,
    summary="Get the transaction ledger",
    description=(
        "Returns the portfolio's append-only transaction ledger, newest first: every simulated "
        "buy, sell, deposit and withdrawal with the cash balance it produced.\n\n"
        "The ledger is never edited or deleted, so summing `net_cash_impact` over all its "
        "entries reproduces exactly how the current balance was reached — which is the "
        "`balance` field. Filter by `transaction_type` to read one stream: cash movements and "
        "trades stay independently auditable. `start_date` and `end_date` are inclusive of a "
        "whole day, so `end_date=2026-01-15` means everything up to and including that day."
    ),
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def list_transactions(
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    transaction_type: Optional[TransactionType] = Query(
        None, description="Only entries of this type"
    ),
    stock_id: Optional[int] = Query(None, ge=1, description="Only entries for this stock"),
    start_date: Optional[datetime] = Query(
        None, description="Only entries at or after this timestamp"
    ),
    end_date: Optional[datetime] = Query(
        None, description="Only entries at or before this timestamp (a bare date includes the day)"
    ),
    skip: int = Query(0, ge=0, description="Records to skip"),
    limit: int = Query(50, ge=1, le=MAX_PAGE_LIMIT, description="Maximum records to return"),
    db: Session = Depends(get_db),
    service: PaperTradingService = Depends(get_paper_trading_service),
) -> PortfolioTransactionsResponse:
    """Return the portfolio's transaction ledger, newest first."""
    portfolio_row = service.get_portfolio(db, portfolio_id)
    entries = service.list_transactions(
        db,
        portfolio_id,
        transaction_type=transaction_type,
        stock_id=stock_id,
        start_date=start_date,
        end_date=end_date,
        skip=skip,
        limit=limit,
    )
    symbols = _stock_symbols(db, [entry.stock_id for entry in entries])
    data = [
        _transaction_response(entry, symbols.get(entry.stock_id)) for entry in entries
    ]
    return PortfolioTransactionsResponse(
        data=data,
        count=len(data),
        total=service.count_transactions(
            db,
            portfolio_id,
            transaction_type=transaction_type,
            stock_id=stock_id,
            start_date=start_date,
            end_date=end_date,
        ),
        portfolio_id=portfolio_id,
        balance=service.ledger_balance(db, portfolio_row),
    )


@router.get(
    "/{portfolio_id}/transactions/summary",
    response_model=PortfolioTransactionTypeResponse,
    summary="Count the transaction ledger by type",
    description=(
        "Returns the number of ledger entries per transaction type, so a caller can summarise a "
        "portfolio's cash and trade history without paging the whole ledger."
    ),
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def summarize_transactions(
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    db: Session = Depends(get_db),
    service: PaperTradingService = Depends(get_paper_trading_service),
) -> PortfolioTransactionTypeResponse:
    """Count ledger entries by transaction type."""
    service.get_portfolio(db, portfolio_id)
    return PortfolioTransactionTypeResponse(
        portfolio_id=portfolio_id,
        counts=service.transaction_counts(db, portfolio_id),
    )


# ----------------------------------------------------------------------
# Portfolio construction and rebalancing (Phase 11)
# ----------------------------------------------------------------------


@router.post(
    "/{portfolio_id}/decisions",
    response_model=PortfolioDecisionsResponse,
    summary="Get explainable portfolio construction decisions",
    description=(
        "Answers *how much should this portfolio hold in each of these stocks*, and returns one "
        "explainable `BUY` / `SELL` / `HOLD` / `REDUCE` decision per candidate with the numbers "
        "behind it.\n\n"
        "**Candidates come from exactly one source** — explicit `candidates`, a Phase 7 "
        "`screener` (reusing `ScreenerService` unchanged, so no screening rule is duplicated), "
        "or neither, in which case only the positions already held are re-examined. That last "
        "mode is how a portfolio discovers a stop-loss or take-profit it has already reached: "
        "no new stock needs to be screened to ask *should I still be holding this?*\n\n"
        "**Nothing is written by default.** Decisions are a recommendation until you set "
        "`apply=true`, which submits the actionable, unblocked ones as simulated orders through "
        "`PaperTradingService`. A decision a risk limit refused is returned blocked and is never "
        "retried.\n\n"
        f"{_DISCLAIMER}"
    ),
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def decide_portfolio(
    payload: PortfolioDecisionRequest,
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    db: Session = Depends(get_db),
    service: PaperTradingService = Depends(get_paper_trading_service),
    construction: PortfolioConstructionService = Depends(get_portfolio_construction_service),
) -> PortfolioDecisionsResponse:
    """Build, and optionally apply, the portfolio's construction decisions."""
    portfolio = service.get_portfolio(db, portfolio_id)
    decisions: PortfolioDecisions = construction.decide(
        db, portfolio, payload, payload.as_of or utcnow()
    )
    results = (
        construction.apply_decisions(db, portfolio, decisions.decisions)
        if payload.apply
        else ()
    )
    return _decisions_response(portfolio, decisions, results)


@router.post(
    "/{portfolio_id}/rebalance",
    response_model=RebalanceResponse,
    summary="Plan, and optionally apply, a rebalance",
    description=(
        "Compares current position weights with their targets and proposes the trades that "
        "would close the gap, reporting the ones that need no trade too.\n\n"
        "Targets come from exactly one source: explicit `target_weights`, or "
        "`candidates` / a Phase 7 `screener` from which targets are derived with the "
        "configured allocation formula (equal weight by default, truncated at "
        "`max_position_weight_pct`).\n\n"
        "Drift smaller than `rebalance_threshold_pct` is **suppressed** — reported as a `HOLD` "
        "with `below_threshold=true` — so rounding noise never produces a transaction. Set "
        "`drop_unlisted=true` to treat a held stock with no target as a target of zero, which "
        "proposes closing it.\n\n"
        "Sells are proposed before buys so the cash they release is available to fund them, and "
        "`apply=true` submits the plan as simulated orders. Default: propose only.\n\n"
        f"{_DISCLAIMER}"
    ),
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def rebalance_portfolio(
    payload: RebalanceRequest,
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    db: Session = Depends(get_db),
    service: PaperTradingService = Depends(get_paper_trading_service),
    construction: PortfolioConstructionService = Depends(get_portfolio_construction_service),
) -> RebalanceResponse:
    """Build, and optionally apply, a rebalancing plan."""
    portfolio = service.get_portfolio(db, portfolio_id)
    plan: RebalancePlan = construction.rebalance(
        db, portfolio, payload, payload.as_of or utcnow()
    )
    results = construction.apply_rebalance(db, portfolio, plan) if payload.apply else ()
    return _rebalance_response(plan, results)


@router.get(
    "/{portfolio_id}/risk/report",
    response_model=PortfolioRiskResponse,
    summary="Get the portfolio-level risk report",
    description=(
        "Returns the portfolio's risk posture as a whole, not as a single pre-trade check: "
        "each open position with its own stop, unrealised loss and risk-to-stop, sector "
        "concentration, portfolio exposure, position count and current drawdown — plus "
        "**recommendations** ranked by severity, one for every rule the portfolio is breaching "
        "or approaching.\n\n"
        "The report is read-only: it never changes cash, positions or P&L.\n\n"
        "Supply `prices` to mark the portfolio before measuring; otherwise the latest stored "
        "closes are used.\n\n"
        f"{_DISCLAIMER}"
    ),
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def portfolio_risk_report(
    prices: Optional[str] = Query(
        None, description="Comma-separated stock_id=price pairs used to mark open positions"
    ),
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    db: Session = Depends(get_db),
    service: PaperTradingService = Depends(get_paper_trading_service),
    construction: PortfolioConstructionService = Depends(get_portfolio_construction_service),
) -> PortfolioRiskResponse:
    """Report portfolio-level risk, position by position and sector by sector."""
    portfolio = service.get_portfolio(db, portfolio_id)
    price_map = _parse_prices(prices)
    report: PortfolioRiskReport = construction.risk_report(db, portfolio, prices=price_map)
    return _risk_report_response(db, service, portfolio, report, price_map)

# ----------------------------------------------------------------------
# Historical paper-trading engine
# ----------------------------------------------------------------------


@router.post(
    "/{portfolio_id}/simulate",
    response_model=PaperRunResponse,
    summary="Replay stored history against a paper portfolio",
    description=(
        "Steps through one stock's stored bars in chronological order, deciding from each "
        "bar's close and executing on the next bar, sizing each entry with the requested "
        "formula and recording every fill in the order and trade history.\n\n"
        "**No look-ahead:** the indicator context for bar *N* contains nothing from bar *N+1* "
        "onward, and under the `OPEN` policy the fill price is the *open* of bar *N+1* — the "
        "bar after the one that produced the decision. Every order records both "
        "`signal_date` and `executed_at` so this can be verified from the history.\n\n"
        f"{_DISCLAIMER}"
    ),
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def simulate_history(
    payload: PaperRunRequest,
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    db: Session = Depends(get_db),
    service: PaperTradingService = Depends(get_paper_trading_service),
    engine: PaperTradingEngine = Depends(get_paper_trading_engine),
) -> PaperRunResponse:
    """Replay stored market data against a paper portfolio."""
    portfolio = service.get_portfolio(db, portfolio_id)
    result = engine.run(
        db,
        portfolio,
        payload.stock_id,
        start_date=payload.start_date,
        end_date=payload.end_date,
        strategy_name=payload.strategy,
        policy=payload.execution_price_policy or default_policy(),
        sizing=payload.position_sizing,
    )
    performance = service.get_performance(
        db, portfolio_id, risk_free_rate_pct=payload.risk_free_rate_pct
    )
    positions = service.get_positions(db, portfolio_id)
    return PaperRunResponse(
        portfolio_id=result.portfolio_id,
        stock_id=result.stock_id,
        symbol=result.symbol,
        strategy=result.strategy,
        start_date=result.start_date,
        end_date=result.end_date,
        execution_price_policy=result.execution_price_policy,
        bars_processed=result.bars_processed,
        skipped_bars=result.skipped_bars,
        orders=[
            PaperRunOrderResponse(
                order_id=order.order_id,
                trade_id=order.trade_id,
                stock_id=order.stock_id,
                side=order.side,
                quantity=order.quantity,
                status=order.status,
                signal_date=order.signal_date,
                executed_at=order.executed_at,
                reference_price=order.reference_price,
                execution_price=order.execution_price,
                fees=order.fees,
                realized_pnl=order.realized_pnl,
                rejection_code=order.rejection_code,
                rejection_reason=order.rejection_reason,
                cash_after=order.cash_after,
                equity_after=order.equity_after,
            )
            for order in result.orders
        ],
        performance=_performance_response(db, service, performance, positions),
    )


# ----------------------------------------------------------------------
# Portfolio & risk management engine (Phase 13)
# ----------------------------------------------------------------------


@router.post(
    "/{portfolio_id}/allocate",
    response_model=AllocationResponse,
    summary="Allocate capital across one or more simultaneous candidate signals",
    description=(
        "The portfolio allocation workflow, over a list of one or more candidates:\n\n"
        "1. **collect** the candidates, in whatever order they were sent;\n"
        "2. **validate** the side, the entry price and the stop;\n"
        "3. **rank** by the Phase 7 `composite_score` descending, then `stock_id` ascending — "
        "the same ordering key the Phase 11 construction engine uses, so the two can never "
        "disagree about which candidate is better;\n"
        "4. **size** each one with `RISK_AND_WEIGHT`: "
        "`min(floor(risk_budget / (entry - stop)), floor(max_position_value / entry))`, "
        "capped by the cash actually available;\n"
        "5. **allocate** one at a time, re-checking every portfolio limit against the state the "
        "previous allocation actually left behind;\n"
        "6. **stop** when capital is exhausted, the exposure ceiling is reached or the position "
        "count is full.\n\n"
        "Every refused candidate is still returned, with **every** rule it broke — not only the "
        "first — so one call answers completely. A candidate the run never reached says so, which "
        "is different from one that was reached and refused.\n\n"
        "Set `policy` to replace the portfolio's limits outright for this call. **Nothing is "
        "written unless `apply=true`**, and even then every approved allocation travels through "
        "the ordinary order path, so a refusal there is reported rather than bypassed.\n\n"
        f"{_DISCLAIMER}"
    ),
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def allocate_candidates(
    payload: AllocateRequest,
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    db: Session = Depends(get_db),
    allocation: PortfolioAllocationService = Depends(get_portfolio_allocation_service),
) -> AllocationResponse:
    """Evaluate — and optionally apply — a set of candidate signals."""
    portfolio = allocation.paper.get_portfolio(db, portfolio_id)
    result, applications = allocation.allocate(db, portfolio, payload)
    return _allocation_response(result, applications, portfolio)


@router.get(
    "/{portfolio_id}/risk/state",
    response_model=PortfolioRiskStateResponse,
    summary="Get the portfolio's current risk state",
    description=(
        "Where the portfolio stands against its policy **right now**: exposure, cash weight, "
        "invested weight, per-position weight and risk-to-stop, realised and unrealised P&L, "
        "return, current drawdown, the worst drawdown over the recorded equity curve, and the "
        "sector weights that are known.\n\n"
        "`flags` is the machine-readable answer to *is any configured limit currently "
        "breached?*, raised in a fixed order so two reads of the same portfolio always report "
        "the same sequence. A flag is a statement about the portfolio, not about a proposed "
        "trade — see `POST /risk/check` for that.\n\n"
        "Read-only: nothing is mutated.\n\n"
        f"{_DISCLAIMER}"
    ),
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def portfolio_risk_state(
    prices: Optional[str] = Query(
        None, description="Comma-separated stock_id=price pairs used to mark open positions"
    ),
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    db: Session = Depends(get_db),
    allocation: PortfolioAllocationService = Depends(get_portfolio_allocation_service),
) -> PortfolioRiskStateResponse:
    """Report the portfolio's current risk posture as structured risk flags."""
    portfolio = allocation.paper.get_portfolio(db, portfolio_id)
    state = allocation.risk_state(db, portfolio, prices=_parse_prices(prices))
    return _risk_state_response(db, allocation, portfolio, state)


@router.post(
    "/{portfolio_id}/backtest",
    response_model=PortfolioBacktestResponse,
    summary="Replay several stocks against this portfolio",
    description=(
        "The Phase 8 backtesting engine applied at **portfolio** level: the same bars, the same "
        "point-in-time indicator context, the same strategy registry, the same cost model and the "
        "same chronological stepping, with several stocks competing for one cash balance, one "
        "exposure ceiling and one position count, each entry sized by the Phase 13 "
        "risk-and-weight rule.\n\n"
        "**No look-ahead.** For every trading day, in this order: fill each stock's decision "
        "taken from its *previous* bar at *this* bar's open; evaluate the resting stop against "
        "that bar's own open and low; mark every position to that bar's close; then decide from "
        "that bar's close for that stock's next bar. Sizing uses **last-known closes** for "
        "positions already held, so no entry can be sized from a price the portfolio had not yet "
        "seen. `no_look_ahead` in the response is verified from the run's own fills.\n\n"
        "**Nothing is written.** A backtest is a question about recorded history: no portfolio "
        "row, position, order, trade or snapshot is created or changed.\n\n"
        f"{_DISCLAIMER}"
    ),
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def portfolio_backtest(
    payload: PortfolioBacktestRequest,
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    db: Session = Depends(get_db),
    allocation: PortfolioAllocationService = Depends(get_portfolio_allocation_service),
) -> PortfolioBacktestResponse:
    """Replay several stocks chronologically against this portfolio's policy."""
    portfolio = allocation.paper.get_portfolio(db, portfolio_id)
    result = allocation.backtest(db, portfolio, payload)
    return _backtest_response(result, portfolio)


@router.post(
    "/{portfolio_id}/risk/position-size",
    response_model=PositionSizeResponse,
    summary="Size a candidate trade with the risk-and-weight rule",
    description=(
        "The Phase 13 sizing model, returning **every intermediate figure** that produced the "
        "quantity rather than only the answer:\n\n"
        "```\n"
        "risk_budget         = equity * max_risk_per_trade_pct / 100\n"
        "risk_per_share      = entry_price - stop_price\n"
        "quantity_by_risk    = floor(risk_budget / risk_per_share)\n"
        "max_position_value  = equity * max_position_weight_pct / 100\n"
        "quantity_by_capital = floor(max_position_value / entry_price)\n"
        "quantity            = min(quantity_by_risk, quantity_by_capital, quantity_by_cash)\n"
        "```\n\n"
        "`quantity_by_cash` is the largest whole-share quantity whose price **plus fees** fits in "
        "the available cash, which is what guarantees a sized order can always be funded.\n\n"
        "Long-only: a `stop_price` at or above the entry is refused with `INVALID_STOP_PRICE`, "
        "because a stop above the entry defines no per-share risk and therefore no defensible "
        "size. No order is placed.\n\n"
        f"{_DISCLAIMER}"
    ),
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def portfolio_position_size(
    payload: PositionSizeRequest,
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    db: Session = Depends(get_db),
    allocation: PortfolioAllocationService = Depends(get_portfolio_allocation_service),
) -> PositionSizeResponse:
    """Size one candidate trade against the portfolio's capital and risk policy."""
    portfolio = allocation.paper.get_portfolio(db, portfolio_id)
    plan = allocation.size_position(db, portfolio, payload)
    return _position_size_response(portfolio, payload, plan)


@router.post(
    "/{portfolio_id}/optimize",
    response_model=OptimizationResultSchema,
    summary="Optimise strategy-level capital allocation",
    description=(
        "The Phase 14 strategy-level capital allocator. It gathers historical performance for "
        "each strategy from persisted backtests, computes a pairwise correlation matrix, and "
        "distributes ``total_capital`` across the eligible strategies using one of four weights:\n\n"
        "- **equal_weight** — every eligible strategy receives 1/N of capital.\n"
        "- **risk_adjusted** — weights proportional to each strategy's Sharpe ratio "
        "(strategies with non-positive Sharpe receive zero).\n"
        "- **inverse_volatility** — weights proportional to 1/volatility (lower volatility, higher weight).\n"
        "- **user_weights** — the caller supplies explicit weights; the constraint solver then "
        "enforces min/max bounds and renormalises to 100%.\n\n"
        "The optimizer does **not** place orders. It returns a capital-allocation plan; each "
        "strategy's share is then turned into a sized, risk-checked portfolio by the Phase 13 "
        "allocation engine.\n\n"
        "All weights are clamped to ``[min_weight_pct, max_weight_pct]`` and redistributed so "
        "they sum to exactly 100%. Each assignment carries its eligibility status and the "
        "reasons behind the weight, so the decision is always explainable.\n\n"
        f"{_DISCLAIMER}"
    ),
    status_code=status.HTTP_200_OK,
    responses=_ERROR_RESPONSES,
)
def optimize_strategies(
    payload: OptimizeRequest,
    portfolio_id: int = Path(..., ge=1, description="Portfolio ID"),
    db: Session = Depends(get_db),
    optimization: StrategyOptimizationService = Depends(get_strategy_optimization_service),
) -> OptimizationResultSchema:
    """Distribute capital across eligible strategies using the selected weighting method."""
    from app.services.portfolio.service import paper_trading_service

    paper_trading_service.get_portfolio(db, portfolio_id)
    return optimization.optimize_to_schema(db, payload)


# ----------------------------------------------------------------------
# Internal builders
# ----------------------------------------------------------------------


def _price_map(entries: List[PortfolioPriceInput]) -> dict:
    """Build ``{stock_id: Decimal}`` from request entries, keeping the last price per stock."""
    prices: dict = {}
    for entry in entries:
        prices[entry.stock_id] = Decimal(str(entry.price))
    return prices


def _parse_prices(raw: Optional[str]) -> Dict[int, Decimal]:
    """Parse a ``stock_id=price`` query list into a price map.

    A malformed pair is a client error, not something to guess past: silently dropping a price
    would mark a position to the stored close while the caller believes it was marked to
    theirs, which is exactly the kind of quiet disagreement this API avoids elsewhere.
    """
    if not raw:
        return {}
    prices: Dict[int, Decimal] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        stock_id, separator, price = pair.partition("=")
        if not separator:
            raise InvalidOrderError(
                f"'{pair}' is not a valid price; use 'stock_id=price', for example '1=250.50'.",
                {"prices": raw},
            )
        try:
            stock_id_value = int(stock_id)
            price_value = Decimal(price)
        except (ArithmeticError, ValueError) as exc:
            raise InvalidOrderError(
                f"'{pair}' is not a valid price; both parts must be numeric.",
                {"prices": raw},
            ) from exc
        if stock_id_value < 1 or price_value <= 0:
            raise InvalidOrderError(
                f"'{pair}' is not a valid price; the id must be positive and the price above zero.",
                {"prices": raw},
            )
        prices[stock_id_value] = price_value
    return prices


def _load_metadata(raw: Optional[str]) -> Optional[dict]:
    """Parse an order's stored metadata back into a dict, or ``None`` when absent."""
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _violation_response(violation) -> RiskViolationResponse:
    """Map an engine risk violation onto its response shape."""
    return RiskViolationResponse(
        rule=violation.rule,
        reason=violation.reason,
        limit=violation.limit,
        observed=violation.observed,
        details=violation.details,
    )


def _risk_check_response(request: RiskCheckRequest, assessment) -> RiskCheckResponse:
    """Map a risk assessment onto its response, including its machine-readable code.

    Every refusal gets the code for the *actual* reason — a structural fault, a funding fault,
    or the specific configured limit that fired — rather than one blanket code, so a client can
    react to the cause without parsing the reason text. When several rules fire, the first
    reported violation determines the code and the rest stay in ``violations``.
    """
    first = assessment.violations[0] if assessment.violations else None
    return RiskCheckResponse(
        portfolio_id=assessment.portfolio_id,
        stock_id=request.stock_id,
        side=request.side,
        quantity=request.quantity,
        evaluation_price=request.requested_price,
        accepted=assessment.accepted,
        rejection_code=None if assessment.accepted else _violation_code(first),
        violations=[_violation_response(item) for item in assessment.violations],
        equity=assessment.equity,
        cash=assessment.cash,
        peak_equity=assessment.peak_equity,
        current_drawdown_pct=assessment.current_drawdown_pct,
        exposure_pct=assessment.exposure_pct,
        position_count=assessment.position_count,
        evaluated_at=utcnow(),
    )


#: Rule name (as reported in a violation's ``rule`` field) to its rejection code.
_RULE_CODES = {
    "portfolio_status": RejectionCode.PORTFOLIO_INACTIVE,
    "positive_quantity": RejectionCode.INVALID_QUANTITY,
    "positive_price": RejectionCode.INVALID_PRICE,
    "oversell_protection": RejectionCode.INSUFFICIENT_POSITION,
    # The engine's funding guard reports itself under its rule name; the reporting shape uses
    # the shorter alias. Both map to the same code.
    "insufficient_cash_protection": RejectionCode.INSUFFICIENT_CASH,
    "insufficient_cash": RejectionCode.INSUFFICIENT_CASH,
    "max_drawdown_pct": RejectionCode.MAX_DRAWDOWN_EXCEEDED,
    "max_positions": RejectionCode.MAX_POSITIONS_EXCEEDED,
    "max_position_quantity": RejectionCode.MAX_POSITION_QUANTITY_EXCEEDED,
    "max_position_value": RejectionCode.MAX_POSITION_SIZE_EXCEEDED,
    "max_position_allocation_pct": RejectionCode.MAX_POSITION_ALLOCATION_EXCEEDED,
    "max_exposure_pct": RejectionCode.MAX_EXPOSURE_EXCEEDED,
    "max_trade_allocation_pct": RejectionCode.MAX_TRADE_ALLOCATION_EXCEEDED,
    "min_cash_reserve_pct": RejectionCode.MIN_CASH_RESERVE_BREACHED,
    "min_trade_value": RejectionCode.MIN_TRADE_VALUE_BELOW,
    "max_daily_loss_pct": RejectionCode.MAX_DAILY_LOSS_EXCEEDED,
}


def _violation_code(violation) -> RejectionCode:
    """The machine-readable code for the first violation of a refused check."""
    return _RULE_CODES.get(violation.rule, RejectionCode.RISK_LIMIT_EXCEEDED)


def _timestamp_of(point) -> Optional[datetime]:
    """Timestamp of a drawdown-curve point, or ``None`` when there is no curve."""
    return point.timestamp if point is not None else None


def _portfolio_summary_view(
    db: Session, service: PaperTradingService, portfolio
) -> PortfolioSummary:
    """Portfolio summary with exposure, invested capital, peak equity and drawdown."""
    valuation = service.calculate_portfolio_value(db, portfolio.id)
    peak = Decimal(portfolio.initial_capital)
    for snapshot in service.get_snapshots(db, portfolio.id, skip=0, limit=MAX_PAGE_LIMIT):
        peak = max(peak, Decimal(snapshot.equity))
    peak = max(peak, valuation.equity)
    return PortfolioSummary(
        portfolio_id=portfolio.id,
        name=portfolio.name,
        description=portfolio.description,
        trading_mode=portfolio.trading_mode,
        status=PortfolioStatus(portfolio.status),
        base_currency=portfolio.currency,
        initial_capital=portfolio.initial_capital,
        current_cash=valuation.cash,
        market_value=valuation.market_value,
        equity=valuation.equity,
        invested_capital=valuation.invested_capital,
        exposure_pct=valuation.exposure_pct,
        realized_pnl=valuation.realized_pnl,
        unrealized_pnl=valuation.unrealized_pnl,
        total_pnl=valuation.total_pnl,
        return_pct=valuation.return_pct,
        position_count=len(valuation.positions),
        order_count=service.count_orders(db, portfolio.id),
        trade_count=service.count_trades(db, portfolio.id),
        peak_equity=peak,
        current_drawdown_pct=abs(signed_drawdown_pct(peak, valuation.equity)),
        risk_limits=_risk_config(service.risk_limits(portfolio)),
        allocation_config=_allocation_config(service.allocation_config(portfolio)),
        created_at=portfolio.created_at,
        updated_at=portfolio.updated_at,
    )


def _portfolio_summary(db: Session, service: PaperTradingService, portfolio) -> PortfolioResponse:
    """Portfolio summary assembled from a fresh valuation and live activity counts."""
    valuation = service.calculate_portfolio_value(db, portfolio.id)
    positions = service.get_positions(db, portfolio.id)
    return PortfolioResponse(
        portfolio_id=portfolio.id,
        name=portfolio.name,
        description=portfolio.description,
        trading_mode=portfolio.trading_mode,
        status=PortfolioStatus(portfolio.status),
        currency=portfolio.currency,
        initial_capital=portfolio.initial_capital,
        current_cash=valuation.cash,
        market_value=valuation.market_value,
        equity=valuation.equity,
        invested_capital=valuation.invested_capital,
        exposure_pct=valuation.exposure_pct,
        realized_pnl=valuation.realized_pnl,
        unrealized_pnl=valuation.unrealized_pnl,
        total_pnl=valuation.total_pnl,
        total_fees=valuation.total_fees,
        open_entry_fees=valuation.open_entry_fees,
        return_pct=valuation.return_pct,
        position_count=len(positions),
        order_count=service.count_orders(db, portfolio.id),
        trade_count=service.count_trades(db, portfolio.id),
        transaction_count=service.count_transactions(db, portfolio.id),
        trading_costs=_cost_config(service.trading_costs(portfolio)),
        risk_limits=_risk_config(service.risk_limits(portfolio)),
        allocation_config=_allocation_config(service.allocation_config(portfolio)),
        created_at=portfolio.created_at,
        updated_at=portfolio.updated_at,
    )


def _cost_config(costs) -> TradingCostSettings:
    return TradingCostSettings(
        commission_rate=costs.commission_rate,
        min_fee=costs.min_fee,
        slippage_rate=costs.slippage_rate,
    )


def _risk_config(limits) -> RiskLimitSettings:
    return RiskLimitSettings(
        max_position_quantity=limits.max_position_quantity,
        max_position_value=limits.max_position_value,
        max_position_allocation_pct=limits.max_position_allocation_pct,
        max_drawdown_pct=limits.max_drawdown_pct,
        max_daily_loss_pct=limits.max_daily_loss_pct,
        min_trade_value=limits.min_trade_value,
        max_exposure_pct=limits.max_exposure_pct,
        max_trade_allocation_pct=limits.max_trade_allocation_pct,
        min_cash_reserve_pct=limits.min_cash_reserve_pct,
        max_positions=limits.max_positions,
    )


def _order_response(order, symbol: Optional[str] = None) -> OrderResponse:
    return OrderResponse(
        order_id=order.id,
        portfolio_id=order.portfolio_id,
        stock_id=order.stock_id,
        symbol=symbol,
        trading_mode=order.trading_mode,
        side=order.side,
        order_type=order.order_type,
        status=order.status,
        source=order.source,
        quantity=order.quantity,
        requested_price=order.requested_price,
        execution_price=order.execution_price,
        gross_value=order.gross_value,
        fees=order.fees,
        net_cash_impact=order.net_cash_impact,
        realized_pnl=order.realized_pnl,
        rejection_code=order.rejection_code,
        rejection_reason=order.rejection_reason,
        strategy_event_id=order.strategy_event_id,
        idempotency_key=order.idempotency_key,
        signal_as_of=order.signal_as_of,
        execution_price_policy=order.execution_price_policy,
        metadata=_load_metadata(order.metadata_json),
        created_at=order.created_at,
        executed_at=order.executed_at,
    )


def _trade_response(trade, symbol: Optional[str] = None) -> TradeResponse:
    return TradeResponse(
        trade_id=trade.id,
        order_id=trade.order_id,
        portfolio_id=trade.portfolio_id,
        stock_id=trade.stock_id,
        symbol=symbol,
        side=trade.side,
        quantity=trade.quantity,
        execution_price=trade.execution_price,
        gross_value=trade.gross_value,
        fees=trade.fees,
        net_cash_impact=trade.net_cash_impact,
        realized_pnl=trade.realized_pnl,
        executed_at=trade.executed_at,
    )


def _snapshot_response(snapshot) -> PortfolioSnapshotResponse:
    """Map a persisted snapshot onto its response (``snapshot_id`` is the row id).

    Percentages are re-quantized to 4 decimals and money to 2, so the same figure always
    serialises identically whether or not a value happens to be zero.
    """
    return PortfolioSnapshotResponse(
        snapshot_id=snapshot.id,
        portfolio_id=snapshot.portfolio_id,
        timestamp=snapshot.timestamp,
        cash=quantize_money(Decimal(snapshot.cash)),
        market_value=quantize_money(Decimal(snapshot.market_value)),
        equity=quantize_money(Decimal(snapshot.equity)),
        realized_pnl=quantize_money(Decimal(snapshot.realized_pnl)),
        unrealized_pnl=quantize_money(Decimal(snapshot.unrealized_pnl)),
        total_pnl=quantize_money(Decimal(snapshot.total_pnl)),
        total_fees=quantize_money(Decimal(snapshot.total_fees)),
        return_pct=quantize_percent(Decimal(snapshot.return_pct)),
        drawdown_pct=quantize_percent(Decimal(snapshot.drawdown_pct)),
        exposure_pct=(
            None if snapshot.exposure_pct is None else quantize_percent(Decimal(snapshot.exposure_pct))
        ),
        peak_equity=quantize_money(Decimal(snapshot.peak_equity)),
    )


def _valuation_response(
    db: Session, valuation: PortfolioValuation, positions: List, as_of: datetime
) -> PortfolioValuationResponse:
    return PortfolioValuationResponse(
        portfolio_id=valuation.portfolio_id,
        as_of=as_of,
        current_cash=valuation.cash,
        market_value=valuation.market_value,
        equity=valuation.equity,
        realized_pnl=valuation.realized_pnl,
        unrealized_pnl=valuation.unrealized_pnl,
        total_pnl=valuation.total_pnl,
        total_fees=valuation.total_fees,
        open_entry_fees=valuation.open_entry_fees,
        return_pct=valuation.return_pct,
        positions=[_position_response(db, position, valuation) for position in positions],
    )


def _performance_response(
    db: Session, service: PaperTradingService, performance: PortfolioPerformance, positions: List
) -> PortfolioPerformanceResponse:
    valuation = PortfolioValuation(
        portfolio_id=performance.portfolio_id,
        initial_capital=performance.initial_capital,
        cash=performance.cash,
        market_value=performance.market_value,
        equity=performance.equity,
        realized_pnl=performance.realized_pnl,
        unrealized_pnl=performance.unrealized_pnl,
        total_pnl=performance.total_pnl,
        total_fees=performance.total_fees,
        open_entry_fees=performance.open_entry_fees,
        return_pct=performance.return_pct,
        positions=performance.positions,
    )
    return PortfolioPerformanceResponse(
        portfolio_id=performance.portfolio_id,
        name=performance.name,
        status=performance.status,
        as_of=utcnow(),
        initial_capital=performance.initial_capital,
        current_cash=performance.cash,
        market_value=performance.market_value,
        equity=performance.equity,
        realized_pnl=performance.realized_pnl,
        unrealized_pnl=performance.unrealized_pnl,
        total_pnl=performance.total_pnl,
        total_fees=performance.total_fees,
        open_entry_fees=performance.open_entry_fees,
        return_pct=performance.return_pct,
        position_count=performance.position_count,
        order_count=performance.order_count,
        filled_order_count=performance.filled_order_count,
        rejected_order_count=performance.rejected_order_count,
        trade_count=performance.trade_count,
        winning_trades=performance.winning_trades,
        losing_trades=performance.losing_trades,
        snapshot_count=performance.snapshot_count,
        peak_equity=performance.peak_equity,
        current_drawdown_pct=performance.current_drawdown_pct,
        max_drawdown_pct=performance.max_drawdown_pct,
        max_drawdown=performance.max_drawdown,
        exposure_pct=performance.exposure_pct,
        invested_capital=performance.invested_capital,
        win_rate_pct=performance.win_rate_pct,
        closed_trades=performance.closed_trades,
        average_realized_pnl=performance.average_realized_pnl,
        average_winning_trade=performance.average_winning_trade,
        average_losing_trade=performance.average_losing_trade,
        profit_factor=performance.profit_factor,
        return_observations=performance.return_observations,
        annualized_volatility_pct=performance.annualized_volatility_pct,
        sharpe_ratio=performance.sharpe_ratio,
        sortino_ratio=performance.sortino_ratio,
        cagr_pct=performance.cagr_pct,
        cagr_window_days=performance.cagr_window_days,
        max_drawdown_pct_observed_over_days=performance.max_drawdown_pct_observed_over_days,
        gross_exposure_pct=performance.gross_exposure_pct,
        net_exposure_pct=performance.net_exposure_pct,
        cash_pct=performance.cash_pct,
        invested_pct=performance.invested_pct,
        day_open_equity=performance.day_open_equity,
        daily_pnl=performance.daily_pnl,
        daily_return_pct=performance.daily_return_pct,
        insufficient_data=list(performance.insufficient_data),
        risk_free_rate_pct=performance.risk_free_rate_pct,
        currency=service.get_portfolio(db, performance.portfolio_id).currency,
        positions=[_position_response(db, position, valuation) for position in positions],
        risk_limits=_risk_config(service.risk_limits(service.get_portfolio(db, performance.portfolio_id))),
    )


def _order_execution_response(
    db: Session, service: PaperTradingService, result: OrderExecutionResult
) -> OrderExecutionResponse:
    symbols = _stock_symbols(db, [result.order.stock_id])
    portfolio = service.get_portfolio(db, result.order.portfolio_id)
    return OrderExecutionResponse(
        order=_order_response(result.order, symbols.get(result.order.stock_id)),
        trade=_trade_response(result.trade, symbols.get(result.trade.stock_id)) if result.trade else None,
        portfolio=_portfolio_summary(db, service, portfolio),
        idempotent_replay=result.idempotent_replay,
    )


def _strategy_response(
    db: Session, service: PaperTradingService, result: StrategyExecutionResult
) -> StrategyOrderResponse:
    symbols = _stock_symbols(db, [result.stock_id])
    symbol = symbols.get(result.stock_id)
    portfolio = service.get_portfolio(db, result.order.portfolio_id if result.order else _portfolio_id(result))
    order = _order_response(result.order, symbol) if result.order else None
    trade = _trade_response(result.trade, symbol) if result.trade else None
    return StrategyOrderResponse(
        portfolio_id=portfolio.id,
        stock_id=result.stock_id,
        symbol=symbol,
        strategy=result.strategy_name,
        action=result.action,
        composite_score=result.composite_score,
        signal_indicators=result.signal_indicators,
        as_of=result.as_of,
        reason=result.reason,
        order=order,
        trade=trade,
        idempotent_replay=result.idempotent_replay,
        portfolio=_portfolio_summary(db, service, portfolio),
    )


def _portfolio_id(result: StrategyExecutionResult) -> int:
    """Portfolio id of a strategy result; the valuation carries it when no order exists."""
    return result.valuation.portfolio_id


# ----------------------------------------------------------------------
# Phase 13 builders — sizing, allocation, risk state, portfolio backtest
# ----------------------------------------------------------------------


def _policy_schema(policy: PortfolioRiskPolicy) -> PortfolioRiskPolicySchema:
    """The effective policy, echoed back so a client sees the rules in force for *this* call."""
    return PortfolioRiskPolicySchema(
        max_position_weight_pct=policy.max_position_weight_pct,
        max_exposure_pct=policy.max_exposure_pct,
        max_risk_per_trade_pct=policy.max_risk_per_trade_pct,
        max_drawdown_pct=policy.max_drawdown_pct,
        max_positions=policy.max_positions,
        min_cash_reserve_pct=policy.min_cash_reserve_pct,
        max_sector_weight_pct=policy.max_sector_weight_pct,
        max_total_risk_pct=policy.max_total_risk_pct,
        stop_loss_pct=policy.stop_loss_pct,
        min_trade_value=policy.min_trade_value,
        commission_rate=policy.commission_rate,
        min_fee=policy.min_fee,
        slippage_rate=policy.slippage_rate,
    )


def _position_size_response(
    portfolio, request: PositionSizeRequest, plan: SizingBreakdown
) -> PositionSizeResponse:
    """One sizing decision with all of its arithmetic. No order is placed."""
    weight = allocation_pct(plan.capital_required, plan.equity)
    return PositionSizeResponse(
        portfolio_id=portfolio.id,
        stock_id=request.stock_id,
        method=request.method,
        entry_price=plan.entry_price,
        stop_price=plan.stop_price,
        quantity=plan.quantity,
        equity=plan.equity,
        risk_budget=plan.risk_budget,
        risk_per_share=plan.risk_per_share,
        quantity_by_risk=plan.quantity_by_risk,
        max_position_value=plan.max_position_value,
        quantity_by_capital=plan.quantity_by_capital,
        quantity_by_cash=plan.quantity_by_cash or 0,
        capital_required=plan.capital_required,
        fees=plan.fees,
        risk_amount=plan.risk_amount,
        portfolio_weight_pct=weight,
        binding_constraint=BindingConstraint(plan.binding_constraint),
        explanation=plan.explanation,
        policy=_policy_schema(policy_of(portfolio, request)),
        currency=portfolio.currency,
    )


def policy_of(portfolio, request) -> PortfolioRiskPolicy:
    """The policy a sizing call actually used.

    Resolved through the same path the service used, so the response reports the rules in force
    for *this* request rather than the portfolio's stored ones — which is what makes the echoed
    policy trustworthy when a per-request override was supplied.
    """
    from app.services.portfolio.models import as_risk_policy
    from app.services.portfolio.service import paper_trading_service

    if request.policy is not None:
        return as_risk_policy(request.policy)
    return policy_from_allocation(
        paper_trading_service.allocation_config(portfolio),
        paper_trading_service.risk_limits(portfolio),
        paper_trading_service.trading_costs(portfolio),
    )


def _allocation_response(
    result: AllocationResult, applications: List[tuple], portfolio
) -> AllocationResponse:
    """One allocation run, one decision per candidate, paired with any applied orders.

    ``applications`` is ``(decision, outcome)`` in the engine's own order, matched by identity,
    so an approved allocation the ordinary order path later refused is reported as refused rather
    than silently dropped.
    """
    applied = {id(decision): outcome for decision, outcome in applications}
    decisions: List[AllocationDecisionResponse] = []
    for decision in result.decisions:
        outcome = applied.get(id(decision))
        decisions.append(
            AllocationDecisionResponse(
                stock_id=decision.stock_id,
                symbol=decision.symbol,
                sector=decision.sector,
                action=DecisionAction.BUY if decision.approved else DecisionAction.HOLD,
                approved=decision.approved,
                quantity=decision.quantity,
                entry_price=decision.entry_price,
                stop_price=decision.stop_price,
                capital_required=decision.capital_required,
                risk_amount=decision.risk_amount,
                portfolio_weight_pct=decision.portfolio_weight_pct,
                resulting_exposure_pct=decision.resulting_exposure_pct,
                remaining_cash=decision.remaining_cash,
                binding_constraint=decision.binding_constraint,
                composite_score=decision.composite_score,
                rejection_reasons=_rejection_responses(decision),
                explanation=list(decision.explanation),
                applied=outcome is not None,
                order_id=getattr(getattr(outcome, "order", None), "id", None),
                trade_id=getattr(outcome, "trade_id", None),
            )
        )
    return AllocationResponse(
        portfolio_id=result.portfolio_id,
        status=PortfolioStatus(portfolio.status),
        as_of=result.as_of,
        currency=portfolio.currency,
        equity=result.equity,
        cash_before=result.cash_before,
        cash_after=result.cash_after,
        invested_before=result.invested_before,
        invested_after=result.invested_after,
        exposure_before_pct=result.exposure_before_pct,
        exposure_after_pct=result.exposure_after_pct,
        position_count_before=result.position_count_before,
        position_count_after=result.position_count_after,
        candidates_considered=result.candidates_considered,
        approved_count=len(result.approved),
        rejected_count=len(result.rejected),
        total_capital_committed=quantize_money(
            sum((decision.capital_required for decision in result.approved), Decimal("0"))
        ),
        total_risk_committed=quantize_money(
            sum((decision.risk_amount for decision in result.approved), Decimal("0"))
        ),
        stop_reason=result.stop_reason,
        policy=_policy_schema(result.policy),
        decisions=decisions,
        warnings=list(result.warnings),
    )


def _rejection_responses(decision) -> List[AllocationRejectionResponse]:
    """Pair each rejection code with its prose.

    The engine writes one explanation line per code, in order, so the pairing is positional. A
    decision with more codes than lines (a refusal whose detail is a sizing breakdown rather than
    one sentence per rule) still reports every code, falling back to the code's own name — a
    refusal is never reported with fewer codes than actually fired.
    """
    lines = list(decision.explanation)
    return [
        AllocationRejectionResponse(
            code=code,
            reason=lines[index] if index < len(lines) else code.value,
        )
        for index, code in enumerate(decision.rejection_reasons)
    ]


def _risk_state_response(
    db: Session, allocation: PortfolioAllocationService, portfolio, state: PortfolioRiskState
) -> PortfolioRiskStateResponse:
    """The portfolio's current risk posture, with a structured flag for each breach."""
    metadata = allocation.stock_metadata(db, [position.stock_id for position in state.positions])
    return PortfolioRiskStateResponse(
        portfolio_id=state.portfolio_id,
        status=PortfolioStatus(portfolio.status),
        as_of=state.as_of,
        currency=portfolio.currency,
        equity=state.equity,
        initial_capital=Decimal(portfolio.initial_capital),
        cash=state.cash,
        market_value=state.market_value,
        invested_capital=state.invested_capital,
        exposure_pct=state.exposure_pct,
        cash_weight_pct=state.cash_weight_pct,
        invested_weight_pct=state.invested_weight_pct,
        available_capital=state.cash,
        position_count=state.position_count,
        largest_position_weight_pct=state.largest_position_weight_pct,
        largest_position_stock_id=state.largest_position_stock_id,
        total_risk_amount=state.total_risk_amount,
        total_risk_pct=state.total_risk_pct,
        realized_pnl=state.realized_pnl,
        unrealized_pnl=state.unrealized_pnl,
        total_pnl=state.total_pnl,
        portfolio_return_pct=state.portfolio_return_pct,
        peak_equity=state.peak_equity,
        current_drawdown_pct=state.current_drawdown_pct,
        max_drawdown_pct=state.max_drawdown_pct,
        max_drawdown=state.max_drawdown,
        cash_reserve_required=state.policy.cash_reserve_required(state.equity),
        sector_weights=[
            SectorWeightResponse(sector=sector, weight_pct=weight)
            for sector, weight in state.sector_weights
        ],
        breached=state.breached,
        flags=list(state.flags),
        flag_details=[
            RiskFlagDetailResponse(
                flag=detail.flag,
                severity=RiskSeverity.BREACH,
                rule=detail.rule,
                reason=detail.reason,
                limit=detail.limit,
                observed=detail.observed,
            )
            for detail in state.flag_details
        ],
        positions=[
            PositionExposureResponse(
                stock_id=position.stock_id,
                symbol=metadata.get(position.stock_id, {}).get("symbol"),
                sector=position.sector,
                quantity=position.quantity,
                average_entry_price=position.average_entry_price,
                current_price=position.current_price,
                market_value=position.market_value,
                cost_basis=position.cost_basis,
                unrealized_pnl=position.unrealized_pnl,
                unrealized_pnl_pct=position.unrealized_pnl_pct,
                portfolio_weight_pct=position.portfolio_weight_pct,
                stop_price=position.stop_price,
                risk_amount=position.risk_amount,
            )
            for position in state.positions
        ],
        policy=_policy_schema(state.policy),
    )


def _backtest_response(result, portfolio) -> PortfolioBacktestResponse:
    """Everything one portfolio backtest produced, reusing the Phase 8 metrics engine's figures."""
    metrics = result.metrics
    return PortfolioBacktestResponse(
        portfolio_id=result.portfolio_id,
        initial_capital=result.initial_capital,
        final_equity=result.final_equity,
        ending_cash=result.ending_cash,
        ending_invested_capital=result.ending_invested_capital,
        total_pnl=result.total_pnl,
        total_return_pct=metrics.total_return_pct,
        total_fees=result.total_fees,
        number_of_trades=metrics.number_of_trades,
        winning_trades=metrics.winning_trades,
        losing_trades=metrics.losing_trades,
        win_rate_pct=metrics.win_rate_pct,
        max_drawdown=metrics.max_drawdown,
        max_drawdown_pct=metrics.max_drawdown_pct,
        peak_equity=metrics.peak_equity,
        average_position_size=result.average_position_size,
        average_exposure_pct=result.average_exposure_pct,
        maximum_exposure_pct=result.maximum_exposure_pct,
        days_processed=result.days_processed,
        rejected_signals=result.rejected_signals,
        trades=[
            BacktestTradeResponse(
                stock_id=trade.stock_id,
                side=OrderSide.BUY,
                entry_date=trade.entry_date,
                exit_date=trade.exit_date,
                quantity=trade.quantity,
                entry_price=trade.entry_price,
                exit_price=trade.exit_price,
                gross_pnl=trade.gross_pnl,
                fees=trade.commission,
                net_pnl=trade.net_pnl,
                return_pct=trade.return_pct,
                holding_period=trade.holding_period,
                exit_reason=trade.exit_reason.value,
            )
            for trade in result.trades
        ],
        equity_curve=[
            BacktestEquityPointResponse(
                date=point.date,
                cash=point.cash,
                market_value=point.market_value,
                equity=point.equity,
                exposure_pct=point.exposure_pct,
                drawdown_pct=point.drawdown_pct,
                position_count=point.position_count,
            )
            for point in result.equity_curve
        ],
        policy=_policy_schema(result.policy),
        currency=portfolio.currency,
        no_look_ahead=result.no_look_ahead,
    )


# ----------------------------------------------------------------------
# Phase 11 builders — the ledger, the decisions, the rebalance, the report
# ----------------------------------------------------------------------


def _transaction_response(entry, symbol: Optional[str] = None) -> PortfolioTransactionResponse:
    """Map one ledger row onto its response.

    ``metadata`` is stored as JSON text, so a malformed value is surfaced as ``None`` rather
    than failing the whole listing — an unparseable audit note must not make the ledger
    unreadable.
    """
    return PortfolioTransactionResponse(
        transaction_id=entry.id,
        portfolio_id=entry.portfolio_id,
        stock_id=entry.stock_id,
        symbol=symbol,
        transaction_type=TransactionType(entry.transaction_type),
        quantity=entry.quantity,
        price=_decimal_or_none(entry.price),
        fees=quantize_money(Decimal(entry.fees or 0)),
        gross_value=quantize_money(Decimal(entry.gross_value or 0)),
        net_cash_impact=quantize_money(Decimal(entry.net_cash_impact)),
        realized_pnl=quantize_money(Decimal(entry.realized_pnl or 0)),
        cash_after=quantize_money(Decimal(entry.cash_after)),
        order_id=entry.order_id,
        trade_id=entry.trade_id,
        timestamp=entry.timestamp,
        reason=entry.reason,
        metadata=_load_metadata(entry.metadata_json),
    )


def _decimal_or_none(raw) -> Optional[Decimal]:
    """Parse a nullable numeric column, keeping ``None`` as ``None``."""
    return None if raw is None else Decimal(raw)


def _cash_movement_response(
    db: Session, service: PaperTradingService, portfolio_id: int, entry
) -> CashMovementResponse:
    """Pair the appended ledger entry with the portfolio it changed.

    The portfolio is re-read rather than reused from before the movement, so the summary in the
    response is derived from the same committed write as the entry — the two can never disagree.
    """
    portfolio = service.get_portfolio(db, portfolio_id)
    symbols = _stock_symbols(db, [entry.stock_id])
    return CashMovementResponse(
        portfolio=_portfolio_summary(db, service, portfolio),
        transaction=_transaction_response(entry, symbols.get(entry.stock_id)),
    )


def _decision_response(decision, result=None) -> PortfolioDecisionResponse:
    """Map one construction decision onto its response.

    The outcome of applying a decision is carried by ``result`` — the execution the paper
    trading service returned — rather than written back onto the frozen decision object, so the
    recommendation the engine computed and the report of what happened to it stay separate.
    """
    order_id = result.order.id if result is not None and result.order is not None else None
    trade_id = result.trade.id if result is not None and result.trade is not None else None
    return PortfolioDecisionResponse(
        stock_id=decision.stock_id,
        symbol=decision.symbol,
        sector=decision.sector,
        action=decision.action,
        eligible=decision.eligible,
        current_quantity=decision.current_quantity,
        target_quantity=decision.target_quantity,
        current_weight_pct=decision.current_weight_pct,
        target_weight_pct=decision.target_weight_pct,
        weight_difference_pct=decision.weight_difference_pct,
        price=None if decision.price is None else quantize_price(decision.price),
        estimated_trade_value=quantize_money(decision.estimated_trade_value),
        risk_budget=_money_or_none(decision.risk_budget),
        risk_exposure=_money_or_none(decision.risk_exposure),
        blocked=decision.blocked,
        reasons=list(decision.reasons),
        explanation=list(decision.explanation),
        applied=result is not None,
        order_id=order_id,
        trade_id=trade_id,
    )


def _decisions_response(
    portfolio, decisions: PortfolioDecisions, results: Tuple
) -> PortfolioDecisionsResponse:
    """Assemble the full decision response, merging in what the apply pass actually achieved.

    Results are keyed by stock id so a decision and the order it produced are reported together
    even though the two are separate objects. A decision the apply pass could not fill — the
    executor refused it against live state — is reported as *not* applied, with no order id,
    rather than as applied with a null result.
    """
    applied_by_stock: Dict[int, object] = {
        decision.stock_id: result for decision, result in results if result is not None
    }
    data = [
        _decision_response(decision, applied_by_stock.get(decision.stock_id))
        for decision in decisions.decisions
    ]
    skipped = [_decision_response(item) for item in decisions.skipped]
    return PortfolioDecisionsResponse(
        portfolio_id=portfolio.id,
        status=PortfolioStatus(portfolio.status),
        as_of=decisions.as_of,
        sizing_method=decisions.sizing_method,
        equity=quantize_money(decisions.equity),
        cash=quantize_money(decisions.cash),
        invested_value=quantize_money(decisions.invested_value),
        cash_reserve_required=quantize_money(decisions.cash_reserve_required),
        candidate_count=decisions.candidate_count,
        eligible_count=decisions.eligible_count,
        skipped=skipped,
        decisions=data,
        applied=bool(applied_by_stock),
        configuration=_allocation_config(decisions.configuration),
        risk_violations=list(decisions.risk_violations),
    )


def _rebalance_response(plan: RebalancePlan, results: Tuple) -> RebalanceResponse:
    """Assemble the rebalance response, merging in what the apply pass actually achieved."""
    applied_by_stock: Dict[int, object] = {
        item.stock_id: result for item, result in results if result is not None
    }
    trades = plan.trades
    return RebalanceResponse(
        portfolio_id=plan.portfolio_id,
        status=plan.status,
        as_of=plan.as_of,
        equity=quantize_money(plan.equity),
        cash=quantize_money(plan.cash),
        invested_value=quantize_money(plan.invested_value),
        rebalance_threshold_pct=quantize_percent(plan.rebalance_threshold_pct),
        target_weights={
            str(stock_id): quantize_percent(weight) for stock_id, weight in plan.target_weights
        },
        items=[_rebalance_item_response(item, applied_by_stock.get(item.stock_id)) for item in plan.items],
        trade_count=len(trades),
        estimated_total_value=quantize_money(plan.estimated_total_value),
        estimated_total_fees=quantize_money(plan.estimated_total_fees),
        applied=bool(applied_by_stock),
        configuration=_allocation_config(plan.configuration),
    )


def _rebalance_item_response(item, result=None) -> RebalanceItemResponse:
    """Map one rebalancing verdict onto its response, tagged with what applying it achieved."""
    order = result.order if result is not None else None
    trade = result.trade if result is not None else None
    return RebalanceItemResponse(
        stock_id=item.stock_id,
        symbol=item.symbol,
        action=item.action,
        side=item.side,
        current_quantity=item.current_quantity,
        target_quantity=item.target_quantity,
        required_quantity=item.required_quantity,
        current_weight_pct=item.current_weight_pct,
        target_weight_pct=quantize_percent(item.target_weight_pct),
        weight_difference_pct=item.weight_difference_pct,
        price=None if item.price is None else quantize_price(item.price),
        estimated_trade_value=quantize_money(item.estimated_trade_value),
        estimated_fees=quantize_money(item.estimated_fees),
        below_threshold=item.below_threshold,
        blocked=item.blocked,
        reasons=list(item.reasons),
        explanation=list(item.explanation),
        applied=result is not None,
        order_id=order.id if order is not None else None,
        trade_id=trade.id if trade is not None else None,
    )


def _allocation_config(config) -> PortfolioAllocationConfig:
    """Map the engine's allocation config onto the API's representation.

    ``None`` means the engine reported no configuration — a portfolio that has never had one
    stored — so the application defaults are reported rather than a set of nulls, which is what
    the engine would have used anyway.

    Percentages are quantised here so that every percentage in every response has the same
    precision. The stored configuration keeps whatever text it was written with (``"40"``), and
    echoing that verbatim would print ``40%`` here and ``40.0000%`` in the target weight beside
    it for the same number.
    """
    if config is None:
        from app.services.portfolio.defaults import default_allocation_config

        config = default_allocation_config()
    return PortfolioAllocationConfig(
        sizing_method=config.sizing_method,
        max_position_weight_pct=quantize_percent(config.max_position_weight_pct),
        max_sector_weight_pct=_percent_or_none(config.max_sector_weight_pct),
        max_positions=config.max_positions,
        max_exposure_pct=_percent_or_none(config.max_exposure_pct),
        max_position_loss_pct=_percent_or_none(config.max_position_loss_pct),
        stop_loss_pct=_percent_or_none(config.stop_loss_pct),
        take_profit_pct=_percent_or_none(config.take_profit_pct),
        rebalance_threshold_pct=quantize_percent(config.rebalance_threshold_pct),
        min_position_value=_money_or_none(config.min_position_value),
        risk_per_trade_pct=quantize_percent(config.risk_per_trade_pct),
        cash_reserve_pct=quantize_percent(config.cash_reserve_pct),
        target_position_count=config.target_position_count,
    )


def _percent_or_none(raw) -> Optional[Decimal]:
    """Quantise a nullable percentage, keeping ``None`` as ``None``."""
    return None if raw is None else quantize_percent(Decimal(raw))


def _money_or_none(raw) -> Optional[Decimal]:
    """Quantise a nullable money amount, keeping ``None`` as ``None``."""
    return None if raw is None else quantize_money(Decimal(raw))


def _position_risk_response(db: Session, risk) -> PositionRiskResponse:
    """Map one position's risk figures onto its response."""
    return PositionRiskResponse(
        stock_id=risk.stock_id,
        symbol=_one_symbol(db, risk.stock_id, risk.symbol),
        sector=risk.sector,
        quantity=risk.quantity,
        average_entry_price=quantize_price(risk.average_entry_price),
        current_price=quantize_price(risk.current_price),
        market_value=quantize_money(risk.market_value),
        weight_pct=risk.weight_pct,
        unrealized_pnl=quantize_money(risk.unrealized_pnl),
        unrealized_pnl_pct=risk.unrealized_pnl_pct,
        stop_price=None if risk.stop_price is None else quantize_price(risk.stop_price),
        take_profit_price=(
            None if risk.take_profit_price is None else quantize_price(risk.take_profit_price)
        ),
        stop_loss_breached=risk.stop_loss_breached,
        take_profit_triggered=risk.take_profit_triggered,
        risk_amount=quantize_money(risk.risk_amount),
        risk_contribution_pct=risk.risk_contribution_pct,
    )


def _one_symbol(db: Session, stock_id: int, known: Optional[str]) -> Optional[str]:
    """Resolve one symbol, reusing a value the engine already looked up when it has one."""
    if known:
        return known
    return _stock_symbols(db, [stock_id]).get(stock_id)


def _recommendation_response(db: Session, recommendation) -> RiskRecommendationResponse:
    """Map one risk recommendation onto its response."""
    return RiskRecommendationResponse(
        stock_id=recommendation.stock_id,
        symbol=_one_symbol(db, recommendation.stock_id, recommendation.symbol),
        action=recommendation.action,
        quantity=recommendation.quantity,
        price=quantize_price(recommendation.price),
        rule=recommendation.rule,
        reason=recommendation.reason,
        details=dict(recommendation.details or {}),
    )


def _risk_report_response(
    db: Session,
    service: PaperTradingService,
    portfolio,
    report: PortfolioRiskReport,
    prices: Optional[Dict[int, Decimal]] = None,
) -> PortfolioRiskResponse:
    """Assemble the full portfolio risk report from the engine's output.

    The pre-trade half of the response is taken from :meth:`effective_risk` — the same source
    ``GET /risk`` reads — so the two endpoints can never disagree about cash, equity, exposure
    or which limits fired. The Phase 11 half comes from the risk engine's own report, which was
    marked at the same prices.

    ``violations`` is the **union** of both: the pre-trade service knows the order-level and
    portfolio-level limits, while the Phase 11 engine knows the allocation rules it alone
    evaluates — the sector cap, the single-position cap, the exposure and position-count caps, the
    cash reserve and the drawdown guard. Reporting only either half would hide exactly the rules
    that endpoint exists to surface. They are merged by rule name, with the pre-trade entry first,
    so the response stays deterministic.

    A crossed stop-loss or take-profit is **not** a violation, even though the engine evaluates
    both: those are levels to *act* at rather than limits the portfolio is outside of, so they are
    reported as ``recommendations`` with a quantity. A portfolio whose stop has been hit is not
    "in breach" — it has an exit to make, and the caller decides whether to apply it.

    ``exposure_pct`` is the pre-trade field: for a long-only portfolio it equals the report's
    ``gross_exposure_pct`` and ``net_exposure_pct``, and it is read from the pre-trade side so it
    keeps that field's definition if shorts are ever added.
    """
    state = service.effective_risk(db, portfolio.id, prices=prices)
    valuation = state["valuation"]
    violations = _merged_violations(state["violations"], report.violations)
    return PortfolioRiskResponse(
        portfolio_id=portfolio.id,
        status=PortfolioStatus(portfolio.status),
        equity=quantize_money(report.equity),
        cash=quantize_money(report.cash),
        cash_reserve_required=quantize_money(report.cash_reserve_required),
        market_value=quantize_money(report.market_value),
        exposure_pct=valuation.exposure_pct,
        invested_capital=quantize_money(report.invested_capital),
        position_count=report.position_count,
        max_positions=report.max_positions,
        peak_equity=quantize_money(report.peak_equity),
        current_drawdown_pct=quantize_percent(report.current_drawdown_pct),
        drawdown_limit_pct=report.drawdown_limit_pct,
        drawdown_breached=report.drawdown_breached,
        risk_limits=_risk_config(service.risk_limits(portfolio)),
        violations=[_violation_response(item) for item in violations],
        allocation_config=_allocation_config(report.configuration),
        gross_exposure_pct=report.gross_exposure_pct,
        net_exposure_pct=report.net_exposure_pct,
        cash_pct=report.cash_pct,
        invested_pct=report.invested_pct,
        top_position_weight_pct=report.top_position_weight_pct,
        top_positions_weight_pct=report.top_positions_weight_pct,
        sector_weights={
            sector: quantize_percent(weight)
            for sector, weight in report.sector_weight_map.items()
        },
        total_risk_amount=quantize_money(report.total_risk_amount),
        position_risks=[_position_risk_response(db, risk) for risk in report.position_risks],
        recommendations=[
            _recommendation_response(db, item) for item in report.recommendations
        ],
        as_of=report.as_of or utcnow(),
    )


def _merged_violations(primary, secondary):
    """Union two violation sequences by rule name, keeping the first occurrence of each.

    Both sides can report the same rule — ``max_positions`` is both an order-level limit and a
    portfolio-level one — and a caller seeing it twice would read two problems where there is
    one. The pre-trade entry wins because it carries the limit as configured.
    """
    merged = []
    seen = set()
    for violation in tuple(primary) + tuple(secondary):
        if violation.rule in seen:
            continue
        seen.add(violation.rule)
        merged.append(violation)
    return merged


__all__: List[str] = ["router"]
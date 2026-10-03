"""Phase 9 paper-trading service — the only layer that writes portfolio state.

Responsibilities, in order of an order submission:

1. load the portfolio and the stock, rejecting unknown ids;
2. honour idempotency and strategy-event duplicate protection;
3. build the runtime account state and hand the order to the pure
   :class:`~app.services.portfolio.executor.OrderExecutor`;
4. persist the outcome **atomically** — order, trade, position, cash and portfolio
   aggregates in one transaction, or nothing at all;
5. raise a clear domain error for a rejected order while keeping the rejection on record.

The service contains no HTTP concerns, no SQL of its own (all access goes through the
repositories) and no duplicated financial logic (all money math lives in
:mod:`app.services.portfolio.calculator`).

**Paper trading only.** Nothing here connects to a brokerage, stores credentials or sends
an order anywhere; orders are simulated locally against the price the caller supplies.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from app.core.config import default_currency, settings
from app.core.exceptions import (
    AppException,
    InsufficientCashError,
    InsufficientCashForWithdrawalError,
    InsufficientPositionError,
    InvalidAllocationConfigError,
    InvalidCashMovementError,
    OrderNotExecutableError,
    InvalidOrderError,
    OrderNotFoundError,
    PortfolioInactiveError,
    PositionNotFoundError,
    PortfolioNotFoundError,
    RiskLimitExceededError,
    StockNotFoundError,
)
from app.core.logging import logger
from app.models.portfolio import (
    PAPER_TRADING_MODE,
    Portfolio,
    PortfolioOrder,
    PortfolioPosition,
    PortfolioSnapshot,
    PortfolioTrade,
    PortfolioTransaction,
)
from app.repositories.order_repository import order_repository
from app.repositories.portfolio_transaction_repository import (
    portfolio_transaction_repository,
)
from app.repositories.price_repository import price_repository
from app.repositories.portfolio_repository import portfolio_repository
from app.repositories.portfolio_snapshot_repository import portfolio_snapshot_repository
from app.repositories.position_repository import position_repository
from app.repositories.stock_repository import stock_repository
from app.repositories.trade_repository import trade_repository
from app.schemas.allocation import CashMovementRequest
from app.schemas.portfolio import (
    ExecutionPricePolicy,
    PortfolioUpdate,
    PositionSizingRequest,
    RiskCheckRequest,
    OrderCreate,
    OrderSide,
    OrderSource,
    OrderStatus,
    OrderType,
    PortfolioCreate,
    PortfolioStatus,
    PortfolioStatusUpdate,
    RejectionCode,
    StrategyOrderRequest,
    TransactionType,
)
from app.services.backtest.metrics import max_drawdown
from app.services.backtest.models import EquityPoint, quantize_money, quantize_percent
from app.services.backtest.strategy import STRATEGY_NAME
from app.services.portfolio.calculator import (
    apply_deposit,
    apply_withdrawal,
    cost_basis,
    drawdown_pct,
    equity,
    market_value,
    quantize_price,
    return_pct,
    signed_drawdown_pct,
    sum_market_value,
    sum_open_entry_fees,
    sum_unrealized_pnl,
    total_pnl,
    unrealized_pnl,
    value_position,
)
from app.services.portfolio.defaults import (
    default_allocation_config,
    default_risk_limits,
    default_trading_cost_config,
)
from app.services.portfolio.executor import OrderExecutor
from app.services.portfolio.models import (
    ZERO,
    AllocationConfig,
    ExecutionPlan,
    OrderIntent,
    PaperRunOrder,
    PortfolioAccount,
    PortfolioPerformance,
    PortfolioValuation,
    PositionState,
    RiskLimits,
    TradingCostConfig,
    allocation_config_from_dict,
    allocation_config_to_dict,
    as_allocation_config,
    as_risk_limits,
    as_trading_cost_config,
)
from app.services.portfolio.performance_service import PortfolioPerformanceService
from app.services.portfolio.position_sizing_service import position_sizing_service
from app.services.portfolio.risk import peak_equity
from app.services.portfolio.risk_service import day_open_equity, risk_check_service
from app.services.portfolio.strategy_bridge import PaperStrategyBridge
from app.services.portfolio.valuation_service import (
    PortfolioValuationService,
    position_state,
)

DEFAULT_PAGE_LIMIT = 100
MAX_PAGE_LIMIT = 1000


class OrderExecutionResult:
    """Outcome of one order submission, including the resulting portfolio state."""

    def __init__(
        self,
        order: PortfolioOrder,
        trade: Optional[PortfolioTrade],
        valuation: PortfolioValuation,
        idempotent_replay: bool = False,
    ):
        self.order = order
        self.trade = trade
        self.valuation = valuation
        self.idempotent_replay = idempotent_replay


class StrategyExecutionResult:
    """Outcome of running the Phase 7/8 strategy rules against a paper portfolio."""

    def __init__(
        self,
        stock_id: int,
        action: str,
        composite_score: int,
        signal_indicators: Sequence[str],
        as_of: Optional[str],
        reason: str,
        strategy_name: str,
        order: Optional[PortfolioOrder],
        trade: Optional[PortfolioTrade],
        valuation: PortfolioValuation,
        idempotent_replay: bool = False,
    ):
        self.stock_id = stock_id
        self.action = action
        self.composite_score = composite_score
        self.signal_indicators = list(signal_indicators)
        self.as_of = as_of
        self.reason = reason
        self.strategy_name = strategy_name
        self.order = order
        self.trade = trade
        self.valuation = valuation
        self.idempotent_replay = idempotent_replay


class PaperTradingService:
    """Coordinates virtual portfolios, simulated orders and simulated executions."""

    def __init__(
        self,
        portfolio_repo=portfolio_repository,
        position_repo=position_repository,
        order_repo=order_repository,
        trade_repo=trade_repository,
        snapshot_repo=portfolio_snapshot_repository,
        stock_repo=stock_repository,
        bridge: Optional[PaperStrategyBridge] = None,
        price_repo=price_repository,
        valuations: Optional[PortfolioValuationService] = None,
        performance: Optional[PortfolioPerformanceService] = None,
        transaction_repo=portfolio_transaction_repository,
    ):
        self.portfolio_repo = portfolio_repo
        self.position_repo = position_repo
        self.order_repo = order_repo
        self.trade_repo = trade_repo
        self.snapshot_repo = snapshot_repo
        self.transaction_repo = transaction_repo
        self.stock_repo = stock_repo
        self.price_repo = price_repo
        self.bridge = bridge or PaperStrategyBridge(stock_repo=stock_repo)
        self.valuations = valuations or PortfolioValuationService(
            position_repo=position_repo, trade_repo=trade_repo
        )
        self.performance = performance or PortfolioPerformanceService(
            order_repo=order_repo, trade_repo=trade_repo, snapshot_repo=snapshot_repo
        )
        self.risk = risk_check_service
        self.sizer = position_sizing_service

    # ------------------------------------------------------------------
    # Portfolio lifecycle
    # ------------------------------------------------------------------

    def create_portfolio(self, db: Session, request: PortfolioCreate) -> Portfolio:
        """Create a virtual portfolio.

        A new portfolio starts flat: cash and equity both equal ``initial_capital`` and no
        positions exist. The currency is fixed here and never changed afterwards — every later
        figure is recorded in it, so changing it would silently relabel the portfolio's whole
        recorded history.
        """
        costs = as_trading_cost_config(request.trading_costs)
        limits = as_risk_limits(request.risk_limits)
        allocation = as_allocation_config(request.allocation_config)
        costs.validate()
        limits.validate()
        allocation.validate()

        portfolio = Portfolio(
            name=request.name,
            description=request.description,
            trading_mode=PAPER_TRADING_MODE,
            status=PortfolioStatus.ACTIVE.value,
            currency=(request.currency or default_currency()).upper(),
            initial_capital=quantize_money(Decimal(str(request.initial_capital))),
            current_cash=quantize_money(Decimal(str(request.initial_capital))),
            realized_pnl=quantize_money(ZERO),
            unrealized_pnl=quantize_money(ZERO),
            total_pnl=quantize_money(ZERO),
            equity=quantize_money(Decimal(str(request.initial_capital))),
            cost_config_json=json.dumps(_costs_to_dict(costs), sort_keys=True),
            risk_config_json=json.dumps(_limits_to_dict(limits), sort_keys=True),
            allocation_config_json=json.dumps(
                allocation_config_to_dict(allocation), sort_keys=True
            ),
        )
        self.portfolio_repo.add(db, portfolio)
        db.commit()
        db.refresh(portfolio)
        logger.info(
            "Paper portfolio created: id=%s name='%s' initial_capital=%s mode=%s "
            "sizing_method=%s max_position_weight=%s",
            portfolio.id,
            portfolio.name,
            portfolio.initial_capital,
            PAPER_TRADING_MODE,
            allocation.sizing_method.value,
            allocation.max_position_weight_pct,
        )
        return portfolio

    def get_portfolio(self, db: Session, portfolio_id: int) -> Portfolio:
        """Load a portfolio or raise ``PortfolioNotFoundError``."""
        portfolio = self.portfolio_repo.get_by_id(db, portfolio_id)
        if not portfolio:
            raise PortfolioNotFoundError(portfolio_id)
        return portfolio

    def _lock_portfolio(self, db: Session, portfolio_id: int) -> Portfolio:
        """Load a portfolio *and* lock its row for the rest of the transaction.

        Used by every path that is about to change cash or positions. Two concurrent buys then
        queue on this row instead of both committing against the same balance; a rejected or
        failed order releases the lock when its transaction rolls back.
        """
        portfolio = self.portfolio_repo.get_by_id_for_update(db, portfolio_id)
        if not portfolio:
            raise PortfolioNotFoundError(portfolio_id)
        return portfolio

    def pause_portfolio(self, db: Session, portfolio_id: int) -> Portfolio:
        """Stop accepting new orders, keeping every position and every record.

        A paused portfolio can be resumed, and nothing is deleted or closed: the historical
        ledger simply stops moving. Reading it, valuing it and recording snapshots all still
        work, which is what makes a pause a pause rather than a close.
        """
        portfolio = self._lock_portfolio(db, portfolio_id)
        if portfolio.status == PortfolioStatus.CLOSED.value:
            raise PortfolioInactiveError(portfolio_id, PortfolioStatus.CLOSED.value)
        return self.update_portfolio(
            db, portfolio_id, PortfolioUpdate(status=PortfolioStatus.PAUSED)
        )

    def close_portfolio(self, db: Session, portfolio_id: int) -> Portfolio:
        """Permanently retire a portfolio.

        Closing is terminal and destructive only of the portfolio's *ability to trade*: its
        positions, orders, trades, snapshots and cash ledger are all retained, so the result it
        produced stays readable and auditable forever. Closing an already-closed portfolio is a
        no-op rather than an error, so a retried request is safe.
        """
        portfolio = self._lock_portfolio(db, portfolio_id)
        if portfolio.status == PortfolioStatus.CLOSED.value:
            return portfolio
        return self.update_portfolio(
            db, portfolio_id, PortfolioUpdate(status=PortfolioStatus.CLOSED)
        )

    def list_portfolios(self, db: Session, skip: int = 0, limit: int = DEFAULT_PAGE_LIMIT) -> List[Portfolio]:
        """Portfolios, newest first."""
        limit = min(max(limit, 1), MAX_PAGE_LIMIT)
        return self.portfolio_repo.list_all(db, skip=skip, limit=limit)

    def update_status(self, db: Session, portfolio_id: int, request: PortfolioStatusUpdate) -> Portfolio:
        """Move a portfolio between ACTIVE, PAUSED and CLOSED.

        ``CLOSED`` is terminal: it cannot be reactivated, because reopening a closed virtual
        portfolio would silently rewrite its recorded P&L history.
        """
        return self.update_portfolio(db, portfolio_id, PortfolioUpdate(status=request.status))

    def update_portfolio(
        self, db: Session, portfolio_id: int, request: PortfolioUpdate
    ) -> Portfolio:
        """Partially update a portfolio's name, description, status, limits or allocation policy.

        Only the fields actually supplied change; ``None`` means "leave as it is", so an empty
        body is a valid no-op. ``CLOSED`` remains terminal for the same reason as in
        :meth:`update_status`.

        ``risk_limits`` and ``allocation_config`` each replace the stored set rather than merging
        into it: a caller that sends one limit or one policy field is stating the whole
        configuration, which is the only reading that cannot silently leave a stale rule in force.
        """
        portfolio = self.get_portfolio(db, portfolio_id)
        current = PortfolioStatus(portfolio.status)
        if request.status is not None and request.status is not current:
            if current is PortfolioStatus.CLOSED and request.status is not PortfolioStatus.CLOSED:
                raise InvalidOrderError(
                    f"Portfolio '{portfolio_id}' is CLOSED and cannot be reopened."
                )
            portfolio.status = request.status.value

        updates: dict = {}
        if request.name is not None:
            updates["name"] = request.name
        if request.description is not None:
            updates["description"] = request.description
        if request.risk_limits is not None:
            updates["risk_config_json"] = json.dumps(
                _limits_to_dict(as_risk_limits(request.risk_limits)), sort_keys=True
            )
        if request.allocation_config is not None:
            allocation = as_allocation_config(request.allocation_config)
            allocation.validate()
            updates["allocation_config_json"] = json.dumps(
                allocation_config_to_dict(allocation), sort_keys=True
            )
        if updates:
            self.portfolio_repo.update(db, portfolio, updates)
        db.commit()
        db.refresh(portfolio)
        logger.info(
            "Paper portfolio %s updated: status=%s fields=%s",
            portfolio_id,
            portfolio.status,
            sorted(updates) or "none",
        )
        return portfolio

    def get_positions(self, db: Session, portfolio_id: int) -> List[PortfolioPosition]:
        """Open positions of a portfolio, ordered by stock."""
        self.get_portfolio(db, portfolio_id)
        return self.position_repo.list_open(db, portfolio_id)

    # ------------------------------------------------------------------
    # Capital and cash management (Phase 11)
    # ------------------------------------------------------------------

    @staticmethod
    def allocation_config(portfolio: Portfolio) -> AllocationConfig:
        """The portfolio's construction policy, or the application defaults.

        A portfolio created before Phase 11 has no stored policy; it falls back to the
        application settings rather than to a configuration invented here, so the same
        deployment produces the same defaults everywhere.
        """
        config = allocation_config_from_dict(_load_json(portfolio.allocation_config_json))
        if config is None:
            config = default_allocation_config()
        try:
            config.validate()
        except ValueError as exc:
            raise InvalidAllocationConfigError(
                f"Portfolio '{portfolio.id}' has an unusable allocation configuration: {exc}",
                {"portfolio_id": portfolio.id},
            ) from exc
        return config

    def deposit(
        self, db: Session, portfolio_id: int, request: CashMovementRequest
    ) -> PortfolioTransaction:
        """Add capital to a portfolio's cash balance.

        A deposit moves no stock and books no P&L; it changes cash — and therefore equity — by
        exactly the amount deposited, and it is recorded in the immutable ledger. ``PAUSED``
        portfolios accept deposits and withdrawals, because pausing stops *trading*, not
        accounting; a ``CLOSED`` portfolio accepts nothing.
        """
        portfolio = self.get_portfolio(db, portfolio_id)
        self._require_open_for_cash(portfolio)
        try:
            movement = apply_deposit(Decimal(portfolio.current_cash), Decimal(request.amount))
        except ValueError as exc:
            raise InvalidCashMovementError(str(exc), {"amount": str(request.amount)}) from exc

        return self._record_cash_movement(
            db,
            portfolio,
            movement,
            request.as_of or utcnow(),
            request.reason or "Portfolio deposit",
        )

    def withdraw(
        self, db: Session, portfolio_id: int, request: CashMovementRequest
    ) -> PortfolioTransaction:
        """Take capital out of a portfolio's cash balance.

        A withdrawal is refused unless it is strictly positive and fully covered by the cash on
        hand. Overdrawing is not permitted: a negative cash balance would make equity, exposure
        and every weight measured against it meaningless, so the request is rejected with the
        available balance rather than partially filled.
        """
        portfolio = self.get_portfolio(db, portfolio_id)
        self._require_open_for_cash(portfolio)
        requested = Decimal(request.amount)
        available = Decimal(portfolio.current_cash)
        try:
            movement = apply_withdrawal(available, requested)
        except ValueError as exc:
            if "exceeds the available cash" in str(exc):
                raise InsufficientCashForWithdrawalError(
                    requested, available, {"portfolio_id": portfolio_id}
                ) from exc
            raise InvalidCashMovementError(str(exc), {"amount": str(request.amount)}) from exc

        return self._record_cash_movement(
            db,
            portfolio,
            movement,
            request.as_of or utcnow(),
            request.reason or "Portfolio withdrawal",
        )

    def list_transactions(
        self,
        db: Session,
        portfolio_id: int,
        skip: int = 0,
        limit: int = DEFAULT_PAGE_LIMIT,
        stock_id: Optional[int] = None,
        transaction_type: Optional[TransactionType] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> List[PortfolioTransaction]:
        """The transaction ledger, newest first. Entries are never modified."""
        self.get_portfolio(db, portfolio_id)
        return self.transaction_repo.list_history(
            db,
            portfolio_id,
            skip=skip,
            limit=limit,
            stock_id=stock_id,
            transaction_type=transaction_type.value if transaction_type else None,
            start_date=start_date,
            end_date=end_date,
        )

    def count_transactions(
        self,
        db: Session,
        portfolio_id: int,
        stock_id: Optional[int] = None,
        transaction_type: Optional[TransactionType] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> int:
        """Total ledger entries matching the same filters as :meth:`list_transactions`."""
        return self.transaction_repo.count(
            db,
            portfolio_id,
            stock_id=stock_id,
            transaction_type=transaction_type.value if transaction_type else None,
            start_date=start_date,
            end_date=end_date,
        )

    def transaction_counts(self, db: Session, portfolio_id: int) -> Dict[str, int]:
        """Ledger entry count per transaction type."""
        return self.transaction_repo.count_by_type(db, portfolio_id)

    def ledger_balance(self, db: Session, portfolio: Portfolio) -> Decimal:
        """Cash implied by replaying the ledger from the initial capital.

        Kept as a public method because it is the invariant that makes the ledger worth having:
        ``initial_capital + sum(net_cash_impact)`` must equal ``current_cash`` for every
        portfolio, and a drift between the two would mean some cash movement was not recorded.
        """
        return quantize_money(
            Decimal(portfolio.initial_capital)
            + self.transaction_repo.net_cash_impact(db, portfolio.id)
        )

    def _record_cash_movement(
        self,
        db: Session,
        portfolio: Portfolio,
        movement,
        as_of: datetime,
        reason: str,
    ) -> PortfolioTransaction:
        """Persist a deposit or withdrawal: cash, derived aggregates and the ledger entry.

        The ledger entry is written in the same transaction as the cash movement, so a movement
        can never exist without its record.
        """
        portfolio.current_cash = movement.cash_after
        db.flush()

        # Equity and P&L follow cash: a deposit raises equity by exactly the amount and books no
        # P&L, so the derived figures are recomputed rather than adjusted.
        valuation = self._valuation(db, portfolio, None)
        self._persist_valuation(db, portfolio, valuation)

        entry = PortfolioTransaction(
            portfolio_id=portfolio.id,
            transaction_type=movement.movement_type,
            stock_id=None,
            quantity=None,
            price=None,
            fees=movement.fees,
            gross_value=movement.gross_value,
            net_cash_impact=movement.net_cash_impact,
            realized_pnl=quantize_money(ZERO),
            cash_after=movement.cash_after,
            timestamp=as_of,
            reason=reason,
        )
        self.transaction_repo.add(db, entry)
        db.commit()
        db.refresh(entry)

        logger.info(
            "Portfolio %s %s: amount=%s cash=%s -> %s equity=%s",
            portfolio.id,
            movement.movement_type,
            movement.amount,
            movement.cash_before,
            movement.cash_after,
            valuation.equity,
        )
        return entry

    @staticmethod
    def _require_open_for_cash(portfolio: Portfolio) -> None:
        """Refuse a capital movement on a ``CLOSED`` portfolio.

        A closed portfolio's recorded history is final; letting its cash move afterwards would
        rewrite the very history the closure froze.
        """
        if portfolio.status == PortfolioStatus.CLOSED.value:
            raise PortfolioInactiveError(portfolio.id, portfolio.status)

    # ------------------------------------------------------------------
    # Valuation, P&L and snapshots
    # ------------------------------------------------------------------

    def calculate_portfolio_value(
        self,
        db: Session,
        portfolio_id: int,
        prices: Optional[Dict[int, Decimal]] = None,
    ) -> PortfolioValuation:
        """Compute the portfolio's current value without changing any state.

        ``prices`` overrides the mark of specific stocks for this reading only.
        """
        portfolio = self.get_portfolio(db, portfolio_id)
        return self._valuation(db, portfolio, prices)

    def update_market_prices(
        self,
        db: Session,
        portfolio_id: int,
        prices: Dict[int, Decimal],
        as_of: Optional[datetime] = None,
    ) -> PortfolioValuation:
        """Mark open positions to new prices.

        Valuation only: it updates position marks and the derived portfolio figures and never
        creates an order, a trade or any cash movement.
        """
        portfolio = self.get_portfolio(db, portfolio_id)
        valuation = self._valuation(db, portfolio, prices)
        self._persist_valuation(db, portfolio, valuation)
        self._apply_position_marks(db, portfolio, valuation)
        db.commit()
        logger.info(
            "Portfolio %s valued as of %s: cash=%s market_value=%s equity=%s unrealized_pnl=%s",
            portfolio_id,
            (as_of or utcnow()).isoformat(),
            valuation.cash,
            valuation.market_value,
            valuation.equity,
            valuation.unrealized_pnl,
        )
        return valuation

    def create_snapshot(
        self,
        db: Session,
        portfolio_id: int,
        prices: Optional[Dict[int, Decimal]] = None,
        as_of: Optional[datetime] = None,
    ) -> PortfolioSnapshot:
        """Capture one equity-curve point, optionally after applying new prices.

        Snapshots are created on request; Phase 10 runs no background scheduler. Each point
        stores its own drawdown, peak and exposure so the equity curve, the drawdown curve and
        the risk engine all read the same numbers rather than each re-deriving them.
        """
        portfolio = self.get_portfolio(db, portfolio_id)
        valuation = self._valuation(db, portfolio, prices)
        self._persist_valuation(db, portfolio, valuation)
        self._apply_position_marks(db, portfolio, valuation)

        snapshot = self._build_snapshot(db, portfolio, valuation, as_of or utcnow())
        self.snapshot_repo.add(db, snapshot)
        db.commit()
        db.refresh(snapshot)
        logger.info(
            "Portfolio %s snapshot created: equity=%s total_pnl=%s return_pct=%s "
            "drawdown_pct=%s exposure_pct=%s",
            portfolio_id,
            snapshot.equity,
            snapshot.total_pnl,
            snapshot.return_pct,
            snapshot.drawdown_pct,
            snapshot.exposure_pct,
        )
        return snapshot

    def record_equity_point(
        self,
        db: Session,
        portfolio: Portfolio,
        prices: Dict[int, Decimal],
        as_of: datetime,
    ) -> PortfolioSnapshot:
        """Capture an equity-curve point as part of a larger operation.

        Used by the paper-trading engine, which marks positions and records the point as two
        steps of the same walk through history. It shares every code path with
        :meth:`create_snapshot`, including the running peak, so an engine-driven curve is
        identical to one captured through the API.
        """
        valuation = self._valuation(db, portfolio, prices)
        self._persist_valuation(db, portfolio, valuation)
        self._apply_position_marks(db, portfolio, valuation)
        snapshot = self._build_snapshot(db, portfolio, valuation, as_of)
        self.snapshot_repo.add(db, snapshot)
        db.flush()
        return snapshot

    def _build_snapshot(
        self,
        db: Session,
        portfolio: Portfolio,
        valuation: PortfolioValuation,
        as_of: datetime,
    ) -> PortfolioSnapshot:
        """Build the snapshot row, including its running peak, drawdown and exposure.

        The running peak includes the initial capital, so a portfolio that opens below its
        starting capital reports a real drawdown on its very first snapshot instead of zero.
        """
        running_peak = peak_equity(
            [
                Decimal(portfolio.initial_capital),
                valuation.equity,
                self.snapshot_repo.max_equity(db, portfolio.id),
            ]
        )
        return PortfolioSnapshot(
            portfolio_id=portfolio.id,
            timestamp=as_of,
            cash=valuation.cash,
            market_value=valuation.market_value,
            equity=valuation.equity,
            realized_pnl=valuation.realized_pnl,
            unrealized_pnl=valuation.unrealized_pnl,
            total_pnl=valuation.total_pnl,
            total_fees=valuation.total_fees,
            return_pct=valuation.return_pct,
            drawdown_pct=signed_drawdown_pct(running_peak, valuation.equity),
            exposure_pct=valuation.exposure_pct if valuation.exposure_pct is not None else ZERO,
            peak_equity=running_peak,
        )

    def get_snapshots(
        self,
        db: Session,
        portfolio_id: int,
        skip: int = 0,
        limit: int = DEFAULT_PAGE_LIMIT,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> List[PortfolioSnapshot]:
        """Equity curve, oldest first, optionally bounded by timestamp."""
        self.get_portfolio(db, portfolio_id)
        limit = min(max(limit, 1), MAX_PAGE_LIMIT)
        return self.snapshot_repo.list_curve(
            db,
            portfolio_id,
            skip=skip,
            limit=limit,
            start_date=start_date,
            end_date=end_date,
        )

    def count_snapshots(
        self,
        db: Session,
        portfolio_id: int,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> int:
        return self.snapshot_repo.count(
            db, portfolio_id, start_date=start_date, end_date=end_date
        )

    def get_performance(
        self, db: Session, portfolio_id: int, risk_free_rate_pct: Optional[Decimal] = None
    ) -> PortfolioPerformance:
        """Portfolio-level performance summary, including drawdown and activity counts."""
        portfolio = self.get_portfolio(db, portfolio_id)
        valuation = self._valuation(db, portfolio, None)
        return self.performance.performance(db, portfolio, valuation, self._risk_free(risk_free_rate_pct))

    def get_drawdown(self, db: Session, portfolio_id: int):
        """The drawdown curve plus its extremes, oldest point first.

        Percentages are returned at 4-decimal precision even when they are zero, so a caller
        parsing the response never has to special-case the magnitude of a decline.
        """
        portfolio = self.get_portfolio(db, portfolio_id)
        points = self.performance.drawdown_curve(db, portfolio)
        peak, amount, pct, peak_point, trough_point = self.performance.drawdown_extremes(points)
        return {
            "portfolio_id": portfolio.id,
            "peak_equity": quantize_money(peak),
            "current_equity": quantize_money(
                points[-1].equity if points else Decimal(portfolio.initial_capital)
            ),
            "points": points,
            "max_drawdown": quantize_money(amount),
            "max_drawdown_pct": quantize_percent(pct),
            "peak_point": peak_point,
            "trough_point": trough_point,
        }

    def check_risk(self, db: Session, portfolio_id: int, request: RiskCheckRequest):
        """Would the risk engine accept this hypothetical trade?

        Nothing is persisted: the portfolio, its positions and its cash are untouched, which
        is what makes it safe to ask before committing to an order.
        """
        portfolio = self.get_portfolio(db, portfolio_id)
        stock = self.stock_repo.get_by_id(db, request.stock_id)
        if not stock:
            raise StockNotFoundError(request.stock_id)
        price = self._risk_check_price(db, request)
        return self.risk.check(
            db,
            portfolio,
            stock_id=request.stock_id,
            side=request.side,
            quantity=request.quantity,
            price=price,
            costs=self.trading_costs(portfolio),
            limits=self.risk_limits(portfolio),
            as_of=request.as_of,
        )

    def effective_risk(
        self, db: Session, portfolio_id: int, prices: Optional[Dict[int, Decimal]] = None
    ):
        """The portfolio's configured risk limits and the rules it currently breaches.

        ``prices`` marks the portfolio to a caller-supplied price map before the rules are
        evaluated, so a report and a valuation taken at the same prices cannot disagree. Omit it
        to mark to the latest stored closes.
        """
        portfolio = self.get_portfolio(db, portfolio_id)
        limits = self.risk_limits(portfolio)
        return self.risk.effective_limits(db, portfolio, limits, prices=prices)

    def size_position(
        self,
        db: Session,
        portfolio_id: int,
        request: PositionSizingRequest,
        price: Optional[Decimal] = None,
    ):
        """Derive the quantity an order would have, without placing one."""
        portfolio = self.get_portfolio(db, portfolio_id)
        valuation = self._valuation(db, portfolio, None)
        return self.sizer.size(
            db,
            equity=valuation.equity,
            available_cash=valuation.cash,
            costs=self.trading_costs(portfolio),
            request=request,
            price=price,
        )

    def _risk_check_price(self, db: Session, request: RiskCheckRequest) -> Decimal:
        """The price a risk check is evaluated at: explicit, or the latest stored close."""
        if request.requested_price is not None:
            return quantize_price(Decimal(request.requested_price))
        latest = self.price_repo.get_latest(db, request.stock_id)
        if latest is None or latest.close_price is None:
            raise InvalidOrderError(
                f"Stock '{request.stock_id}' has no stored price to evaluate the trade "
                "against; supply an explicit requested_price.",
                {"stock_id": request.stock_id},
            )
        return quantize_price(Decimal(str(latest.close_price)))

    @staticmethod
    def _risk_free(risk_free_rate_pct: Optional[Decimal]) -> Decimal:
        """The annual risk-free rate used for the Sharpe ratio."""
        if risk_free_rate_pct is not None:
            return Decimal(risk_free_rate_pct)
        return Decimal(str(settings.PAPER_TRADING_RISK_FREE_RATE_PCT))

    # ------------------------------------------------------------------
    # Orders and executions
    # ------------------------------------------------------------------

    def place_order(
        self, db: Session, portfolio_id: int, request: OrderCreate
    ) -> OrderExecutionResult:
        """Submit a simulated market order.

        Raises a domain error when the order is not executed: ``PortfolioInactiveError``,
        ``InvalidOrderError``, ``InsufficientCashError``, ``InsufficientPositionError`` or
        ``RiskLimitExceededError``. The rejected order is still recorded so order history
        always explains itself, and no cash, position or P&L is changed.

        With ``defer_execution`` the order is only *recorded* — as ``PENDING``, with its
        requested quantity, price and stock but no execution price — and is filled later by
        :meth:`execute_pending_order`. Nothing is filled twice and nothing is silently
        skipped: a deferred order that is never executed stays ``PENDING`` in the history.
        """
        portfolio = self.get_portfolio(db, portfolio_id)
        if not self.stock_repo.get_by_id(db, request.stock_id):
            raise StockNotFoundError(request.stock_id)

        if request.defer_execution:
            return self._record_pending_order(db, portfolio, request)

        intent = OrderIntent(
            portfolio_id=portfolio.id,
            stock_id=request.stock_id,
            side=request.side,
            quantity=request.quantity,
            requested_price=quantize_price(Decimal(str(request.requested_price))),
            order_type=request.order_type,
            idempotency_key=request.idempotency_key,
            signal_as_of=request.signal_as_of,
            execution_price_policy=request.execution_price_policy,
            metadata=dict(request.metadata) if request.metadata else None,
        )
        return self._submit_intent(db, portfolio, intent)

    def _record_pending_order(
        self, db: Session, portfolio: Portfolio, request: OrderCreate
    ) -> OrderExecutionResult:
        """Persist an order as ``PENDING`` without touching cash, positions or P&L."""
        existing = (
            self.order_repo.get_by_idempotency_key(db, portfolio.id, request.idempotency_key)
            if request.idempotency_key
            else None
        )
        if existing is not None:
            valuation = self._valuation(db, portfolio, None)
            return OrderExecutionResult(
                existing, self.trade_repo.get_by_order(db, existing.id), valuation, idempotent_replay=True
            )

        order = PortfolioOrder(
            portfolio_id=portfolio.id,
            stock_id=request.stock_id,
            trading_mode=PAPER_TRADING_MODE,
            side=request.side.value,
            order_type=request.order_type.value,
            status=OrderStatus.PENDING.value,
            source=OrderSource.MANUAL.value,
            quantity=request.quantity,
            requested_price=quantize_price(Decimal(str(request.requested_price))),
            signal_as_of=request.signal_as_of,
            execution_price_policy=(
                request.execution_price_policy.value if request.execution_price_policy else None
            ),
            metadata_json=(
                json.dumps(request.metadata, sort_keys=True, default=str)
                if request.metadata
                else None
            ),
            idempotency_key=request.idempotency_key,
        )
        self.order_repo.add(db, order)
        db.commit()
        db.refresh(order)
        logger.info(
            "Paper order %s recorded as PENDING: portfolio=%s stock=%s side=%s qty=%s "
            "requested_price=%s",
            order.id,
            portfolio.id,
            order.stock_id,
            order.side,
            order.quantity,
            order.requested_price,
        )
        return OrderExecutionResult(order, None, self._valuation(db, portfolio, None))

    def execute_pending_order(self, db: Session, portfolio_id: int, order_id: int) -> OrderExecutionResult:
        """Fill an order that was recorded as ``PENDING``.

        The order is re-validated from scratch against current portfolio state, so a deferred
        order is only executed if it would *still* be accepted when execution is finally
        requested — a position may have closed, cash may have been spent in between. An order
        that is no longer ``PENDING`` is refused with ``InvalidOrderError`` rather than
        executed twice.

        Raises:
            PortfolioNotFoundError: unknown portfolio.
            OrderNotFoundError: unknown order, or an order belonging to another portfolio.
            InvalidOrderError: the order is not in a ``PENDING`` state.
        """
        portfolio = self.get_portfolio(db, portfolio_id)
        order = self.order_repo.get_by_id(db, order_id)
        if order is None or order.portfolio_id != portfolio.id:
            raise OrderNotFoundError(order_id)
        if order.status != OrderStatus.PENDING.value:
            raise OrderNotExecutableError(order_id, order.status)

        intent = OrderIntent(
            portfolio_id=portfolio.id,
            stock_id=order.stock_id,
            side=OrderSide(order.side),
            quantity=order.quantity,
            requested_price=Decimal(order.requested_price),
            order_type=OrderType(order.order_type),
            source=OrderSource(order.source),
            idempotency_key=order.idempotency_key,
            signal_as_of=order.signal_as_of,
            execution_price_policy=(
                ExecutionPricePolicy(order.execution_price_policy)
                if order.execution_price_policy
                else None
            ),
            metadata=_load_json(order.metadata_json),
        )
        result = self._submit_intent(db, portfolio, intent, existing_order=order)
        return result

    def submit(self, db: Session, intent: OrderIntent) -> PaperRunOrder:
        """Submit a pre-built order intent and report the recorded outcome.

        This is the entry point the paper-trading engine uses, so a historical fill is
        validated, risk-checked, persisted and audited by exactly the same code as a manual
        order. A rejection is recorded and logged rather than raised, because a simulated run
        must keep stepping through history after one of its trades is refused.
        """
        portfolio = self.get_portfolio(db, intent.portfolio_id)
        try:
            result = self._submit_intent(db, portfolio, intent)
        except (
            PortfolioInactiveError,
            InvalidOrderError,
            InsufficientCashError,
            InsufficientPositionError,
            RiskLimitExceededError,
        ) as exc:
            valuation = self._valuation(db, portfolio, None)
            logger.info(
                "Simulated order rejected (portfolio=%s stock=%s): %s",
                portfolio.id,
                intent.stock_id,
                exc.code,
            )
            return PaperRunOrder(
                order_id=self._rejected_order_id(db, portfolio, intent),
                trade_id=None,
                stock_id=intent.stock_id,
                side=intent.side,
                quantity=intent.quantity,
                status=OrderStatus.REJECTED,
                signal_date=intent.signal_as_of,
                executed_at=intent.executed_at,
                reference_price=Decimal(intent.requested_price),
                execution_price=ZERO,
                cash_after=valuation.cash,
                equity_after=valuation.equity,
                rejection_code=_rejection_code_of(exc),
                rejection_reason=exc.message,
            )

        return PaperRunOrder(
            order_id=result.order.id,
            trade_id=result.trade.id if result.trade else None,
            stock_id=intent.stock_id,
            side=intent.side,
            quantity=intent.quantity,
            status=OrderStatus(result.order.status),
            signal_date=intent.signal_as_of,
            executed_at=result.order.executed_at,
            reference_price=Decimal(intent.requested_price),
            execution_price=Decimal(result.order.execution_price or intent.requested_price),
            fees=Decimal(result.order.fees or ZERO),
            realized_pnl=Decimal(result.order.realized_pnl or ZERO),
            cash_after=result.valuation.cash,
            equity_after=result.valuation.equity,
        )

    def get_order(self, db: Session, portfolio_id: int, order_id: int) -> PortfolioOrder:
        """One order of a portfolio.

        Raises ``OrderNotFoundError`` for an unknown order *and* for an order that belongs to
        a different portfolio, so an id from another portfolio can never be read here.
        """
        self.get_portfolio(db, portfolio_id)
        order = self.order_repo.get_by_id(db, order_id)
        if order is None or order.portfolio_id != portfolio_id:
            raise OrderNotFoundError(order_id)
        return order

    def get_orders(
        self,
        db: Session,
        portfolio_id: int,
        skip: int = 0,
        limit: int = DEFAULT_PAGE_LIMIT,
        status: Optional[OrderStatus] = None,
        stock_id: Optional[int] = None,
    ) -> List[PortfolioOrder]:
        """Order history, newest first."""
        self.get_portfolio(db, portfolio_id)
        limit = min(max(limit, 1), MAX_PAGE_LIMIT)
        return self.order_repo.list_history(
            db,
            portfolio_id,
            skip=skip,
            limit=limit,
            status=status.value if status is not None else None,
            stock_id=stock_id,
        )

    def count_orders(
        self,
        db: Session,
        portfolio_id: int,
        status: Optional[OrderStatus] = None,
        stock_id: Optional[int] = None,
    ) -> int:
        """Number of orders matching the same filters the history query applies."""
        return self.order_repo.count(
            db,
            portfolio_id,
            status=status.value if status is not None else None,
            stock_id=stock_id,
        )

    def get_trades(
        self,
        db: Session,
        portfolio_id: int,
        skip: int = 0,
        limit: int = DEFAULT_PAGE_LIMIT,
        stock_id: Optional[int] = None,
    ) -> List[PortfolioTrade]:
        """Trade history, newest first."""
        self.get_portfolio(db, portfolio_id)
        limit = min(max(limit, 1), MAX_PAGE_LIMIT)
        return self.trade_repo.list_history(db, portfolio_id, skip=skip, limit=limit, stock_id=stock_id)

    def count_trades(self, db: Session, portfolio_id: int, stock_id: Optional[int] = None) -> int:
        """Number of trades matching the same filters the history query applies."""
        return self.trade_repo.count(db, portfolio_id, stock_id=stock_id)

    def execute_strategy_order(
        self, db: Session, portfolio_id: int, request: StrategyOrderRequest
    ) -> StrategyExecutionResult:
        """Run the existing Phase 7 signal / Phase 8 strategy rules for one stock.

        The strategy decides; this method only places the resulting paper order through the
        same risk validation and execution path as a manual order. A ``hold`` decision places
        no order at all, and a decision already processed for that signal date is never
        executed twice.
        """
        portfolio = self.get_portfolio(db, portfolio_id)
        costs = self.trading_costs(portfolio)
        account = self._account(db, portfolio, request.stock_id)
        plan = self.bridge.build_plan(
            db,
            account,
            request.stock_id,
            costs=costs,
            strategy_name=request.strategy or STRATEGY_NAME,
            price=Decimal(str(request.price)) if request.price is not None else None,
            position_size_pct=Decimal(str(request.position_size_pct))
            if request.position_size_pct is not None
            else None,
            quantity=request.quantity,
            idempotency_key=request.idempotency_key,
        )

        if plan.intent is None:
            valuation = self._valuation(db, portfolio, None)
            return StrategyExecutionResult(
                stock_id=plan.stock_id,
                action=plan.action,
                composite_score=plan.composite_score,
                signal_indicators=plan.signal_indicators,
                as_of=plan.as_of,
                reason=plan.reason,
                strategy_name=plan.strategy_name,
                order=None,
                trade=None,
                valuation=valuation,
            )

        result = self._submit_intent(db, portfolio, plan.intent)
        logger.info(
            "Strategy '%s' action=%s for portfolio %s stock %s produced order %s (%s)",
            plan.strategy_name,
            plan.action,
            portfolio.id,
            plan.stock_id,
            result.order.id,
            result.order.status,
        )
        return StrategyExecutionResult(
            stock_id=plan.stock_id,
            action=plan.action,
            composite_score=plan.composite_score,
            signal_indicators=plan.signal_indicators,
            as_of=plan.as_of,
            reason=plan.reason,
            strategy_name=plan.strategy_name,
            order=result.order,
            trade=result.trade,
            valuation=result.valuation,
            idempotent_replay=result.idempotent_replay,
        )

    # ------------------------------------------------------------------
    # Configuration accessors
    # ------------------------------------------------------------------

    @staticmethod
    def trading_costs(portfolio: Portfolio) -> TradingCostConfig:
        """The cost model recorded on the portfolio, falling back to application defaults."""
        payload = _load_json(portfolio.cost_config_json)
        if payload is None:
            return default_trading_cost_config()
        return TradingCostConfig(
            commission_rate=Decimal(str(payload.get("commission_rate", "0.001"))),
            min_fee=Decimal(str(payload.get("min_fee", "0"))),
            slippage_rate=Decimal(str(payload.get("slippage_rate", "0"))),
        )

    @staticmethod
    def risk_limits(portfolio: Portfolio) -> RiskLimits:
        """The risk limits recorded on the portfolio, falling back to application defaults.

        A portfolio row written before a limit existed simply has no key for it, so every field
        is read with ``.get`` and an unset limit stays ``None`` (rule disabled) rather than
        raising or defaulting to an arbitrary cap.
        """
        payload = _load_json(portfolio.risk_config_json)
        if payload is None:
            return default_risk_limits()
        return RiskLimits(
            max_position_quantity=payload.get("max_position_quantity"),
            max_position_value=_optional_decimal(payload.get("max_position_value")),
            max_position_allocation_pct=_optional_decimal(payload.get("max_position_allocation_pct")),
            max_drawdown_pct=_optional_decimal(payload.get("max_drawdown_pct")),
            max_daily_loss_pct=_optional_decimal(payload.get("max_daily_loss_pct")),
            min_trade_value=_optional_decimal(payload.get("min_trade_value")),
            max_exposure_pct=_optional_decimal(payload.get("max_exposure_pct")),
            max_trade_allocation_pct=_optional_decimal(payload.get("max_trade_allocation_pct")),
            min_cash_reserve_pct=_optional_decimal(payload.get("min_cash_reserve_pct")),
            max_positions=payload.get("max_positions"),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _submit_intent(
        self,
        db: Session,
        portfolio: Portfolio,
        intent: OrderIntent,
        existing_order: Optional[PortfolioOrder] = None,
    ) -> OrderExecutionResult:
        """Validate, execute and persist one order intent, then report or raise.

        Duplicate protection runs before anything else: an order already recorded for the
        same idempotency key or the same strategy event is returned as-is instead of being
        executed a second time. ``existing_order`` reuses a row that was already recorded as
        ``PENDING`` instead of inserting a second order for it.
        """
        replay = self._find_replay(db, portfolio.id, intent)
        if replay is not None:
            trade = self.trade_repo.get_by_order(db, replay.id)
            valuation = self._valuation(db, portfolio, None)
            logger.info(
                "Duplicate order request for portfolio %s (key=%s event=%s) returned existing order %s",
                portfolio.id,
                intent.idempotency_key,
                intent.strategy_event_id,
                replay.id,
            )
            return OrderExecutionResult(replay, trade, valuation, idempotent_replay=True)

        # Serialise against any concurrent order on this portfolio *before* reading its cash and
        # positions. The lock is held until the commit below, and released by the rollback in the
        # failure path, so two simultaneous buys can never both spend the same balance.
        portfolio = self._lock_portfolio(db, portfolio.id)

        account = self._account(db, portfolio, intent.stock_id)
        limits = self.risk_limits(portfolio)
        executor = OrderExecutor(self.trading_costs(portfolio), limits)
        day_open = (
            day_open_equity(
                db,
                portfolio,
                as_of=intent.executed_at,
                snapshot_repo=self.snapshot_repo,
                transaction_repo=self.transaction_repo,
            )
            if limits.max_daily_loss_pct is not None
            else None
        )
        plan = executor.execute(
            account,
            intent,
            peak_equity=self._peak_equity(db, portfolio, account),
            day_open_equity=day_open,
        )

        try:
            order, trade, valuation = self._persist_plan(
                db, portfolio, intent, plan, existing_order=existing_order
            )
            db.commit()
        except Exception:
            db.rollback()
            logger.error(
                "Paper order failed for portfolio %s stock %s; transaction rolled back",
                portfolio.id,
                intent.stock_id,
            )
            raise

        if not plan.accepted:
            self._raise_rejection(plan, portfolio)
        return OrderExecutionResult(order, trade, valuation)

    def _rejected_order_id(
        self, db: Session, portfolio: Portfolio, intent: OrderIntent
    ) -> Optional[int]:
        """The recorded id of a rejected order, so a refusal is still auditable.

        A rejection is persisted before the domain error is raised, so the caller of
        :meth:`submit` can point at the exact ``REJECTED`` row rather than reporting an
        id-less outcome.
        """
        recorded = self._find_replay(db, portfolio.id, intent)
        return recorded.id if recorded is not None else None

    def _find_replay(
        self, db: Session, portfolio_id: int, intent: OrderIntent
    ) -> Optional[PortfolioOrder]:
        """A settled order for the same idempotency key or strategy event, if any.

        Only ``FILLED`` and ``REJECTED`` orders count as settled. A ``PENDING`` order is
        deliberately ignored: it is the same request waiting for execution, so returning it
        here would leave the deferred order permanently unfillable. Duplicate protection for
        a pending order is enforced by :meth:`place_order`, which refuses to record a second
        order for the same key.
        """
        settled = {OrderStatus.FILLED.value, OrderStatus.REJECTED.value}
        if intent.idempotency_key:
            existing = self.order_repo.get_by_idempotency_key(
                db, portfolio_id, intent.idempotency_key
            )
            if existing is not None and existing.status in settled:
                return existing
        if intent.strategy_event_id:
            candidate = self.order_repo.get_by_strategy_event(
                db, portfolio_id, intent.strategy_event_id
            )
            if candidate is not None and candidate.status in settled:
                return candidate
        return None

    def _persist_plan(
        self,
        db: Session,
        portfolio: Portfolio,
        intent: OrderIntent,
        plan: ExecutionPlan,
        existing_order: Optional[PortfolioOrder] = None,
    ) -> Tuple[PortfolioOrder, Optional[PortfolioTrade], PortfolioValuation]:
        """Write the whole outcome in one transaction: order, trade, position, cash, P&L.

        ``existing_order`` reuses a row already recorded as ``PENDING`` — only its execution
        fields are filled in, so the original submission stays auditable instead of being
        replaced by a second row.
        """
        executed_at = intent.executed_at or utcnow()
        order = existing_order or PortfolioOrder(
            portfolio_id=portfolio.id,
            stock_id=intent.stock_id,
            trading_mode=PAPER_TRADING_MODE,
            side=intent.side.value,
            order_type=intent.order_type.value,
            status=plan.order_status.value,
            source=intent.source.value,
            quantity=intent.quantity,
            requested_price=quantize_price(intent.requested_price),
        )
        if existing_order is not None:
            order.status = plan.order_status.value
        order.execution_price = plan.execution_price if plan.accepted else None
        order.gross_value = plan.gross_value if plan.accepted else None
        order.fees = plan.fees if plan.accepted else None
        order.net_cash_impact = plan.net_cash_impact if plan.accepted else None
        order.realized_pnl = plan.realized_pnl if plan.accepted else None
        order.rejection_code = plan.rejection.code.value if plan.rejection else None
        order.rejection_reason = plan.rejection.reason if plan.rejection else None
        order.signal_as_of = intent.signal_as_of
        order.execution_price_policy = (
            intent.execution_price_policy.value if intent.execution_price_policy else None
        )
        order.metadata_json = (
            json.dumps(intent.metadata, sort_keys=True, default=str) if intent.metadata else None
        )
        order.idempotency_key = intent.idempotency_key
        order.strategy_event_id = intent.strategy_event_id
        order.executed_at = executed_at if plan.accepted else None
        self.order_repo.add(db, order)

        trade: Optional[PortfolioTrade] = None
        if plan.accepted:
            trade = PortfolioTrade(
                portfolio_id=portfolio.id,
                order_id=order.id,
                stock_id=intent.stock_id,
                side=intent.side.value,
                quantity=intent.quantity,
                execution_price=plan.execution_price,
                gross_value=plan.gross_value,
                fees=plan.fees,
                net_cash_impact=plan.net_cash_impact,
                realized_pnl=plan.realized_pnl,
                executed_at=executed_at,
            )
            self.trade_repo.add(db, trade)
            self._record_fill_transaction(db, portfolio, order, trade, plan, executed_at)
            self._apply_position(db, portfolio, plan)
            self._apply_cash_and_realized(db, portfolio, plan)
            self._log_position_change(portfolio, plan)

        valuation = self._valuation(db, portfolio, None)
        self._persist_valuation(db, portfolio, valuation)

        logger.info(
            "Paper order %s %s portfolio=%s stock=%s qty=%s status=%s cash=%s equity=%s",
            order.id,
            order.side,
            portfolio.id,
            order.stock_id,
            order.quantity,
            order.status,
            valuation.cash,
            valuation.equity,
        )
        if plan.rejection is not None:
            logger.warning(
                "Risk engine rejected order %s (portfolio=%s rule=%s): %s",
                order.id,
                portfolio.id,
                order.rejection_code,
                order.rejection_reason,
            )
        return order, trade, valuation

    def _record_fill_transaction(
        self,
        db: Session,
        portfolio: Portfolio,
        order: PortfolioOrder,
        trade: PortfolioTrade,
        plan: ExecutionPlan,
        executed_at: datetime,
    ) -> PortfolioTransaction:
        """Append the ledger entry for one filled order.

        The figures are taken from the execution record rather than recomputed, so the ledger and
        the trade history can never disagree about what a fill cost.
        """
        entry = PortfolioTransaction(
            portfolio_id=portfolio.id,
            transaction_type=plan.side.value,
            stock_id=plan.stock_id,
            quantity=trade.quantity,
            price=trade.execution_price,
            fees=trade.fees,
            gross_value=trade.gross_value,
            net_cash_impact=trade.net_cash_impact,
            realized_pnl=trade.realized_pnl,
            cash_after=plan.cash_after,
            order_id=order.id,
            trade_id=trade.id,
            timestamp=executed_at,
            reason=f"Simulated {plan.side.value} of {trade.quantity} share(s) at {trade.execution_price}",
            metadata_json=order.metadata_json,
        )
        return self.transaction_repo.add(db, entry)

    def _log_position_change(self, portfolio: Portfolio, plan: ExecutionPlan) -> None:
        """Log the position lifecycle event an accepted fill produced."""
        before = plan.position_before
        after = plan.position_after
        if after is None:
            logger.info(
                "Position closed: portfolio=%s stock=%s realised_pnl=%s",
                portfolio.id,
                plan.stock_id,
                plan.realized_pnl,
            )
        elif before is None:
            logger.info(
                "Position opened: portfolio=%s stock=%s quantity=%s average_price=%s",
                portfolio.id,
                plan.stock_id,
                after.quantity,
                after.average_entry_price,
            )
        elif after.quantity > before.quantity:
            logger.info(
                "Position increased: portfolio=%s stock=%s quantity %s -> %s average %s -> %s",
                portfolio.id,
                plan.stock_id,
                before.quantity,
                after.quantity,
                before.average_entry_price,
                after.average_entry_price,
            )
        else:
            logger.info(
                "Position reduced: portfolio=%s stock=%s quantity %s -> %s realised_pnl=%s",
                portfolio.id,
                plan.stock_id,
                before.quantity,
                after.quantity,
                plan.realized_pnl,
            )

    def _apply_position(self, db: Session, portfolio: Portfolio, plan: ExecutionPlan) -> None:
        """Create, update or close the persisted position from an accepted plan."""
        stock_id = plan.stock_id
        if plan.position_after is None:
            existing = self.position_repo.get(db, portfolio.id, stock_id)
            if existing is not None:
                # A full exit closes the position: the row is removed, while the realised
                # P&L stays on the portfolio and in the immutable trade history.
                self.position_repo.delete(db, existing)
            return

        state = plan.position_after
        position = self.position_repo.get(db, portfolio.id, stock_id)
        if position is None:
            position = PortfolioPosition(
                portfolio_id=portfolio.id,
                stock_id=stock_id,
                quantity=state.quantity,
                average_entry_price=state.average_entry_price,
                cost_basis=state.cost_basis,
                entry_fees_total=state.entry_fees_total,
                realized_pnl=quantize_money(plan.realized_pnl),
                current_price=state.current_price,
                market_value=market_value(state.quantity, state.current_price or ZERO),
                unrealized_pnl=unrealized_pnl(
                    state.quantity, state.average_entry_price, state.current_price or ZERO
                ),
            )
            self.position_repo.add(db, position)
            return

        position.quantity = state.quantity
        position.average_entry_price = state.average_entry_price
        position.cost_basis = cost_basis(state.quantity, state.average_entry_price)
        position.entry_fees_total = state.entry_fees_total
        position.realized_pnl = quantize_money(Decimal(position.realized_pnl) + plan.realized_pnl)
        position.current_price = state.current_price
        position.market_value = market_value(state.quantity, state.current_price or ZERO)
        position.unrealized_pnl = unrealized_pnl(
            state.quantity, state.average_entry_price, state.current_price or ZERO
        )

    def _apply_cash_and_realized(self, db: Session, portfolio: Portfolio, plan: ExecutionPlan) -> None:
        """Move cash and accumulate realised P&L on the portfolio."""
        portfolio.current_cash = plan.cash_after
        portfolio.realized_pnl = quantize_money(
            Decimal(portfolio.realized_pnl) + plan.realized_pnl
        )

    def _account(
        self, db: Session, portfolio: Portfolio, stock_id: Optional[int] = None
    ) -> PortfolioAccount:
        """Build the runtime account state an order is validated against."""
        return build_account(db, portfolio, self.position_repo, stock_id)

    def account_state(self, db: Session, portfolio: Portfolio) -> PortfolioAccount:
        """Runtime account state for the portfolio as a whole.

        Used by the risk check and the paper engine, both of which need the portfolio-level
        totals (all positions, position count) rather than one stock's position.
        """
        return build_account(db, portfolio, self.position_repo)

    def get_position_state(
        self, db: Session, portfolio_id: int, stock_id: int
    ) -> Optional[PositionState]:
        """The open position for ``stock_id``, or ``None`` when the portfolio is flat."""
        position = self.position_repo.get(db, portfolio_id, stock_id)
        return position_state(position) if position is not None else None

    def get_position(self, db: Session, portfolio_id: int, stock_id: int) -> PortfolioPosition:
        """The persisted open position for ``stock_id``.

        Raises ``PositionNotFoundError`` when the portfolio holds none, which is distinct from
        a portfolio that does not exist at all.
        """
        self.get_portfolio(db, portfolio_id)
        position = self.position_repo.get(db, portfolio_id, stock_id)
        if position is None:
            raise PositionNotFoundError(portfolio_id, stock_id)
        return position

    def _valuation(
        self,
        db: Session,
        portfolio: Portfolio,
        prices: Optional[Dict[int, Decimal]],
    ) -> PortfolioValuation:
        """Pure valuation of the portfolio, optionally overriding position marks."""
        return self.valuations.value(db, portfolio, prices)

    def _persist_valuation(self, db: Session, portfolio: Portfolio, valuation: PortfolioValuation) -> None:
        """Write the derived portfolio figures. ``total_pnl`` stays derived, never set twice."""
        portfolio.equity = valuation.equity
        portfolio.unrealized_pnl = valuation.unrealized_pnl
        portfolio.total_pnl = valuation.total_pnl
        db.flush()

    def _apply_position_marks(
        self, db: Session, portfolio: Portfolio, valuation: PortfolioValuation
    ) -> None:
        """Persist the marks a valuation produced onto the open positions."""
        for valued in valuation.positions:
            position = self.position_repo.get(db, portfolio.id, valued.stock_id)
            if position is None:
                continue
            position.current_price = valued.current_price
            position.market_value = valued.market_value
            position.unrealized_pnl = valued.unrealized_pnl
        db.flush()

    def _peak_equity(
        self, db: Session, portfolio: Portfolio, account: PortfolioAccount
    ) -> Decimal:
        """Highest equity ever recorded: initial capital, current equity and every snapshot."""
        return self.peak_equity(db, portfolio, account)

    def peak_equity(
        self,
        db: Session,
        portfolio: Portfolio,
        account: Optional[PortfolioAccount] = None,
    ) -> Decimal:
        """Highest equity ever recorded, used as the base of the drawdown guard.

        The maximum of the opening capital, the equity right now and every recorded snapshot.
        Snapshots are included so a drawdown measured from a historical peak is not silently
        forgotten once the portfolio recovers past it.
        """
        if account is None:
            account = self.account_state(db, portfolio)
        return peak_equity(
            [
                Decimal(portfolio.initial_capital),
                account.equity,
                self.snapshot_repo.max_equity(db, portfolio.id),
            ]
        )

    @staticmethod
    def _raise_rejection(plan: ExecutionPlan, portfolio: Portfolio) -> None:
        """Translate a rejection into the matching domain error, for HTTP mapping."""
        assert plan.rejection is not None
        code = plan.rejection.code
        details = dict(plan.rejection.details or {})
        details["portfolio_id"] = portfolio.id
        details["rejection_reason"] = plan.rejection.reason

        if code is RejectionCode.PORTFOLIO_INACTIVE:
            raise PortfolioInactiveError(portfolio.id, portfolio.status)
        if code is RejectionCode.INSUFFICIENT_CASH:
            raise InsufficientCashError(
                details.get("required_cash", "0"), details.get("available_cash", "0"), details
            )
        if code is RejectionCode.INSUFFICIENT_POSITION:
            raise InsufficientPositionError(
                details.get("requested_quantity", "0"), details.get("owned_quantity", "0"), details
            )
        if code in RISK_REJECTION_CODES:
            raise RiskLimitExceededError(
                str(details.get("rule", "risk_limit")),
                plan.rejection.reason,
                details,
                code=str(code.value),
            )
        raise InvalidOrderError(plan.rejection.reason, details)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


#: Every code that means "a configured risk limit refused this trade". Each limit has its own
#: code so a caller can branch on the rule; they share one domain error and one HTTP status.
RISK_REJECTION_CODES = frozenset(
    {
        RejectionCode.RISK_LIMIT_EXCEEDED,
        RejectionCode.MAX_DRAWDOWN_EXCEEDED,
        RejectionCode.MAX_POSITIONS_EXCEEDED,
        RejectionCode.MAX_POSITION_QUANTITY_EXCEEDED,
        RejectionCode.MAX_POSITION_SIZE_EXCEEDED,
        RejectionCode.MAX_POSITION_ALLOCATION_EXCEEDED,
        RejectionCode.MAX_EXPOSURE_EXCEEDED,
        RejectionCode.MAX_TRADE_ALLOCATION_EXCEEDED,
        RejectionCode.MIN_CASH_RESERVE_BREACHED,
        RejectionCode.MAX_DAILY_LOSS_EXCEEDED,
        RejectionCode.MIN_TRADE_VALUE_BELOW,
    }
)


def utcnow() -> datetime:
    """Naive UTC timestamp used for orders, trades, snapshots and valuations.

    Stored values are naive UTC, matching the naive ``DateTime`` columns used across the
    project, so a value read from the database compares equal to one just written.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def build_account(
    db: Session,
    portfolio: Portfolio,
    position_repo=position_repository,
    stock_id: Optional[int] = None,
) -> PortfolioAccount:
    """Assemble the runtime account an order or risk check is validated against.

    Exposed at module level (rather than kept as a method) because the risk-check service and
    the paper engine both need exactly this view and must not build their own — the risk rules
    are only meaningful if every caller measures the portfolio the same way.

    ``stock_id`` names the position the caller is asking about. Leaving it ``None`` builds the
    portfolio-level view: totals and position count only, with no single position in focus.
    """
    positions = position_repo.list_open(db, portfolio.id)
    total_market_value = quantize_money(
        sum(
            (
                Decimal(position.market_value)
                if position.market_value is not None
                else cost_basis(position.quantity, position.average_entry_price)
                for position in positions
            ),
            ZERO,
        )
    )
    # A position is only "in focus" when the caller named one. Without this guard the loop
    # resolved ``target`` to whichever row happened to be loaded last, so a portfolio-level
    # account carried an arbitrary position and every rule that asked for a *different* stock
    # read it as flat. That reported "no open position" for a sell the portfolio could fund.
    states = tuple(position_state(row) for row in positions)
    target: Optional[PositionState] = None
    for state in states:
        if stock_id is not None and state.stock_id == stock_id:
            target = state
    return PortfolioAccount(
        portfolio_id=portfolio.id,
        name=portfolio.name,
        status=PortfolioStatus(portfolio.status),
        initial_capital=Decimal(portfolio.initial_capital),
        cash=Decimal(portfolio.current_cash),
        realized_pnl=Decimal(portfolio.realized_pnl),
        total_market_value=total_market_value,
        position=target,
        position_count=len(positions),
        positions=states,
    )


def _rejection_code_of(exc: AppException) -> Optional[RejectionCode]:
    """The machine-readable rejection code behind a domain error, when there is one."""
    try:
        return RejectionCode(exc.code)
    except ValueError:
        return None


def _costs_to_dict(costs: TradingCostConfig) -> Dict[str, str]:
    return {
        "commission_rate": str(costs.commission_rate),
        "min_fee": str(costs.min_fee),
        "slippage_rate": str(costs.slippage_rate),
    }


def _limits_to_dict(limits: RiskLimits) -> Dict[str, object]:
    """Serialise every risk limit for ``risk_config_json``.

    Decimals are stored as strings so a limit round-trips exactly rather than through binary
    floating point, and every field is written — including the ``None`` ones — so a stored
    config always describes the full rule set.
    """
    return {
        "max_position_quantity": limits.max_position_quantity,
        "max_position_value": _money_or_none(limits.max_position_value),
        "max_position_allocation_pct": _money_or_none(limits.max_position_allocation_pct),
        "max_drawdown_pct": _money_or_none(limits.max_drawdown_pct),
        "max_daily_loss_pct": _money_or_none(limits.max_daily_loss_pct),
        "min_trade_value": _money_or_none(limits.min_trade_value),
        "max_exposure_pct": _money_or_none(limits.max_exposure_pct),
        "max_trade_allocation_pct": _money_or_none(limits.max_trade_allocation_pct),
        "min_cash_reserve_pct": _money_or_none(limits.min_cash_reserve_pct),
        "max_positions": limits.max_positions,
    }


def _money_or_none(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else str(value)


def _load_json(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Stored paper-trading configuration could not be parsed; using defaults")
        return None
    return payload if isinstance(payload, dict) else None


def _optional_decimal(value) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _equity_curve_for_metrics(
    portfolio: Portfolio, snapshots: Sequence[PortfolioSnapshot]
) -> List[EquityPoint]:
    """Adapt portfolio snapshots onto the Phase 8 equity-curve type.

    The initial capital is prepended as a synthetic first point so peak, drawdown and
    drawdown percentage are measured against the capital the portfolio started with, exactly
    as a backtest measures them.
    """
    points = [
        EquityPoint(
            date=snapshot.timestamp.date(),
            cash=Decimal(snapshot.cash),
            position_value=Decimal(snapshot.market_value),
            equity=Decimal(snapshot.equity),
        )
        for snapshot in snapshots
    ]
    initial = Decimal(portfolio.initial_capital)
    return [
        EquityPoint(
            date=points[0].date if points else utcnow().date(),
            cash=initial,
            position_value=ZERO,
            equity=initial,
        ),
        *points,
    ]


paper_trading_service = PaperTradingService()
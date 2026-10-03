"""Phase 13 orchestration — the layer that resolves state and delegates to the pure engines.

This module is the only Phase 13 code that touches the database. It reads a portfolio, its
valuation, its peak equity and its stored policy, turns the request's candidates into
:class:`~app.services.portfolio.models.AllocationCandidate` values, and hands everything to the
pure engines in :mod:`app.services.portfolio.sizing`,
:mod:`app.services.portfolio.allocation_engine`, :mod:`app.services.portfolio.risk_state` and
:mod:`app.services.portfolio.portfolio_backtest`.

It contains no allocation arithmetic of its own. That split is deliberate: the arithmetic is
unit-testable without a fixture, and this layer stays a thin, obvious translation between stored
rows and engine inputs.

Applying an allocation
----------------------
``apply=True`` submits each approved allocation through
:meth:`~app.services.portfolio.service.PaperTradingService.submit` — the **same** path a manual
order takes, so the Phase 9-12 validation, risk check, cash ledger, position update and
snapshot all still apply. Phase 13 adds no second writer of portfolio state: an allocation that
is refused by the pre-trade engine is reported as refused, and the allocation engine's own
approval never bypasses the ordinary order path.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from app.core.exceptions import (
    InvalidRiskPolicyError,
    StockNotFoundError,
    UnsupportedOrderSideError,
)
from app.core.config import settings
from app.core.logging import logger
from app.models.portfolio import Portfolio
from app.repositories.price_repository import price_repository
from app.schemas.portfolio import OrderSide, OrderSource, PositionSizingMethod
from app.schemas.risk import (
    AllocateRequest,
    AllocationCandidateInput,
    PositionSizeRequest,
)
from app.services.backtest.indicators import PointInTimeContextBuilder
from app.services.backtest.service import INDICATOR_WARMUP_CALENDAR_DAYS
from app.services.backtest.validator import load_bars, validate_bars
from app.services.portfolio.allocation import side_value
from app.services.portfolio.allocation_engine import PortfolioAllocationEngine
from app.services.portfolio.models import (
    ZERO,
    AllocationCandidate,
    AllocationResult,
    PortfolioRiskPolicy,
    PortfolioRiskState,
    SizingBreakdown,
    policy_from_allocation,
)
from app.services.portfolio.portfolio_backtest import (
    BacktestStock,
    PortfolioBacktestEngine,
    PortfolioBacktestResult,
)
from app.services.portfolio.risk_state import PortfolioRiskStateEngine
from app.services.portfolio.service import PaperTradingService, OrderExecutionResult, utcnow
from app.services.portfolio.sizing import (
    METHOD_MAX_POSITION_CAP,
    METHOD_RISK_AND_WEIGHT,
    PositionSizingEngine,
)


class PortfolioAllocationService:
    """Resolves portfolio state and delegates to the Phase 13 pure engines."""

    def __init__(
        self,
        paper: Optional[PaperTradingService] = None,
        allocation_engine: Optional[PortfolioAllocationEngine] = None,
        risk_engine: Optional[PortfolioRiskStateEngine] = None,
        sizer: Optional[PositionSizingEngine] = None,
        price_repo=price_repository,
        context_builder: Optional[PointInTimeContextBuilder] = None,
    ):
        self.paper = paper or PaperTradingService()
        self.allocations = allocation_engine or PortfolioAllocationEngine()
        self.risk = risk_engine or PortfolioRiskStateEngine()
        self.sizer = sizer or PositionSizingEngine()
        self.price_repo = price_repo
        self.context_builder = context_builder or PointInTimeContextBuilder()

    # ------------------------------------------------------------------
    # Policy
    # ------------------------------------------------------------------

    def policy(
        self,
        portfolio: Portfolio,
        override: Optional[PortfolioRiskPolicy] = None,
    ) -> PortfolioRiskPolicy:
        """The portfolio's effective Phase 13 policy.

        Assembled from the portfolio's stored construction policy, its stored order limits and
        its stored cost model, then **replaced outright** by ``override`` when the caller sent
        one. The override is a complete policy rather than a patch: a partial merge would leave a
        caller unable to tell which limits were theirs and which were inherited, and a risk
        policy nobody can read is not auditable.

        Raises:
            InvalidRiskPolicyError: the assembled or supplied policy cannot be acted on.
        """
        if override is not None:
            policy = override
        else:
            policy = policy_from_allocation(
                self.paper.allocation_config(portfolio),
                self.paper.risk_limits(portfolio),
                self.paper.trading_costs(portfolio),
            )
        try:
            policy.validate()
        except ValueError as exc:
            raise InvalidRiskPolicyError(
                f"Unusable portfolio risk policy: {exc}",
                {"portfolio_id": portfolio.id},
            ) from exc
        return policy

    # ------------------------------------------------------------------
    # Risk state
    # ------------------------------------------------------------------

    def risk_state(
        self,
        db: Session,
        portfolio: Portfolio,
        *,
        prices: Optional[Dict[int, Decimal]] = None,
        policy: Optional[PortfolioRiskPolicy] = None,
        as_of=None,
    ) -> PortfolioRiskState:
        """Where the portfolio stands against its policy, right now. Read-only."""
        effective = self.policy(portfolio, policy)
        valuation = self.paper.valuations.value(db, portfolio, prices)
        if valuation.equity < ZERO:
            raise InvalidRiskPolicyError(
                f"Portfolio {portfolio.id} has negative equity ({valuation.equity}); every "
                "weight, exposure and drawdown figure is a ratio against equity and would be "
                "meaningless.",
                {"portfolio_id": portfolio.id, "equity": str(valuation.equity)},
            )
        history = [
            Decimal(point.equity) for point in self.paper.performance.drawdown_curve(db, portfolio)
        ]
        return self.risk.evaluate(
            valuation,
            effective,
            peak_equity=self.paper.peak_equity(db, portfolio),
            equity_history=history,
            sectors=self.sector_map(db, [p.stock_id for p in valuation.positions]),
            portfolio_id=portfolio.id,
            as_of=as_of or utcnow(),
            initial_capital=Decimal(portfolio.initial_capital),
        )

    # ------------------------------------------------------------------
    # Position sizing
    # ------------------------------------------------------------------

    def size_position(
        self,
        db: Session,
        portfolio: Portfolio,
        request: PositionSizeRequest,
    ) -> SizingBreakdown:
        """Size one candidate trade with the Phase 13 risk-and-weight rule.

        Raises:
            StockNotFoundError: unknown ``stock_id``.
            UnsupportedOrderSideError: a side other than BUY.
            InvalidOrderError: no usable price could be resolved.
            InvalidStopPriceError: the stop is missing, at or above the entry, or zero.
        """
        self._require_buy(request.side)
        stock = self.paper.stock_repo.get_by_id(db, request.stock_id)
        if not stock:
            raise StockNotFoundError(request.stock_id)

        valuation = self.paper.valuations.value(db, portfolio, None)
        entry = self._entry_price(db, request.stock_id, request.entry_price)
        effective = self.policy(portfolio, self._override_policy(request.policy))
        position = self.paper.get_position_state(db, portfolio.id, request.stock_id)
        held = position.quantity if position else 0
        held_value = position.market_value() if position else ZERO

        return self.sizer.size(
            equity=valuation.equity,
            available_cash=valuation.cash,
            entry_price=entry,
            stop_price=request.stop_price,
            policy=effective,
            method=self._method(request.method),
            existing_quantity=held,
            remaining_weight_capacity=(
                effective.max_position_value(valuation.equity) - held_value
                if held_value
                else None
            ),
        )

    # ------------------------------------------------------------------
    # Allocation
    # ------------------------------------------------------------------

    def allocate(
        self,
        db: Session,
        portfolio: Portfolio,
        request: AllocateRequest,
    ) -> tuple:
        """Evaluate — and optionally apply — one or more candidate signals.

        Returns ``(result, applications)`` where ``applications`` pairs each approved decision
        with whatever the ordinary order path returned for it, in the same order. The pairing is
        what lets a caller see that an approved allocation was still refused downstream, rather
        than having to reconcile two lists itself.
        """
        as_of = request.as_of or utcnow()
        valuation = self.paper._valuation(db, portfolio, request.prices)
        if valuation.equity < ZERO:
            raise InvalidRiskPolicyError(
                f"Portfolio {portfolio.id} has negative equity ({valuation.equity}); a "
                "portfolio cannot be allocated capital it does not have.",
                {"portfolio_id": portfolio.id, "equity": str(valuation.equity)},
            )
        effective = self.policy(portfolio, self._override_policy(request.policy))

        held = {
            position.stock_id: position.quantity
            for position in self.paper.get_positions(db, portfolio.id)
        }
        held_value = {
            valuation_position.stock_id: valuation_position.market_value
            for valuation_position in valuation.positions
        }
        sectors = self.sector_map(db, [p.stock_id for p in valuation.positions])
        sector_weights = self._sector_weights(valuation, sectors)

        candidates = self._resolve_candidates(db, request.candidates, request.prices)
        sides = {side_value(candidate.side) for candidate in request.candidates}
        if sides - {"BUY"}:
            raise UnsupportedOrderSideError(", ".join(sorted(sides - {"BUY"})))

        result: AllocationResult = self.allocations.allocate(
            portfolio_id=portfolio.id,
            as_of=as_of,
            cash=valuation.cash,
            equity=valuation.equity,
            invested_value=valuation.market_value,
            position_count=len(valuation.positions),
            candidates=candidates,
            held=held,
            held_value=held_value,
            sector_weights=sector_weights,
            policy=effective,
            method=self._method(request.sizing_method),
        )

        applications: List[tuple] = []
        if request.apply:
            applications = self._apply(db, portfolio, result, effective)
        return result, applications

    def _apply(
        self,
        db: Session,
        portfolio: Portfolio,
        result: AllocationResult,
        policy: PortfolioRiskPolicy,
    ) -> List[tuple]:
        """Submit every approved allocation through the ordinary order path.

        Approvals are applied in the order the engine produced them, and each application's
        **actual** result is returned. An allocation the pre-trade engine refuses is reported as
        refused: the allocation engine's approval is a proposal, and the order path has the last
        word. One approval failing never stops the rest.
        """
        applied: List[tuple] = []
        for decision in result.approved:
            try:
                outcome: OrderExecutionResult = self.paper.submit(
                    db,
                    _intent(portfolio.id, decision),
                )
            except Exception as exc:  # noqa: BLE001 - one refusal must not end the batch
                logger.warning(
                    "Phase 13 allocation for stock %s was approved but its order was refused: %s",
                    decision.stock_id,
                    exc,
                )
                applied.append((decision, None))
                continue
            applied.append((decision, outcome))
        return applied

    # ------------------------------------------------------------------
    # Portfolio backtest
    # ------------------------------------------------------------------

    def backtest(
        self,
        db: Session,
        portfolio: Portfolio,
        request,
    ) -> PortfolioBacktestResult:
        """Replay several stocks against this portfolio's policy and capital.

        Loads nothing but price history, indicator contexts and company metadata; **no portfolio
        row, position, order, trade or snapshot is written**. A backtest is a question about the
        recorded history, not an event in the portfolio's life.
        """
        initial_capital = (
            Decimal(str(request.initial_capital))
            if request.initial_capital is not None
            else Decimal(portfolio.initial_capital)
        )
        effective = self.policy(portfolio, self._override_policy(request.policy))
        engine = PortfolioBacktestEngine()

        stocks: List[BacktestStock] = []
        for stock_id in request.unique_candidates():
            stock = self.paper.stock_repo.get_by_id(db, stock_id)
            if not stock:
                raise StockNotFoundError(stock_id)
            bars, contexts = self._load_series(db, stock_id, request)
            if not bars:
                logger.warning(
                    "Portfolio backtest for portfolio %s: stock %s has no usable bars in the "
                    "window and was skipped",
                    portfolio.id,
                    stock_id,
                )
                continue
            stocks.append(
                BacktestStock(
                    stock_id=stock.id,
                    symbol=stock.symbol,
                    bars=bars,
                    contexts=contexts,
                    stop_price=request.stop_for(stock_id),
                )
            )
        if not stocks:
            raise ValueError(
                "No candidate has usable price data in the requested window, so the portfolio "
                "backtest has nothing to replay."
            )

        return engine.run(
            portfolio_id=portfolio.id,
            stocks=stocks,
            policy=effective,
            initial_capital=initial_capital,
            start_date=request.start_date,
            end_date=request.end_date,
            strategy_name=request.strategy,
            exit_on_signal=request.exit_on_signal,
            mark_unpriceable_positions=request.mark_unpriceable_positions,
            risk_free_rate_pct=Decimal(str(settings.PAPER_TRADING_RISK_FREE_RATE_PCT)),
        )

    def _load_series(self, db: Session, stock_id: int, request) -> tuple:
        """Warm-up plus recorded bars for one stock, with point-in-time indicator contexts.

        Warm-up bars are fetched but only the ones inside the requested window can trade or be
        recorded, exactly as in Phase 8 — they exist so SMA(50) and MACD are already computed on
        the first replayed day.
        """
        from datetime import timedelta

        warmup_start = request.start_date - timedelta(days=INDICATOR_WARMUP_CALENDAR_DAYS)
        rows = self.price_repo.get_history_asc(
            db, stock_id, start_date=warmup_start, end_date=request.end_date
        )
        report = validate_bars(load_bars(rows))
        bars = tuple(report.bars)
        stock = self.paper.stock_repo.get_by_id(db, stock_id)
        contexts = tuple(self.context_builder.build(stock_id, stock.symbol if stock else "", bars))
        recorded = [bar for bar in bars if request.start_date <= bar.date <= request.end_date]
        return tuple(recorded), contexts

    # ------------------------------------------------------------------
    # Resolution helpers
    # ------------------------------------------------------------------

    def _resolve_candidates(
        self,
        db: Session,
        candidates: Sequence[AllocationCandidateInput],
        fallback_prices: Optional[Dict[int, Decimal]] = None,
    ) -> List[AllocationCandidate]:
        """Turn request candidates into engine candidates with a price and a sector.

        A candidate that does not exist, or that has no price in the request and no stored close,
        is marked **ineligible with a reason** rather than dropped, so "this stock was considered
        and could not be used" reaches the caller instead of disappearing from the allocation.
        """
        prices = fallback_prices or {}
        meta = self.stock_metadata(db, [candidate.stock_id for candidate in candidates])
        resolved: List[AllocationCandidate] = []
        for candidate in candidates:
            known = candidate.stock_id in meta
            info = meta.get(candidate.stock_id, {})
            price = candidate.entry_price
            if price is None:
                price = prices.get(candidate.stock_id)
            if price is None:
                price = self._latest_price(db, candidate.stock_id)
            priced = price is not None and Decimal(price) > ZERO

            # A stock that is not in the database cannot be traded, whatever price the caller
            # supplied: the order path resolves the instrument, so approving one would promise a
            # fill that can never happen.
            if not known:
                reason = (
                    f"Stock {candidate.stock_id} does not exist, so there is nothing to allocate "
                    "to. It is reported as refused rather than dropped."
                )
            elif not priced:
                reason = (
                    f"Stock {candidate.stock_id} has no usable market price in storage or in the "
                    "request, so no position size can be derived from it. It is reported as "
                    "refused rather than dropped."
                )
            else:
                reason = None

            resolved.append(
                AllocationCandidate(
                    stock_id=candidate.stock_id,
                    entry_price=Decimal(price) if priced else ZERO,
                    stop_price=candidate.stop_price,
                    composite_score=candidate.composite_score or 0,
                    symbol=candidate.symbol or info.get("symbol"),
                    sector=candidate.sector or info.get("sector"),
                    eligible=known and priced,
                    ineligible_reason=reason,
                )
            )
        return resolved

    def stock_metadata(self, db: Session, stock_ids: Sequence[int]) -> Dict[int, dict]:
        """``{stock_id: {symbol, sector}}`` in two queries, however many ids there are."""
        from app.models.company import Company
        from app.models.stock import Stock

        unique = {stock_id for stock_id in stock_ids if stock_id is not None}
        if not unique:
            return {}
        rows = (
            db.query(Stock.id, Stock.symbol, Company.sector)
            .outerjoin(Company, Company.stock_id == Stock.id)
            .filter(Stock.id.in_(unique))
            .all()
        )
        return {row.id: {"symbol": row.symbol, "sector": row.sector} for row in rows}

    def sector_map(self, db: Session, stock_ids: Sequence[int]) -> Dict[int, Optional[str]]:
        """``{stock_id: sector}`` for the concentration rule. Unknown sectors stay ``None``."""
        return {
            stock_id: meta.get("sector")
            for stock_id, meta in self.stock_metadata(db, stock_ids).items()
        }

    @staticmethod
    def _sector_weights(valuation, sectors: Dict[int, Optional[str]]) -> Dict[str, Decimal]:
        """Current weight per known sector, for the sector cap. Unknown sectors are excluded."""
        from app.services.portfolio.calculator import allocation_pct

        totals: Dict[str, Decimal] = {}
        for position in valuation.positions:
            sector = sectors.get(position.stock_id)
            if sector is None:
                continue
            weight = allocation_pct(position.market_value, valuation.equity)
            if weight is None:
                continue
            totals[sector] = totals.get(sector, ZERO) + weight
        return totals

    def _entry_price(self, db: Session, stock_id: int, supplied: Optional[Decimal]) -> Decimal:
        """The price to size against: the request's, or the latest stored close."""
        from app.core.exceptions import InvalidOrderError
        from app.services.portfolio.calculator import quantize_price

        if supplied is not None:
            return quantize_price(Decimal(supplied))
        latest = self.price_repo.get_latest(db, stock_id)
        if latest is None or latest.close_price is None:
            raise InvalidOrderError(
                f"Stock {stock_id} has no stored price to size against; supply 'entry_price'.",
                {"stock_id": stock_id},
            )
        return quantize_price(Decimal(str(latest.close_price)))

    def _latest_price(self, db: Session, stock_id: int) -> Optional[Decimal]:
        latest = self.price_repo.get_latest(db, stock_id)
        if latest is None or latest.close_price is None:
            return None
        value = Decimal(str(latest.close_price))
        return value if value > ZERO else None

    @staticmethod
    def _override_policy(raw) -> Optional[PortfolioRiskPolicy]:
        from app.services.portfolio.models import as_risk_policy

        return None if raw is None else as_risk_policy(raw)

    @staticmethod
    def _method(raw) -> str:
        """The engine-side sizing method, compared by value so either enum class is accepted."""
        value = str(getattr(raw, "value", raw)).upper()
        if value == PositionSizingMethod.MAX_POSITION_CAP.value:
            return METHOD_MAX_POSITION_CAP
        if value == PositionSizingMethod.RISK_BASED.value:
            return METHOD_RISK_AND_WEIGHT
        return METHOD_RISK_AND_WEIGHT

    @staticmethod
    def _require_buy(side) -> None:
        if side_value(side) != OrderSide.BUY.value:
            raise UnsupportedOrderSideError(side)


def _intent(portfolio_id: int, decision) -> object:
    """Build the :class:`~app.services.portfolio.models.OrderIntent` for an approved allocation.

    ``signal_as_of`` is the allocation's own timestamp, so the simulated order records when the
    decision was taken — exactly as a Phase 10 paper-run order does.
    """
    from app.services.portfolio.models import OrderIntent

    return OrderIntent(
        portfolio_id=portfolio_id,
        stock_id=decision.stock_id,
        side=OrderSide.BUY,
        quantity=decision.quantity,
        requested_price=decision.entry_price,
        source=OrderSource.STRATEGY,
        metadata={
            "phase": 13,
            "binding_constraint": decision.binding_constraint.value
            if decision.binding_constraint
            else None,
            "risk_amount": str(decision.risk_amount),
            "stop_price": None if decision.stop_price is None else str(decision.stop_price),
            "composite_score": decision.composite_score,
        },
    )


#: Module-level service, matching the pattern of every other service in this package.
portfolio_allocation_service = PortfolioAllocationService()
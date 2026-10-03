"""Phase 11 construction service — the database-aware half of portfolio construction.

This layer resolves *state* (candidates, prices, positions, sectors) and hands pure values to
:mod:`app.services.portfolio.construction` and
:mod:`app.services.portfolio.risk_engine`. It holds no arithmetic of its own, which is what keeps
the decision rules testable without a database.

Integration with the existing layers
------------------------------------
============================  ==========================================================
Phase 7 ``ScreenerService``   decides **which** stocks qualify. Reused as-is when the caller
                              passes a ``screener`` filter set; no screening rule is duplicated.
Phase 7 ``SignalService``     supplies the composite alignment score used for the deterministic
                              candidate ordering. Also reused as-is.
Phase 6 ``PriceRepository``   supplies the latest stored close for any candidate without an
                              explicit price.
``Company.sector``            supplies the sector used by the concentration rule.
============================  ==========================================================

The screener decides *which* stocks qualify; this layer decides *how much to hold in each of
them*. Keeping the two apart is what stops portfolio rules from silently becoming screening
rules.

Determinism
-----------
``as_of`` is resolved **once**, by the caller-facing service, before any pure calculation runs —
no pure function reads the clock. Candidates are sorted by composite score descending then
stock id ascending, and the stock master's identifiers break every remaining tie, so the same
inputs always produce the same decisions in the same order.

Stated limitation
-----------------
The pre-trade risk check behind each proposed ``BUY`` is evaluated against the portfolio's
**stored** state, so two proposed buys in one run are each checked against the portfolio as it is
before the run, not after the first of them. What the run *does* account for is the cash and the
uninvested equity each proposal consumes: the engine carries a running budget, so the last buy in
a run can never be sized against cash the earlier buys in that same run already spent. Portfolio
limits whose value changes only once orders settle (notional exposure, the position count) are
therefore checked per proposal against the pre-run portfolio, and the executor re-checks them
against the live state when the decision is applied.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from app.core.exceptions import AppException, InvalidRebalanceRequestError
from app.core.logging import logger
from app.models.company import Company
from app.models.portfolio import Portfolio
from app.models.stock import Stock
from app.repositories.price_repository import price_repository
from app.schemas.allocation import (
    PortfolioCandidateInput,
    PortfolioDecisionRequest,
    RebalanceRequest,
)
from app.schemas.portfolio import OrderCreate, OrderSide, OrderSource, PortfolioStatus
from app.schemas.screeners import TechnicalScreenerRequest
from app.services.backtest.models import quantize_money
from app.services.portfolio.allocation import normalise_weights
from app.services.portfolio.calculator import (
    apply_slippage,
    calculate_fee,
    gross_value,
    quantize_price,
)
from app.services.portfolio.construction import build_decisions
from app.services.portfolio.models import (
    ZERO,
    AllocationConfig,
    Candidate,
    PortfolioDecisions,
    PortfolioRiskReport,
    PortfolioValuation,
    RebalancePlan,
)
from app.services.portfolio.rebalancing import derive_target_weights, plan_rebalance
from app.services.portfolio.risk_engine import PortfolioRiskEngine
from app.services.portfolio.service import PaperTradingService, paper_trading_service

#: Upper bound on the total weight a rebalance may allocate, as a percentage of equity. Targets
#: summing above this are refused rather than scaled down: silently rescaling would make the
#: executed portfolio differ from the requested one.
MAX_TOTAL_TARGET_WEIGHT_PCT = Decimal("100")


class PortfolioConstructionService:
    """Builds portfolio decisions and rebalancing plans from stored state."""

    def __init__(
        self,
        trading: Optional[PaperTradingService] = None,
        risk_engine: Optional[PortfolioRiskEngine] = None,
        screener_service=None,
    ):
        self.trading = trading or paper_trading_service
        self.risk_engine = risk_engine or PortfolioRiskEngine()
        # Imported lazily: the screener depends on the signal service, and pulling it in at module
        # scope would make the portfolio package import the whole Phase 7 stack for a caller that
        # only ever supplies explicit candidates.
        self._screener_service = screener_service

    # ------------------------------------------------------------------
    # Candidates
    # ------------------------------------------------------------------

    @property
    def screener(self):
        """The existing Phase 7 screener, resolved on first use."""
        if self._screener_service is None:
            from app.services.screener_service import screener_service

            self._screener_service = screener_service
        return self._screener_service

    def candidates_from_screener(
        self, db: Session, request: TechnicalScreenerRequest
    ) -> List[PortfolioCandidateInput]:
        """Run the existing screener and return its matches as candidates.

        The screener already orders its results by composite score, and the candidate ordering
        applied downstream sorts them again by ``(score desc, stock_id asc)``, so a screen that
        returned two equally-scored stocks in either order still yields identical decisions.
        """
        response = self.screener.screen(db, request)
        return [
            PortfolioCandidateInput(
                stock_id=result.stock_id,
                price=result.latest_price,
                composite_score=result.composite_score,
                symbol=result.symbol,
            )
            for result in response.results
        ]

    def resolve_candidates(
        self,
        db: Session,
        candidates: Sequence[PortfolioCandidateInput],
        fallback_prices: Optional[Dict[int, Decimal]] = None,
    ) -> List[Candidate]:
        """Turn a request's candidates into engine candidates with a price and a sector.

        A candidate with no price supplied and no stored close is marked **ineligible with a
        reason** rather than dropped, so "this stock was considered and could not be priced"
        reaches the caller instead of disappearing from the decision list.
        """
        prices = fallback_prices or {}
        stock_ids = [candidate.stock_id for candidate in candidates]
        meta = self._stock_metadata(db, stock_ids)
        resolved: List[Candidate] = []
        for candidate in candidates:
            info = meta.get(candidate.stock_id, {})
            price = candidate.price
            if price is None:
                price = prices.get(candidate.stock_id)
            if price is None:
                price = self._latest_price(db, candidate.stock_id)
            price = Decimal(price) if price is not None else None
            eligible = price is not None and price > ZERO
            reason = None
            if not eligible:
                reason = (
                    f"Stock {candidate.stock_id} has no usable market price in storage or in the "
                    "request, so no position size can be derived from it. It is reported as "
                    "skipped rather than dropped."
                )
            resolved.append(
                Candidate(
                    stock_id=candidate.stock_id,
                    price=price,
                    composite_score=candidate.composite_score
                    if candidate.composite_score is not None
                    else 0,
                    symbol=candidate.symbol or info.get("symbol"),
                    sector=candidate.sector or info.get("sector"),
                    eligible=eligible,
                    ineligible_reason=reason,
                )
            )
        return resolved

    def _stock_metadata(self, db: Session, stock_ids: Sequence[int]) -> Dict[int, dict]:
        """``{stock_id: {symbol, sector}}`` in two queries, regardless of how many ids there are."""
        if not stock_ids:
            return {}
        rows = (
            db.query(Stock.id, Stock.symbol, Company.sector)
            .outerjoin(Company, Company.stock_id == Stock.id)
            .filter(Stock.id.in_(set(stock_ids)))
            .all()
        )
        return {row.id: {"symbol": row.symbol, "sector": row.sector} for row in rows}

    @staticmethod
    def _latest_price(db: Session, stock_id: int) -> Optional[Decimal]:
        latest = price_repository.get_latest(db, stock_id)
        if latest is None or latest.close_price is None:
            return None
        value = Decimal(str(latest.close_price))
        return value if value > ZERO else None

    def sector_map(self, db: Session, stock_ids: Sequence[int]) -> Dict[int, Optional[str]]:
        """``{stock_id: sector}`` for the concentration rule. Unknown sectors stay ``None``."""
        return {
            stock_id: meta.get("sector")
            for stock_id, meta in self._stock_metadata(db, stock_ids).items()
        }

    # ------------------------------------------------------------------
    # Decisions
    # ------------------------------------------------------------------

    def decide(
        self,
        db: Session,
        portfolio: Portfolio,
        request: PortfolioDecisionRequest,
        as_of: datetime,
    ) -> PortfolioDecisions:
        """Every decision for ``portfolio``, with no state changed.

        ``as_of`` is supplied by the caller so the timestamp is fixed once, outside the pure
        engine, and the same ``as_of`` produces byte-identical decisions on a repeat run.
        """
        config = self._effective_config(portfolio, request)
        prices = _price_map(request.prices)
        valuation = self.trading.calculate_portfolio_value(db, portfolio.id, prices)
        risk_report = self.risk_report(db, portfolio, config, valuation, as_of=as_of)
        candidates = self.resolve_candidates(
            db, self._request_candidates(db, portfolio, request), fallback_prices=prices
        )

        decisions = build_decisions(
            portfolio_id=portfolio.id,
            status=PortfolioStatus(portfolio.status),
            as_of=as_of,
            equity=valuation.equity,
            cash=valuation.cash,
            invested_value=valuation.market_value,
            candidates=candidates,
            positions=valuation.positions,
            config=config,
            costs=self.trading.trading_costs(portfolio),
            risk_report=risk_report,
            risk_check=self._risk_check(db, portfolio),
        )

        logger.info(
            "Portfolio %s construction: candidates=%s eligible=%s decisions=%s skipped=%s "
            "sizing_method=%s equity=%s",
            portfolio.id,
            decisions.candidate_count,
            decisions.eligible_count,
            len(decisions.decisions),
            len(decisions.skipped),
            decisions.sizing_method.value,
            decisions.equity,
        )
        return decisions

    def risk_report(
        self,
        db: Session,
        portfolio: Portfolio,
        config: Optional[AllocationConfig] = None,
        valuation: Optional[PortfolioValuation] = None,
        prices: Optional[Dict[int, Decimal]] = None,
        as_of: Optional[datetime] = None,
    ) -> PortfolioRiskReport:
        """The portfolio-level risk report for ``portfolio``.

        ``allocation_config_json`` is read rather than a passed-in configuration when none is
        given, so the report always describes the policy actually stored for the portfolio.
        ``prices`` marks the portfolio before every figure is derived; it is the same map the
        caller passes to :meth:`rebalance`, so a decision and the report behind it can be taken
        at one set of prices. ``as_of`` is stamped on the report so it and the decision run
        behind it share one timestamp instead of each reading the clock.
        """
        if valuation is None:
            valuation = self.trading.calculate_portfolio_value(db, portfolio.id, prices)
        if config is None:
            config = self.trading.allocation_config(portfolio)
        limits = self.trading.risk_limits(portfolio)
        account = self.trading.account_state(db, portfolio)
        peak = self.trading.peak_equity(db, portfolio, account)
        sectors = self.sector_map(db, [position.stock_id for position in valuation.positions])
        report = self.risk_engine.evaluate(
            valuation,
            config=config,
            limits=limits,
            peak_equity=peak,
            sectors=sectors,
        )
        return replace(report, as_of=as_of or datetime.now(timezone.utc))

    def rebalance(
        self,
        db: Session,
        portfolio: Portfolio,
        request: RebalanceRequest,
        as_of: datetime,
    ) -> RebalancePlan:
        """Every stock's rebalancing verdict, with no state changed."""
        config = self._effective_rebalance_config(portfolio, request)
        prices = _price_map(request.prices)
        valuation = self.trading.calculate_portfolio_value(db, portfolio.id, prices)
        targets = self._resolve_targets(db, request, config, valuation.equity)
        symbols = self._stock_metadata(db, list(targets))
        sectors = {stock_id: meta.get("sector") for stock_id, meta in symbols.items()}

        items = plan_rebalance(
            equity=valuation.equity,
            positions=valuation.positions,
            target_weights=targets,
            config=config,
            costs=self.trading.trading_costs(portfolio),
            prices=prices,
            drop_unlisted=request.drop_unlisted,
            valuation=valuation,
            symbols={stock_id: meta.get("symbol") for stock_id, meta in symbols.items()},
            sectors=sectors,
        )
        items = self._annotate_risk_blocks(db, portfolio, items, valuation.cash)

        plan = RebalancePlan(
            portfolio_id=portfolio.id,
            status=PortfolioStatus(portfolio.status),
            as_of=as_of,
            equity=valuation.equity,
            cash=valuation.cash,
            invested_value=valuation.market_value,
            rebalance_threshold_pct=config.rebalance_threshold_pct,
            target_weights=tuple((stock_id, weight) for stock_id, weight in sorted(targets.items())),
            items=items,
            configuration=config,
        )
        logger.info(
            "Portfolio %s rebalance: targets=%s items=%s trades=%s estimated_value=%s "
            "threshold=%s",
            portfolio.id,
            len(targets),
            len(items),
            len(plan.trades),
            plan.estimated_total_value,
            config.rebalance_threshold_pct,
        )
        return plan

    # ------------------------------------------------------------------
    # Applying decisions
    # ------------------------------------------------------------------

    def apply_decisions(
        self,
        db: Session,
        portfolio: Portfolio,
        decisions: Sequence,
    ) -> Tuple:
        """Submit the actionable, unblocked decisions as simulated paper orders.

        Returns ``(decision, result)`` pairs in the decision order. A decision the engine blocked
        is skipped untouched and keeps its blocking reason, so applying a plan never silently
        retries a trade the risk engine refused.

        Orders go through :meth:`~app.services.portfolio.service.PaperTradingService.place_order`,
        which re-runs the full validation and risk check against live state. A rejection there is
        caught and reported on the result rather than raised, so one unfundable proposal does not
        abandon the rest of the plan.
        """
        results = []
        for decision in decisions:
            if decision.blocked or not decision.requires_trade or decision.price is None:
                results.append((decision, None))
                continue
            quantity = abs(decision.target_quantity - decision.current_quantity)
            if quantity <= 0:
                results.append((decision, None))
                continue
            side = OrderSide.BUY if decision.target_quantity > decision.current_quantity else OrderSide.SELL
            try:
                result = self.trading.place_order(
                    db,
                    portfolio.id,
                    OrderCreate(
                        stock_id=decision.stock_id,
                        side=side,
                        quantity=quantity,
                        requested_price=decision.price,
                        source=OrderSource.STRATEGY,
                        idempotency_key=_decision_key(portfolio.id, decision),
                        metadata={
                            "engine": "phase11_construction",
                            "action": decision.action.value,
                            "target_weight_pct": str(decision.target_weight_pct),
                            "reasons": list(decision.reasons),
                        },
                    ),
                )
            except AppException as exc:
                logger.warning(
                    "Portfolio %s could not apply %s for stock %s: %s",
                    portfolio.id,
                    decision.action.value,
                    decision.stock_id,
                    exc.message,
                )
                results.append((decision, None))
                continue
            results.append((decision, result))
        return tuple(results)

    def apply_rebalance(
        self, db: Session, portfolio: Portfolio, plan: RebalancePlan
    ) -> Tuple:
        """Submit the proposed rebalancing trades as simulated paper orders.

        Trades are submitted in :func:`_trade_order` — sells before buys — because that is the
        order the plan's own funding check assumed. The returned pairs are still keyed by stock
        id, so the caller matches them to ``plan.items`` without caring about the order the
        orders actually went out in.
        """
        results = []
        for item in _trade_order(plan.trades):
            side = OrderSide.BUY if item.required_quantity > 0 else OrderSide.SELL
            try:
                result = self.trading.place_order(
                    db,
                    portfolio.id,
                    OrderCreate(
                        stock_id=item.stock_id,
                        side=side,
                        quantity=abs(item.required_quantity),
                        requested_price=item.price,
                        source=OrderSource.STRATEGY,
                        idempotency_key=_rebalance_key(portfolio.id, plan, item),
                        metadata={
                            "engine": "phase11_rebalance",
                            "action": item.action.value,
                            "target_weight_pct": str(item.target_weight_pct),
                            "weight_difference_pct": str(item.weight_difference_pct),
                            "reasons": list(item.reasons),
                        },
                    ),
                )
            except AppException as exc:
                logger.warning(
                    "Portfolio %s could not apply rebalance %s for stock %s: %s",
                    portfolio.id,
                    side.value,
                    item.stock_id,
                    exc.message,
                )
                results.append((item, None))
                continue
            results.append((item, result))
        return tuple(results)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _request_candidates(
        self,
        db: Session,
        portfolio: Portfolio,
        request: PortfolioDecisionRequest,
    ) -> List[PortfolioCandidateInput]:
        """The candidate list, from the screener when one was supplied.

        With neither candidates nor a screener, the *held* positions become the candidate list.
        That is how a portfolio discovers a stop-loss or take-profit it has already reached: no new
        stock needs to be screened to ask "should I still be holding what I have?".
        """
        if request.candidates:
            return list(request.candidates)
        if request.screener is not None:
            return self.candidates_from_screener(db, request.screener)
        return [
            PortfolioCandidateInput(stock_id=position.stock_id)
            for position in self.trading.get_positions(db, portfolio.id)
        ]

    def _resolve_targets(
        self,
        db: Session,
        request: RebalanceRequest,
        config: AllocationConfig,
        equity: Decimal,
    ) -> Dict[int, Decimal]:
        """The target weights this rebalance trades towards, validated before use."""
        if request.target_weights is not None:
            try:
                return normalise_weights(
                    dict(request.target_weights),
                    max_total_pct=MAX_TOTAL_TARGET_WEIGHT_PCT,
                )
            except ValueError as exc:
                raise InvalidRebalanceRequestError(str(exc)) from exc

        candidates = self._rebalance_candidates(db, request)
        ordered = sorted(
            self.resolve_candidates(db, candidates),
            key=Candidate.sort_key,
        )
        ids = [candidate.stock_id for candidate in ordered]
        try:
            return derive_target_weights(ids, config)
        except ValueError as exc:
            raise InvalidRebalanceRequestError(str(exc)) from exc

    def _rebalance_candidates(
        self, db: Session, request: RebalanceRequest
    ) -> List[PortfolioCandidateInput]:
        if request.candidates:
            return list(request.candidates)
        return self.candidates_from_screener(db, request.screener)

    def _effective_config(
        self, portfolio: Portfolio, request: PortfolioDecisionRequest
    ) -> AllocationConfig:
        """The stored configuration with this call's non-``None`` overrides applied."""
        config = self.trading.allocation_config(portfolio)
        overrides = {
            "sizing_method": request.sizing_method,
            "max_position_weight_pct": request.max_position_weight_pct,
            "risk_per_trade_pct": request.risk_per_trade_pct,
            "cash_reserve_pct": request.cash_reserve_pct,
            "stop_loss_pct": request.stop_loss_pct,
            "take_profit_pct": request.take_profit_pct,
            "rebalance_threshold_pct": request.rebalance_threshold_pct,
            "max_positions": request.max_positions,
        }
        changes = {
            name: value for name, value in overrides.items() if value is not None
        }
        return config.replace(**changes) if changes else config

    def _effective_rebalance_config(
        self, portfolio: Portfolio, request: RebalanceRequest
    ) -> AllocationConfig:
        config = self.trading.allocation_config(portfolio)
        changes = {
            name: value
            for name, value in {
                "sizing_method": request.sizing_method,
                "rebalance_threshold_pct": request.rebalance_threshold_pct,
            }.items()
            if value is not None
        }
        return config.replace(**changes) if changes else config

    def _risk_check(self, db: Session, portfolio: Portfolio):
        """A callable the decision engine uses to pre-trade check each proposed buy.

        Returns the machine-readable rule names that would refuse the order, so a ``BUY`` decision
        is already risk-aware. It reuses the existing
        :class:`~app.services.portfolio.risk_service.RiskCheckService`, so a decision can never
        disagree with what the executor would actually do.
        """

        def check(stock_id: int, quantity: int, price: Decimal) -> Tuple[str, ...]:
            if quantity <= 0 or price is None or price <= ZERO:
                return ()
            assessment = self.trading.risk.check(
                db,
                portfolio,
                stock_id=stock_id,
                side=OrderSide.BUY,
                quantity=quantity,
                price=price,
                costs=self.trading.trading_costs(portfolio),
                limits=self.trading.risk_limits(portfolio),
            )
            return assessment.rules

        return check

    def _annotate_risk_blocks(
        self,
        db: Session,
        portfolio: Portfolio,
        items: Tuple,
        cash: Decimal,
    ) -> Tuple:
        """Mark proposed rebalancing trades the risk engine would refuse.

        A rebalance that only reported the drift would happily propose a buy the executor then
        rejects, which reads as an engine failure. Preflighting every proposed trade means the
        plan says which trades are actually executable, and why the others are not.

        The preflight treats the whole plan as one instruction, so a buy is checked against the
        cash the plan's **own sells** release. That is the difference between "this rebalance is
        unfundable" and "this individual buy is unfundable", and only the second is true here:
        selling to fund a rebalance is the ordinary case, not an exception, and a plan that
        refused its own buys for want of cash it had just committed to raise would be reporting a
        fiction.

        Items are walked in **trade order**, not in the order they are reported:
        :func:`plan_rebalance` sorts its items by stock id so the report is stable, which is the
        right order to *read* them in and the wrong order to *fund* them in, so the trading order
        is derived here rather than taken from the item list. Only the trades are walked that way —
        the holds are not trades and are carried through untouched, so annotating a plan can never
        shorten it.

        A buy is still checked against the live portfolio for everything else — position count,
        per-position size, exposure, drawdown — because those only settle once orders execute.
        """
        results = {}
        available = cash
        for item in _trade_order(items):
            # Unreachable while ``_trade_order`` yields only sells and buys, and kept so that
            # widening that helper later cannot start preflighting a zero-share order.
            if item.required_quantity == 0:
                continue
            # Sells are preflighted against the state they find and release cash as they pass;
            # buys are preflighted against the cash left by the sells before them.
            if item.required_quantity < 0:
                assessment = self.trading.risk.check(
                    db,
                    portfolio,
                    stock_id=item.stock_id,
                    side=OrderSide.SELL,
                    quantity=abs(item.required_quantity),
                    price=item.price,
                    costs=self.trading.trading_costs(portfolio),
                    limits=self.trading.risk_limits(portfolio),
                )
            else:
                assessment = self.trading.risk.check(
                    db,
                    portfolio,
                    stock_id=item.stock_id,
                    side=OrderSide.BUY,
                    quantity=item.required_quantity,
                    price=item.price,
                    costs=self.trading.trading_costs(portfolio),
                    limits=self.trading.risk_limits(portfolio),
                    available_cash=available,
                )
            if not assessment.accepted:
                results[item.stock_id] = replace(
                    item,
                    blocked=True,
                    reasons=tuple(item.reasons) + assessment.rules,
                    explanation=tuple(item.explanation)
                    + tuple(
                        "Blocked: the pre-trade risk engine refused this trade on rule "
                        f"'{rule}'."
                        for rule in assessment.rules
                    ),
                )
                continue
            if item.required_quantity < 0:
                # Only cash that will actually arrive counts towards funding the buys below.
                available = quantize_money(
                    available + _net_sell_proceeds(item, self.trading.trading_costs(portfolio))
                )
            else:
                available = quantize_money(
                    available - _net_buy_cost(item, self.trading.trading_costs(portfolio))
                )
        # Keyed by stock id, so the plan is reassembled in the order it was planned rather than
        # the order it was funded: an item the risk engine did not touch keeps its place.
        return tuple(results.get(item.stock_id, item) for item in items)


def _trade_order(items: Tuple) -> Tuple:
    """The order a rebalancing plan must be executed in: sells first, then buys.

    Sells release cash and buys consume it, so a plan that buys before it sells would fund
    nothing it could not already afford. Within the sells, the largest proceeds come first,
    because they release the most cash soonest; within the buys, the smallest costs come first,
    for the same reason a pay-as-you-go payer pays the small bills. ``stock_id`` breaks every
    tie, so the order is total and reproducible.

    This is the *execution* order. The items are still *reported* in stock-id order, which is
    what makes the response line up with the portfolio's positions.
    """
    sells = sorted(
        (item for item in items if item.required_quantity < 0),
        key=lambda item: (-item.estimated_trade_value, item.stock_id),
    )
    buys = sorted(
        (item for item in items if item.required_quantity > 0),
        key=lambda item: (item.estimated_trade_value, item.stock_id),
    )
    return tuple(sells) + tuple(buys)


def _net_buy_cost(item, costs) -> Decimal:
    """What a proposed buy takes out of cash: gross value plus the fees it pays."""
    price = quantize_price(Decimal(item.price))
    value = quantize_money(gross_value(item.required_quantity, apply_slippage(price, True, costs)))
    return quantize_money(value + calculate_fee(value, costs))


def _net_sell_proceeds(item, costs) -> Decimal:
    """What a proposed sell puts into cash: gross value less the fees it pays.

    Slippage works against a sell, so it is applied in the seller's favour here exactly as the
    executor will apply it — otherwise the plan would promise cash the fill would not deliver.
    """
    price = quantize_price(Decimal(item.price))
    value = quantize_money(gross_value(abs(item.required_quantity), apply_slippage(price, False, costs)))
    return quantize_money(value - calculate_fee(value, costs))


def _price_map(inputs) -> Dict[int, Decimal]:
    """``{stock_id: price}`` from the request's price list. Later entries win, deterministically."""
    prices: Dict[int, Decimal] = {}
    for item in inputs or ():
        prices[item.stock_id] = Decimal(str(item.price))
    return prices


def _decision_key(portfolio_id: int, decision) -> str:
    """Idempotency key for a decision, so re-applying the same plan cannot double-trade."""
    return (
        f"p11-decision-{portfolio_id}-{decision.stock_id}-{decision.action.value}-"
        f"{decision.target_quantity}"
    )


def _rebalance_key(portfolio_id: int, plan: RebalancePlan, item) -> str:
    """Idempotency key for a rebalancing trade, scoped to the plan's own timestamp."""
    return (
        f"p11-rebalance-{portfolio_id}-{item.stock_id}-"
        f"{plan.as_of.strftime('%Y%m%dT%H%M%S')}-{item.required_quantity}"
    )


portfolio_construction_service = PortfolioConstructionService()
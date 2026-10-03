"""Pre-trade risk evaluation and effective risk reporting.

Two read-only operations, both of which never mutate portfolio state:

``check``
    Would the risk engine accept this hypothetical trade? It projects the post-fill state of
    the portfolio — exactly the projection
    :class:`~app.services.portfolio.executor.OrderExecutor` computes before executing — and
    runs the same :class:`~app.services.portfolio.risk.RiskManager` rules against it. Every
    violated rule is reported, not only the first, so a caller can see a trade that is over
    both its exposure and its cash-reserve limit in one call.

``effective_limits``
    Which rules are in force for this portfolio right now, and which of them the portfolio is
    *already* breaching (for example a drawdown guard that has been triggered). This is a
    statement about the current state, which is different from "could not take another
    trade".

Both build the projection through one shared helper, so the numbers a pre-trade check reports
are by construction the numbers the executor would use. The projection itself lives in
:func:`project_buy` here rather than in the executor only because the check needs to report a
*rejected* order's projection without executing anything.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Optional

from sqlalchemy.orm import Session

from app.core.logging import logger
from app.models.portfolio import Portfolio
from app.repositories.order_repository import order_repository
from app.repositories.portfolio_snapshot_repository import portfolio_snapshot_repository
from app.repositories.portfolio_transaction_repository import portfolio_transaction_repository
from app.repositories.trade_repository import trade_repository
from app.schemas.portfolio import OrderSide, PortfolioStatus
from app.services.backtest.models import quantize_money
from app.services.portfolio.calculator import (
    apply_buy,
    apply_slippage,
    calculate_fee,
    daily_loss_pct,
    drawdown_pct,
    equity,
    exposure_pct,
    gross_value,
    invested_basis,
    market_value,
)
from app.services.portfolio.models import (
    ZERO,
    PortfolioAccount,
    Rejection,
    RiskAssessment,
    RiskLimits,
    RiskViolation,
    TradingCostConfig,
)
from app.services.portfolio.risk import RiskManager, insufficient_cash_rejection, peak_equity
from app.services.portfolio.valuation_service import PortfolioValuationService


def day_open_equity(
    db: Session,
    portfolio: Portfolio,
    *,
    as_of: Optional[datetime] = None,
    snapshot_repo=portfolio_snapshot_repository,
    transaction_repo=portfolio_transaction_repository,
) -> Decimal:
    """Equity at the start of the calendar day ``as_of`` falls in (UTC).

    The daily-loss rule is measured against this, and it is resolved in three steps, from the
    most precise evidence to the least:

    1. the **last snapshot recorded before today** — the portfolio's own record of where the day
       began;
    2. failing that, the **first snapshot recorded today** — a portfolio that started recording
       today has no earlier point, and its first point is its closest thing to an opening equity;
    3. failing that, the **invested basis** (initial capital plus deposits minus withdrawals) —
       what the portfolio would be worth with no market movement. This last fallback matters:
       without it, a portfolio created today that received a deposit would look as if it had
       already lost that deposit, and the daily-loss rule would refuse to let it trade.

    The result is deterministic: the same stored history always yields the same figure.
    """
    moment = as_of or datetime.now(timezone.utc).replace(tzinfo=None)

    recorded = snapshot_repo.latest_before(db, portfolio.id, moment.replace(
        hour=0, minute=0, second=0, microsecond=0
    ))
    if recorded is not None:
        return quantize_money(Decimal(recorded.equity))

    first_today = snapshot_repo.earliest_on(db, portfolio.id, moment)
    if first_today is not None:
        return quantize_money(Decimal(first_today.equity))

    return invested_basis(
        Decimal(portfolio.initial_capital), transaction_repo.external_flow(db, portfolio.id)
    )


class RiskCheckService:
    """Answers "would this trade be accepted?" without executing anything."""

    def __init__(
        self,
        valuation_service: Optional[PortfolioValuationService] = None,
        order_repo=order_repository,
        trade_repo=trade_repository,
        snapshot_repo=portfolio_snapshot_repository,
        transaction_repo=portfolio_transaction_repository,
    ):
        self.valuations = valuation_service or PortfolioValuationService()
        self.order_repo = order_repo
        self.trade_repo = trade_repo
        self.snapshot_repo = snapshot_repo
        self.transaction_repo = transaction_repo

    # --- Pre-trade check ---

    def check(
        self,
        db: Session,
        portfolio: Portfolio,
        *,
        stock_id: int,
        side: OrderSide,
        quantity: int,
        price: Decimal,
        costs: TradingCostConfig,
        limits: RiskLimits,
        available_cash: Optional[Decimal] = None,
        as_of: Optional[datetime] = None,
    ) -> RiskAssessment:
        """Evaluate a hypothetical trade against the portfolio's risk limits.

        The structural checks that do not depend on a projection (an inactive portfolio, a
        non-positive quantity or price, an unsupported order type) are reported first,
        followed by the funding/coverage checks, followed by every configured growth limit.

        ``available_cash`` overrides the cash the funding check is measured against, without
        changing any other figure. It exists for a caller checking several trades *as one plan*
        — the rebalancer needs this, because the cash a buy needs is often the cash a sell in
        the same rebalance releases. Passing it states that explicitly; leaving it ``None`` keeps
        the plain "what can this single trade afford right now" reading that the pre-trade
        check endpoint reports.

        ``as_of`` is the day the daily-loss rule is measured against; it defaults to today.
        """
        account = self.account(db, portfolio, stock_id)
        if available_cash is not None:
            account = replace(account, cash=available_cash)
        valuation = self.valuations.value(db, portfolio)
        peak = self.peak_equity(db, portfolio, account.equity)
        day_open = (
            day_open_equity(
                db,
                portfolio,
                as_of=as_of,
                snapshot_repo=self.snapshot_repo,
                transaction_repo=self.transaction_repo,
            )
            if limits.max_daily_loss_pct is not None
            else None
        )
        manager = RiskManager(limits)
        violations: list = []

        # ``Portfolio.status`` is a plain string column, so compare on the value directly rather
        # than reaching for the enum member.
        if portfolio.status != PortfolioStatus.ACTIVE.value:
            return RiskAssessment(
                portfolio_id=portfolio.id,
                accepted=False,
                violations=(
                    RiskViolation(
                        rule="portfolio_status",
                        reason=(
                            f"Portfolio {portfolio.id} is {portfolio.status} and cannot "
                            "accept orders."
                        ),
                        details={"status": portfolio.status, "portfolio_id": portfolio.id},
                    ),
                ),
                equity=valuation.equity,
                cash=valuation.cash,
                peak_equity=peak,
                current_drawdown_pct=drawdown_pct(peak, valuation.equity),
                exposure_pct=valuation.exposure_pct,
                position_count=account.position_count,
                limits=limits,
            )

        if quantity <= 0:
            violations.append(
                RiskViolation(
                    rule="positive_quantity",
                    reason=f"Order quantity must be a positive whole number, got {quantity}.",
                    observed=Decimal(quantity),
                )
            )
        if price <= ZERO:
            violations.append(
                RiskViolation(
                    rule="positive_price",
                    reason=f"Order price must be greater than zero, got {price}.",
                    observed=Decimal(price),
                )
            )
        if violations:
            return self._assessment(portfolio, valuation, peak, account, violations, limits)

        if side is OrderSide.SELL:
            position = account.position_for(stock_id)
            owned = position.quantity if position else 0
            if owned <= 0:
                violations.append(
                    RiskViolation(
                        rule="oversell_protection",
                        reason=(
                            f"No open position in stock {stock_id}, so {quantity} share(s) "
                            "cannot be sold. Paper trading is long-only."
                        ),
                        observed=Decimal(quantity),
                        details={"stock_id": stock_id, "owned_quantity": "0"},
                    )
                )
            elif quantity > owned:
                violations.append(
                    RiskViolation(
                        rule="oversell_protection",
                        reason=(
                            f"Cannot sell {quantity} share(s): only {owned} held in stock "
                            f"{stock_id}. A position can never go negative."
                        ),
                        observed=Decimal(quantity),
                        details={"stock_id": stock_id, "owned_quantity": str(owned)},
                    )
                )
            for rejection in manager.min_trade_value_violation(
                gross_value(quantity, apply_slippage(price, False, costs))
            ):
                violations.append(_violation_from_rejection(rejection))
            # Growth limits never block a sell: it always reduces risk.
            return self._assessment(portfolio, valuation, peak, account, violations, limits)

        projection = self.project_buy(account, stock_id, quantity, price, costs)
        if projection is None:
            required = quantize_money(
                gross_value(quantity, apply_slippage(price, True, costs))
                + calculate_fee(gross_value(quantity, apply_slippage(price, True, costs)), costs)
            )
            rejection = insufficient_cash_rejection(required, account.cash)
            violations.append(_violation_from_rejection(rejection))
        else:
            for rejection in manager.violations_for_buy(
                resulting_quantity=projection["resulting_quantity"],
                resulting_position_value=projection["resulting_position_value"],
                projected_equity=projection["projected_equity"],
                peak_equity=peak,
                resulting_total_market_value=projection["resulting_total_market_value"],
                cash_after=projection["cash_after"],
                trade_value=projection["trade_value"],
                open_position_count=account.position_count,
                opening_new_position=projection["opening_new_position"],
                day_open_equity=day_open,
            ):
                violations.append(_violation_from_rejection(rejection))

        return self._assessment(portfolio, valuation, peak, account, violations, limits)

    # --- Effective limits ---

    def effective_limits(
        self,
        db: Session,
        portfolio: Portfolio,
        limits: RiskLimits,
        prices: Optional[Dict[int, Decimal]] = None,
    ) -> dict:
        """Current risk posture: configured limits plus any rule already breached.

        ``prices`` marks the portfolio before the rules are read, so a caller holding a
        valuation it has already taken (the Phase 11 risk report does) can pass the same marks
        and have both sides evaluated against one set of prices. Omit it to mark to the latest
        stored closes.
        """
        account = self.account(db, portfolio)
        valuation = self.valuations.value(db, portfolio, prices)
        peak = self.peak_equity(db, portfolio, account.equity)
        current = drawdown_pct(peak, valuation.equity)

        violations: list = []
        if limits.max_exposure_pct is not None and valuation.exposure_pct is not None:
            if valuation.exposure_pct > limits.max_exposure_pct:
                violations.append(
                    RiskViolation(
                        rule="max_exposure_pct",
                        reason=(
                            f"Portfolio exposure is {valuation.exposure_pct}% of equity, "
                            f"above the {limits.max_exposure_pct}% limit."
                        ),
                        limit=limits.max_exposure_pct,
                        observed=valuation.exposure_pct,
                    )
                )
        if limits.max_positions is not None and account.position_count > limits.max_positions:
            violations.append(
                RiskViolation(
                    rule="max_positions",
                    reason=(
                        f"Portfolio holds {account.position_count} open position(s), above the "
                        f"{limits.max_positions} limit."
                    ),
                    observed=Decimal(account.position_count),
                    details={"limit": limits.max_positions},
                )
            )
        reserve = quantize_money(
            valuation.equity * limits.min_cash_reserve_pct / Decimal("100")
        ) if limits.min_cash_reserve_pct is not None else quantize_money(ZERO)
        if limits.min_cash_reserve_pct is not None and valuation.cash < reserve:
            violations.append(
                RiskViolation(
                    rule="min_cash_reserve_pct",
                    reason=(
                        f"Cash is {valuation.cash} but {reserve} is required "
                        f"({limits.min_cash_reserve_pct}% of equity)."
                    ),
                    limit=limits.min_cash_reserve_pct,
                    observed=valuation.cash,
                    details={"required_reserve": str(reserve)},
                )
            )
        breached_drawdown = (
            limits.max_drawdown_pct is not None and current >= limits.max_drawdown_pct
        )
        if breached_drawdown:
            violations.append(
                RiskViolation(
                    rule="max_drawdown_pct",
                    reason=(
                        f"Drawdown is {current}% from the peak equity of {peak}, at or beyond "
                        f"the {limits.max_drawdown_pct}% limit, so new BUY orders are blocked."
                    ),
                    limit=limits.max_drawdown_pct,
                    observed=current,
                    details={"peak_equity": str(peak)},
                )
            )

        day_open = None
        if limits.max_daily_loss_pct is not None:
            day_open = day_open_equity(
                db,
                portfolio,
                snapshot_repo=self.snapshot_repo,
                transaction_repo=self.transaction_repo,
            )
            todays_loss = daily_loss_pct(day_open, valuation.equity)
            if todays_loss >= limits.max_daily_loss_pct:
                violations.append(
                    RiskViolation(
                        rule="max_daily_loss_pct",
                        reason=(
                            f"Today's loss is {todays_loss}% of the {day_open} the portfolio "
                            f"started the day with, at or beyond the "
                            f"{limits.max_daily_loss_pct}% limit, so new BUY orders are blocked."
                        ),
                        limit=limits.max_daily_loss_pct,
                        observed=todays_loss,
                        details={"day_open_equity": str(day_open)},
                    )
                )

        if violations:
            logger.warning(
                "Portfolio %s is breaching %d configured risk rule(s): %s",
                portfolio.id,
                len(violations),
                ", ".join(violation.rule for violation in violations),
            )
        return {
            "valuation": valuation,
            "account": account,
            "peak_equity": peak,
            "current_drawdown_pct": current,
            "cash_reserve_required": reserve,
            "violations": violations,
            "drawdown_breached": breached_drawdown,
            "day_open_equity": day_open,
            "exposure_pct": exposure_pct(valuation.market_value, valuation.equity),
        }

    # --- Shared projection ---

    @staticmethod
    def project_buy(
        account: PortfolioAccount,
        stock_id: int,
        quantity: int,
        price: Decimal,
        costs: TradingCostConfig,
    ) -> Optional[dict]:
        """Post-fill state of a buy, or ``None`` when it cannot be funded.

        Identical arithmetic to the executor, so a check and an execution can never disagree
        about whether a trade breaches a limit.
        """
        fill_price = apply_slippage(price, True, costs)
        value = gross_value(quantity, fill_price)
        fees = calculate_fee(value, costs)
        total_cost = quantize_money(value + fees)
        if total_cost > account.cash:
            return None

        position_before = account.position_for(stock_id)
        new_position, cash_impact = apply_buy(
            position_before, quantity, fill_price, fees, stock_id
        )
        previous_value = position_before.market_value() if position_before else ZERO
        resulting_value = market_value(new_position.quantity, fill_price)
        resulting_total = quantize_money(
            account.total_market_value - previous_value + resulting_value
        )
        cash_after = quantize_money(account.cash + cash_impact)
        return {
            "fill_price": fill_price,
            "resulting_quantity": new_position.quantity,
            "resulting_position_value": resulting_value,
            "resulting_total_market_value": resulting_total,
            "cash_after": cash_after,
            "projected_equity": equity(cash_after, resulting_total),
            "trade_value": value,
            "fees": fees,
            "required_cash": total_cost,
            "opening_new_position": position_before is None,
        }

    def account(
        self, db: Session, portfolio: Portfolio, stock_id: Optional[int] = None
    ) -> PortfolioAccount:
        """Runtime account state for the portfolio's risk rules.

        ``stock_id`` names the position an order-level rule is asking about. It is required
        whenever the caller is about to ask "is this stock held" — the portfolio-level view has
        totals and a position count but no single position in focus, so a rule that needs one
        reads the portfolio as flat rather than guessing.
        """
        from app.services.portfolio.service import build_account

        return build_account(db, portfolio, stock_id=stock_id)

    def peak_equity(self, db: Session, portfolio: Portfolio, current_equity: Decimal) -> Decimal:
        """Highest equity ever recorded: initial capital, current equity, every snapshot."""
        return peak_equity(
            [
                Decimal(portfolio.initial_capital),
                current_equity,
                self.snapshot_repo.max_equity(db, portfolio.id),
            ]
        )

    @staticmethod
    def _assessment(
        portfolio: Portfolio,
        valuation,
        peak: Decimal,
        account: PortfolioAccount,
        violations: list,
        limits: RiskLimits,
    ) -> RiskAssessment:
        return RiskAssessment(
            portfolio_id=portfolio.id,
            accepted=not violations,
            violations=tuple(violations),
            equity=valuation.equity,
            cash=valuation.cash,
            peak_equity=peak,
            current_drawdown_pct=drawdown_pct(peak, valuation.equity),
            exposure_pct=valuation.exposure_pct,
            position_count=account.position_count,
            limits=limits,
        )


def _violation_from_rejection(rejection: Rejection) -> RiskViolation:
    """Adapt an engine rejection onto the reporting shape of a risk violation."""
    details = dict(rejection.details or {})
    return RiskViolation(
        rule=str(details.get("rule", "risk_limit")),
        reason=rejection.reason,
        limit=_maybe_decimal(details.get("limit_pct") or details.get("limit")),
        observed=_maybe_decimal(
            details.get("projected_exposure_pct")
            or details.get("projected_allocation_pct")
            or details.get("trade_allocation_pct")
            or details.get("current_drawdown_pct")
            or details.get("daily_loss_pct")
            or details.get("resulting_quantity")
        ),
        details=details,
    )


def _maybe_decimal(value) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    return Decimal(str(value))


risk_check_service = RiskCheckService()
"""Order execution engine for Phase 9 — pure, deterministic, database-free.

Given a portfolio account, an order intent, a cost model and a set of risk limits, the
executor either produces a fully derived :class:`ExecutionPlan` (fill price, gross value,
fees, signed cash impact, realised P&L, resulting position) or a rejection. It performs no
I/O, so every rule below is unit-testable without FastAPI or a database.

Validation order (fixed, so a request that breaks several rules always reports the same one)
-------------------------------------------------------------------------------

1. portfolio must be ``ACTIVE``           -> ``PORTFOLIO_INACTIVE``
2. quantity must be a positive whole number -> ``INVALID_QUANTITY``
3. price must be positive                   -> ``INVALID_PRICE``
4. a sell must be covered by the position  -> ``INSUFFICIENT_POSITION``
5. a buy must be funded by available cash  -> ``INSUFFICIENT_CASH``
6. the portfolio's risk limits must hold   -> ``RISK_LIMIT_EXCEEDED``

A rejected plan carries the account's cash and position unchanged, which is what guarantees
Rule 7: a rejected order never mutates portfolio state.

Long only
---------
Phase 9 is long-only, matching Phase 8. There is no short selling: a sell reduces or closes
an existing position and quantity can never go negative. Market orders fill immediately at
the supplied simulation price, adjusted only by any configured slippage.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from app.schemas.portfolio import OrderSide, OrderStatus, PortfolioStatus, RejectionCode
from app.services.portfolio.calculator import (
    allocation_pct,
    apply_buy,
    apply_sell,
    apply_slippage,
    calculate_fee,
    equity,
    gross_value,
    market_value,
)
from app.services.portfolio.models import (
    ZERO,
    ExecutionPlan,
    OrderIntent,
    PortfolioAccount,
    Rejection,
    RiskLimits,
    TradingCostConfig,
    as_risk_limits,
    as_trading_cost_config,
)
from app.services.portfolio.risk import RiskManager, insufficient_cash_rejection
from app.services.backtest.models import quantize_money


class OrderExecutor:
    """Turns an order intent into an accepted fill or a rejection."""

    def __init__(self, costs: TradingCostConfig, limits: Optional[RiskLimits] = None):
        # Accepts either the engine dataclasses or the API request models, so a caller
        # never has to convert configuration by hand.
        self.costs = as_trading_cost_config(costs)
        self.limits = as_risk_limits(limits)
        self.costs.validate()
        self.limits.validate()
        self.risk = RiskManager(self.limits)

    def execute(
        self,
        account: PortfolioAccount,
        intent: OrderIntent,
        *,
        peak_equity: Optional[Decimal] = None,
        day_open_equity: Optional[Decimal] = None,
    ) -> ExecutionPlan:
        """Process one order against ``account``.

        ``peak_equity`` is the highest equity the portfolio has ever recorded; it is only
        consulted by the optional drawdown rule. ``day_open_equity`` is the equity at the start
        of the current UTC day; it is only consulted by the optional daily-loss rule. Both are
        read from stored state by the caller so that this executor stays a pure function of the
        numbers it is handed.
        """
        if account.status is not PortfolioStatus.ACTIVE:
            return ExecutionPlan.reject(
                intent,
                Rejection(
                    code=RejectionCode.PORTFOLIO_INACTIVE,
                    reason=(
                        f"Portfolio '{account.portfolio_id}' is {account.status.value} and "
                        "cannot accept new orders. Only ACTIVE portfolios execute orders."
                    ),
                    details={
                        "rule": "portfolio_status",
                        "portfolio_id": str(account.portfolio_id),
                        "status": account.status.value,
                    },
                ),
                account,
            )

        if intent.quantity <= 0:
            return ExecutionPlan.reject(
                intent,
                Rejection(
                    code=RejectionCode.INVALID_QUANTITY,
                    reason=(
                        f"Order quantity must be a positive whole number of shares, got "
                        f"{intent.quantity}."
                    ),
                    details={"rule": "positive_quantity", "quantity": str(intent.quantity)},
                ),
                account,
            )

        if intent.requested_price <= ZERO:
            return ExecutionPlan.reject(
                intent,
                Rejection(
                    code=RejectionCode.INVALID_PRICE,
                    reason=f"Order price must be greater than zero, got {intent.requested_price}.",
                    details={"rule": "positive_price", "price": str(intent.requested_price)},
                ),
                account,
            )

        is_buy = intent.side is OrderSide.BUY
        fill_price = apply_slippage(intent.requested_price, is_buy, self.costs)

        if is_buy:
            return self._execute_buy(
                account, intent, fill_price, peak_equity, day_open_equity
            )
        return self._execute_sell(account, intent, fill_price)

    # --- Sides ---

    def _execute_buy(
        self,
        account: PortfolioAccount,
        intent: OrderIntent,
        fill_price: Decimal,
        peak_equity: Optional[Decimal],
        day_open_equity: Optional[Decimal],
    ) -> ExecutionPlan:
        """Fund, risk-check and apply a buy."""
        value = gross_value(intent.quantity, fill_price)
        fees = calculate_fee(value, self.costs)
        total_cost = quantize_money(value + fees)

        if total_cost > account.cash:
            return ExecutionPlan.reject(
                intent, insufficient_cash_rejection(total_cost, account.cash), account
            )

        position_before = account.position_for(intent.stock_id)
        new_position, cash_impact = apply_buy(
            position_before, intent.quantity, fill_price, fees, intent.stock_id
        )

        # A buy converts cash into shares plus fees, so projected equity falls by the fees
        # only. The position being increased is replaced by its post-fill value: counting the
        # account's pre-trade market value *and* the new shares would value it twice, which
        # would make a flat portfolio look fully invested in the stock being bought.
        previous_value = position_before.market_value() if position_before else ZERO
        resulting_value = market_value(new_position.quantity, fill_price)
        resulting_total_value = quantize_money(
            account.total_market_value - previous_value + resulting_value
        )
        cash_after = quantize_money(account.cash + cash_impact)
        projected_equity = equity(cash_after, resulting_total_value)
        allocation = allocation_pct(resulting_value, projected_equity)

        rejection = self.risk.validate_buy(
            account,
            resulting_quantity=new_position.quantity,
            resulting_position_value=resulting_value,
            projected_equity=projected_equity,
            peak_equity=peak_equity,
            resulting_total_market_value=resulting_total_value,
            cash_after=cash_after,
            trade_value=value,
            open_position_count=account.position_count,
            opening_new_position=position_before is None,
            day_open_equity=day_open_equity,
        )
        if rejection is not None:
            return ExecutionPlan.reject(
                intent, rejection, account, risk_rule=str(rejection.details.get("rule"))
            )

        return ExecutionPlan(
            accepted=True,
            stock_id=intent.stock_id,
            side=intent.side,
            quantity=intent.quantity,
            order_status=OrderStatus.FILLED,
            execution_price=fill_price,
            gross_value=value,
            fees=fees,
            net_cash_impact=cash_impact,
            realized_pnl=ZERO,
            cash_before=account.cash,
            cash_after=equity(account.cash, cash_impact),
            position_before=position_before,
            position_after=new_position,
            resulting_allocation_pct=allocation,
        )

    def _execute_sell(
        self, account: PortfolioAccount, intent: OrderIntent, fill_price: Decimal
    ) -> ExecutionPlan:
        """Risk-check and apply a sell."""
        rejection = self.risk.validate_sell(
            account, intent, trade_value=gross_value(intent.quantity, fill_price)
        )
        if rejection is not None:
            return ExecutionPlan.reject(
                intent, rejection, account, risk_rule=str(rejection.details.get("rule"))
            )

        position_before = account.position_for(intent.stock_id)
        assert position_before is not None  # guaranteed by validate_sell

        value = gross_value(intent.quantity, fill_price)
        fees = calculate_fee(value, self.costs)
        new_position, realized, cash_impact = apply_sell(
            position_before, intent.quantity, fill_price, fees
        )

        return ExecutionPlan(
            accepted=True,
            stock_id=intent.stock_id,
            side=intent.side,
            quantity=intent.quantity,
            order_status=OrderStatus.FILLED,
            execution_price=fill_price,
            gross_value=value,
            fees=fees,
            net_cash_impact=cash_impact,
            realized_pnl=realized,
            cash_before=account.cash,
            cash_after=equity(account.cash, cash_impact),
            position_before=position_before,
            position_after=new_position,
        )
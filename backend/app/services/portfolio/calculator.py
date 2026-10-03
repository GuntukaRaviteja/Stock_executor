"""Pure financial calculations for the Phase 9 paper-trading engine.

Every function here is deterministic, side-effect free and independent of FastAPI and of
the database, so the money rules can be tested on their own.

Precision rules
---------------
* All inputs and outputs are :class:`decimal.Decimal`. No float ever enters a calculation.
* Quantisation is delegated to the Phase 8 helpers
  (:func:`app.services.backtest.models.quantize_money` / :func:`quantize_percent`) so Phase 8
  and Phase 9 round identically.
* Prices are quantised to 4 decimal places — the scale used by the ``portfolio_*`` price
  columns — *before* they are used downstream, so a persisted price and the P&L derived
  from it can never disagree.
* A monetary total is rounded once, at the end. Adding separately rounded parts is what
  makes ledgers drift by a cent, so parts are kept unrounded internally and only the final
  amount is quantised.

P&L definitions used throughout Phase 9
---------------------------------------
``realized_pnl``   net of the exit fees and of the entry fees allocated to the shares sold;
                   accumulates across trades and survives a position being closed.
``unrealized_pnl`` gross mark-to-market of open shares: ``quantity * (price - average entry)``.
``total_pnl``      ``equity - initial_capital``: the authoritative figure, net of every fee.

These satisfy the identity ``realized + unrealized - total_fees == total_pnl`` exactly, for
every portfolio state, because every fee is charged once to either the realised or the
unrealised side.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Optional, Sequence

from app.services.backtest.models import quantize_money, quantize_percent
from app.services.portfolio.models import (
    ZERO,
    CashMovement,
    PositionState,
    PositionValuation,
    TradingCostConfig,
)
from app.schemas.portfolio import TransactionType

#: Scale for prices (matches the ``Numeric(18, 4)`` price columns).
PRICE_QUANT = Decimal("0.0001")
#: Scale used for ratios such as allocation and drawdown percentages.
PERCENT_QUANT = Decimal("0.0001")
_ONE_HUNDRED = Decimal("100")


def quantize_price(value: Decimal) -> Decimal:
    """Round a price to the 4-decimal scale stored in the database."""
    return Decimal(value).quantize(PRICE_QUANT, rounding=ROUND_HALF_UP)


def calculate_fee(gross_value: Decimal, costs: TradingCostConfig) -> Decimal:
    """Simulated fee for one side of a trade.

    ``max(gross_value * commission_rate, min_fee)``, rounded once. With the default
    ``min_fee`` of 0 this is simply the percentage fee.
    """
    if gross_value <= ZERO:
        return quantize_money(ZERO)
    percentage_fee = Decimal(gross_value) * Decimal(costs.commission_rate)
    return quantize_money(max(percentage_fee, Decimal(costs.min_fee)))


def apply_slippage(price: Decimal, side_is_buy: bool, costs: TradingCostConfig) -> Decimal:
    """Fill price after slippage: a buy fills higher, a sell fills lower."""
    rate = Decimal(costs.slippage_rate)
    factor = Decimal("1") + rate if side_is_buy else Decimal("1") - rate
    return quantize_price(Decimal(price) * factor)


def gross_value(quantity: int, price: Decimal) -> Decimal:
    """``quantity * price``, rounded once."""
    return quantize_money(Decimal(quantity) * Decimal(price))


def weighted_average_entry_price(
    old_quantity: int, old_average: Decimal, added_quantity: int, added_price: Decimal
) -> Decimal:
    """Weighted average entry price after adding a buy to an existing position.

    ``(old_qty * old_avg + new_qty * new_price) / (old_qty + new_qty)``

    A flat position (``old_quantity == 0``) simply takes the new price, which also makes the
    weighted average exact rather than an unnecessary division.
    """
    total_quantity = old_quantity + added_quantity
    if total_quantity <= 0:
        raise ValueError("total quantity must be positive")
    if old_quantity <= 0:
        return quantize_price(Decimal(added_price))
    numerator = Decimal(old_quantity) * Decimal(old_average) + Decimal(added_quantity) * Decimal(
        added_price
    )
    return quantize_price(numerator / Decimal(total_quantity))


def cost_basis(quantity: int, average_entry_price: Decimal) -> Decimal:
    """Cost basis of a position: ``quantity * average_entry_price``."""
    return quantize_money(Decimal(quantity) * Decimal(average_entry_price))


def apply_buy(
    position: Optional[PositionState],
    quantity: int,
    price: Decimal,
    fees: Decimal,
    stock_id: int,
):
    """Return ``(new_position, cash_impact)`` for a filled buy of ``stock_id``.

    The average entry price becomes the quantity-weighted average of the buy prices; entry
    fees accumulate separately so the average stays a clean average of execution prices.
    Cash leaves the portfolio by ``gross_value + fees``.

    ``stock_id`` is required: a flat portfolio has no existing position to inherit the stock
    from, so the new row would otherwise be created against no stock at all.
    """
    old_quantity = position.quantity if position else 0
    old_average = position.average_entry_price if position else ZERO
    old_fees = position.entry_fees_total if position else ZERO
    old_realized = position.realized_pnl if position else ZERO

    new_quantity = old_quantity + quantity
    new_average = weighted_average_entry_price(old_quantity, old_average, quantity, price)
    new_position = PositionState(
        stock_id=position.stock_id if position else stock_id,
        quantity=new_quantity,
        average_entry_price=new_average,
        cost_basis=cost_basis(new_quantity, new_average),
        entry_fees_total=quantize_money(old_fees + fees),
        realized_pnl=old_realized,
        current_price=quantize_price(price),
    )
    return new_position, quantize_money(-(gross_value(quantity, price) + fees))


def allocated_entry_fees(position: PositionState, quantity: int) -> Decimal:
    """Entry fees attributable to ``quantity`` shares of ``position``.

    Allocated pro rata to quantity, which is consistent with the weighted average entry
    price: selling part of a position sells part of what it cost to build.
    """
    if position.quantity <= 0:
        return quantize_money(ZERO)
    if quantity >= position.quantity:
        return quantize_money(position.entry_fees_total)
    share = Decimal(position.entry_fees_total) * Decimal(quantity) / Decimal(position.quantity)
    return quantize_money(share)


def apply_sell(position: PositionState, quantity: int, price: Decimal, fees: Decimal):
    """Return ``(new_position_or_None, realized_pnl, cash_impact)`` for a filled sell.

    Realised P&L is booked only for the shares sold::

        quantity * (sell_price - average_entry_price) - exit_fees - allocated_entry_fees

    The average entry price of the remaining position is never reset: a partial exit leaves
    the remaining shares at the same average cost. Selling the whole quantity closes the
    position (``new_position`` is ``None``) while the realised P&L is retained.
    """
    if position.quantity <= 0:
        raise ValueError("cannot sell from a flat position")
    if quantity <= 0 or quantity > position.quantity:
        raise ValueError("sell quantity must be positive and not exceed the held quantity")

    gross_profit = Decimal(quantity) * (Decimal(price) - Decimal(position.average_entry_price))
    entry_fees = allocated_entry_fees(position, quantity)
    realized = quantize_money(gross_profit - Decimal(fees) - entry_fees)
    cash_impact = quantize_money(gross_value(quantity, price) - Decimal(fees))

    remaining_quantity = position.quantity - quantity
    if remaining_quantity == 0:
        return None, realized, cash_impact

    remaining = PositionState(
        stock_id=position.stock_id,
        quantity=remaining_quantity,
        average_entry_price=position.average_entry_price,
        cost_basis=cost_basis(remaining_quantity, position.average_entry_price),
        entry_fees_total=quantize_money(position.entry_fees_total - entry_fees),
        realized_pnl=quantize_money(position.realized_pnl + realized),
        current_price=quantize_price(price),
    )
    return remaining, realized, cash_impact


def market_value(quantity: int, price: Decimal) -> Decimal:
    """Market value of ``quantity`` shares at ``price``."""
    return quantize_money(Decimal(quantity) * Decimal(price))


def unrealized_pnl(quantity: int, average_entry_price: Decimal, current_price: Decimal) -> Decimal:
    """Gross mark-to-market P&L of an open position (fees excluded)."""
    return quantize_money(
        Decimal(quantity) * (Decimal(current_price) - Decimal(average_entry_price))
    )


def equity(cash: Decimal, market_value_total: Decimal) -> Decimal:
    """``cash + market_value``."""
    return quantize_money(Decimal(cash) + Decimal(market_value_total))


def total_pnl(current_equity: Decimal, invested_basis: Decimal) -> Decimal:
    """``equity - invested basis``.

    The basis is the opening capital plus whatever was later moved in or out (see
    :func:`invested_basis`), not the opening capital alone — otherwise a deposit would be
    reported as a profit.
    """
    return quantize_money(Decimal(current_equity) - Decimal(invested_basis))


def invested_basis(initial_capital: Decimal, external_flow: Decimal = ZERO) -> Decimal:
    """The capital a portfolio's return is measured against.

    ``initial_capital + net deposits and withdrawals``. Capital the owner put in is not
    performance, so it enlarges the denominator rather than the profit: with no external flow
    this is exactly the opening capital, which is what makes a portfolio that has only ever
    traded report the figures it always did.
    """
    return quantize_money(Decimal(initial_capital) + Decimal(external_flow))


def return_pct(total: Decimal, invested_basis: Decimal) -> Decimal:
    """``(total / basis) * 100``, safely returning 0 when the basis is zero."""
    if Decimal(invested_basis) <= ZERO:
        return quantize_percent(ZERO)
    return quantize_percent(Decimal(total) / Decimal(invested_basis) * _ONE_HUNDRED)


def allocation_pct(position_value: Decimal, equity_total: Decimal) -> Optional[Decimal]:
    """Position value as a percentage of portfolio equity; ``None`` when equity is not positive."""
    if Decimal(equity_total) <= ZERO:
        return None
    return quantize_percent(Decimal(position_value) / Decimal(equity_total) * _ONE_HUNDRED)


def exposure_pct(total_market_value: Decimal, equity_total: Decimal) -> Optional[Decimal]:
    """Portfolio exposure: every position's market value as a percentage of equity.

    ``None`` when equity is not positive, because the ratio is then undefined and reporting
    a number would be misleading.
    """
    return allocation_pct(total_market_value, equity_total)


def invested_capital(positions: Sequence[PositionValuation]) -> Decimal:
    """Capital currently at work: the sum of the open positions' cost bases.

    This is the accounting convention used throughout the portfolio service: ``invested
    capital`` is what the open positions *cost*, whereas ``market_value`` is what they are
    currently worth. Their difference is the unrealised P&L of those positions.
    """
    return quantize_money(sum((position.cost_basis for position in positions), ZERO))


def signed_drawdown_pct(peak_equity: Decimal, current_equity: Decimal) -> Decimal:
    """Drawdown from a peak as ``(current - peak) / peak * 100``.

    Signed, so it is ``0`` at a peak and negative below it — the convention used by the
    equity curve, the snapshots and the drawdown curve. Use :func:`drawdown_pct` when the
    positive magnitude is what is wanted (the drawdown risk rule and ``max_drawdown_pct``).
    """
    if Decimal(peak_equity) <= ZERO:
        return quantize_percent(ZERO)
    return quantize_percent((Decimal(current_equity) - Decimal(peak_equity)) / Decimal(peak_equity) * _ONE_HUNDRED)


def drawdown_pct(peak_equity: Decimal, current_equity: Decimal) -> Decimal:
    """Drawdown from a peak as a positive percentage; 0 when the peak is not positive."""
    if Decimal(peak_equity) <= ZERO:
        return quantize_percent(ZERO)
    decline = Decimal(peak_equity) - Decimal(current_equity)
    if decline <= ZERO:
        return quantize_percent(ZERO)
    return quantize_percent(decline / Decimal(peak_equity) * _ONE_HUNDRED)


def daily_pnl(day_open_equity: Decimal, current_equity: Decimal) -> Decimal:
    """Profit or loss booked on the current day: ``current_equity - day_open_equity``.

    Signed, so a loss is negative. ``day_open_equity`` is the equity at the start of the day —
    for a portfolio that has recorded snapshots, the last one from before today; otherwise the
    capital the portfolio started with, which makes the first day's P&L the change from the
    initial capital.

    Measuring against equity rather than against realised P&L is deliberate: a day in which the
    portfolio sells a winner and realises a profit but is now sitting on a loss has not had a
    good day, and only the equity figure says so.
    """
    return quantize_money(Decimal(current_equity) - Decimal(day_open_equity))


def daily_loss_pct(day_open_equity: Decimal, current_equity: Decimal) -> Decimal:
    """Loss on the current day as a **positive** percentage of the day's opening equity.

    ``0`` when the day is flat or profitable, and ``0`` when the day's opening equity is not
    positive — the ratio is then undefined, and a day that started at zero equity has no
    percentage loss to report.
    """
    if Decimal(day_open_equity) <= ZERO:
        return quantize_percent(ZERO)
    return drawdown_pct(day_open_equity, current_equity)


def _affordable_quantity(budget: Decimal, price: Decimal, costs: TradingCostConfig) -> int:
    """Largest whole-share quantity whose price **plus fees** fits inside ``budget``.

    Fees are included in the unit cost, which is what guarantees a sized order can always be
    funded and therefore that cash can never go negative. Returns ``0`` when even one share
    is unaffordable or the inputs are not usable; the result is never rounded *up* into a
    share the portfolio cannot buy.
    """
    price = quantize_price(price)
    if price <= ZERO or Decimal(budget) <= ZERO:
        return 0
    unit_cost = price * (Decimal("1") + Decimal(costs.commission_rate))
    if unit_cost <= ZERO:
        return 0
    return max(int((Decimal(budget) / unit_cost).to_integral_value(rounding=ROUND_DOWN)), 0)


def size_quantity_fixed_capital(
    capital: Decimal, price: Decimal, costs: TradingCostConfig
) -> int:
    """Fixed-capital sizing: ``floor(capital / price)`` whole shares.

    Fees are included in the unit cost, so the returned quantity is always fundable from
    ``capital``.
    """
    return _affordable_quantity(Decimal(capital), price, costs)


def size_quantity_by_percentage(
    equity: Decimal, allocation_pct_value: Decimal, price: Decimal, costs: TradingCostConfig
) -> int:
    """Percentage sizing: ``floor(equity * allocation_pct / price)`` whole shares.

    The basis is **equity**, not available cash, so the quantity answers "what fraction of the
    portfolio should this position be worth" rather than "how much of the cash pile may I
    spend". The cash-reserve rule is what turns a percentage of equity into an affordable
    order.
    """
    if Decimal(equity) <= ZERO or Decimal(allocation_pct_value) <= ZERO:
        return 0
    budget = Decimal(equity) * Decimal(allocation_pct_value) / _ONE_HUNDRED
    return _affordable_quantity(budget, price, costs)


def risk_per_share(entry_price: Decimal, stop_loss_price: Decimal) -> Decimal:
    """Risk carried by one share: ``abs(entry - stop)``.

    Raises ``ValueError`` when the two prices are equal, because the risk per share would be
    zero and every quantity would divide by it. The caller turns that into a domain error
    rather than inventing a size.
    """
    risk = abs(Decimal(entry_price) - Decimal(stop_loss_price))
    if risk <= ZERO:
        raise ValueError(
            "entry_price and stop_loss_price must differ: a stop at the entry price "
            "defines no risk per share"
        )
    return risk


def size_quantity_by_risk(
    equity: Decimal,
    risk_pct: Decimal,
    entry_price: Decimal,
    stop_loss_price: Decimal,
    costs: TradingCostConfig,
    max_capital: Optional[Decimal] = None,
) -> int:
    """Risk-based sizing: ``floor(equity * risk_pct / risk_per_share)`` whole shares.

    ``risk_per_share`` is ``abs(entry_price - stop_loss_price)`` and the allowed risk is
    ``equity * risk_pct``. The risk budget is *risk*, not cash: 1% of 100,000 risked over a
    5-per-share stop is 200 shares, which may well cost far more than 1,000. ``max_capital``
    optionally caps the result at what a given capital amount can actually fund (the service
    passes the portfolio's available cash); without it the pure formula is returned unchanged.

    Raises ``ValueError`` when the stop defines no risk per share.
    """
    unit_risk = risk_per_share(entry_price, stop_loss_price)
    allowed_risk = Decimal(equity) * Decimal(risk_pct) / _ONE_HUNDRED
    if allowed_risk <= ZERO:
        return 0
    quantity = int((allowed_risk / unit_risk).to_integral_value(rounding=ROUND_DOWN))
    if max_capital is not None:
        quantity = min(quantity, _affordable_quantity(Decimal(max_capital), entry_price, costs))
    return max(quantity, 0)


def size_quantity_from_cash(
    available_cash: Decimal,
    budget_pct: Decimal,
    price: Decimal,
    costs: TradingCostConfig,
) -> int:
    """Largest whole-share quantity affordable within ``budget_pct`` of ``available_cash``.

    Deriving the quantity from the budget — fees included in the unit cost — is what
    guarantees the engine can never spend cash it does not have, so cash can never go
    negative. Returns ``0`` when even one share is unaffordable.
    """
    price = quantize_price(price)
    if price <= ZERO or Decimal(available_cash) <= ZERO or Decimal(budget_pct) <= ZERO:
        return 0
    budget = Decimal(available_cash) * Decimal(budget_pct) / _ONE_HUNDRED
    fee_rate = Decimal(costs.commission_rate)
    unit_cost = price * (Decimal("1") + fee_rate)
    if unit_cost <= ZERO:
        return 0
    quantity = int((budget / unit_cost).to_integral_value(rounding=ROUND_DOWN))
    return max(quantity, 0)


def value_position(position: PositionState, price: Optional[Decimal]) -> PositionValuation:
    """Mark one position to ``price``, falling back to its last known price.

    A position that has never been marked is valued at its own average entry price, so an
    unpriced holding reports a market value of its cost and an unrealised P&L of zero
    instead of an invented price move.
    """
    if price is not None:
        marked_price = quantize_price(price)
    elif position.current_price is not None:
        marked_price = quantize_price(position.current_price)
    else:
        marked_price = quantize_price(position.average_entry_price)
    value = market_value(position.quantity, marked_price)
    return PositionValuation(
        stock_id=position.stock_id,
        quantity=position.quantity,
        average_entry_price=position.average_entry_price,
        current_price=marked_price,
        market_value=value,
        unrealized_pnl=unrealized_pnl(position.quantity, position.average_entry_price, marked_price),
        realized_pnl=position.realized_pnl,
        entry_fees_total=position.entry_fees_total,
        cost_basis=position.cost_basis,
    )


def sum_market_value(positions: Sequence[PositionValuation]) -> Decimal:
    """Total market value of a set of valued positions."""
    total = sum((position.market_value for position in positions), ZERO)
    return quantize_money(total)


def sum_unrealized_pnl(positions: Sequence[PositionValuation]) -> Decimal:
    """Total unrealised P&L of a set of valued positions."""
    total = sum((position.unrealized_pnl for position in positions), ZERO)
    return quantize_money(total)


def sum_open_entry_fees(positions: Sequence[PositionValuation]) -> Decimal:
    """Entry fees already paid on the shares that are still held.

    These fees have left cash but are not yet reflected in ``realized_pnl`` (they are
    released into it only when the shares are sold), which is what keeps the portfolio books
    reconcilable exactly::

        realized_pnl + unrealized_pnl - open_entry_fees == total_pnl
    """
    total = sum((position.entry_fees_total for position in positions), ZERO)
    return quantize_money(total)


def apply_deposit(cash: Decimal, amount: Decimal) -> CashMovement:
    """Add capital to a portfolio's cash balance.

    A deposit moves no stock and books no P&L: it changes cash, and therefore equity, by exactly
    the amount deposited. A non-positive amount is refused rather than rounded to zero, because
    a "deposit" of nothing is a caller error, not a no-op.
    """
    amount = Decimal(amount)
    if amount <= ZERO:
        raise ValueError("deposit amount must be greater than zero")
    cash_before = quantize_money(Decimal(cash))
    cash_after = quantize_money(cash_before + amount)
    return CashMovement(
        portfolio_id=0,
        movement_type=TransactionType.DEPOSIT.value,
        amount=quantize_money(amount),
        cash_before=cash_before,
        cash_after=cash_after,
        net_cash_impact=quantize_money(amount),
    )


def apply_withdrawal(cash: Decimal, amount: Decimal) -> CashMovement:
    """Take capital out of a portfolio's cash balance.

    A withdrawal is refused unless it is strictly positive **and** fully covered by the cash on
    hand. Refusing rather than allowing a negative balance is the whole point: a portfolio whose
    cash is negative would make equity, exposure and every weight measured against it
    meaningless.
    """
    amount = Decimal(amount)
    if amount <= ZERO:
        raise ValueError("withdrawal amount must be greater than zero")
    cash_before = quantize_money(Decimal(cash))
    if amount > cash_before:
        raise ValueError(
            f"withdrawal of {quantize_money(amount)} exceeds the available cash of {cash_before}"
        )
    cash_after = quantize_money(cash_before - amount)
    return CashMovement(
        portfolio_id=0,
        movement_type=TransactionType.WITHDRAWAL.value,
        amount=quantize_money(amount),
        cash_before=cash_before,
        cash_after=cash_after,
        net_cash_impact=quantize_money(-amount),
    )


def cash_after_deposit_or_withdrawal(cash: Decimal, net_cash_impact: Decimal) -> Decimal:
    """``cash + net_cash_impact``, rounded once. Shared by the ledger and the cash service."""
    return quantize_money(Decimal(cash) + Decimal(net_cash_impact))
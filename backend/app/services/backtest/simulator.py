"""Execution simulator and portfolio/equity engine for Phase 8.

Execution model (normative for every Phase 8 backtest)
------------------------------------------------------

* **Signal timing** — the strategy is evaluated from the *close of bar N*, using only
  information available on or before bar N.
* **Execution timing** — the resulting order is filled at the **open of bar N+1**. A
  signal therefore never fills on the close that produced it, which removes same-candle
  look-ahead.
* **Stop-loss / take-profit** — evaluated intrabar on every bar *after* the entry fill.
  A stop is filled at the stop price, or at the bar open when the bar gapped straight
  through it; a take-profit is filled at the target price, or at the open when the bar
  opened above it. Both use only that bar's own open/high/low.
* **Commission** — ``commission_pct`` of notional, charged on entry *and* exit.
* **Slippage** — ``slippage_pct`` applied adversely to the fill price: buys fill higher,
  sells fill lower. Slippage is also reported as a separate cost field so P&L reconciles.
* **Position sizing** — ``position_size_pct`` of currently available cash, floored to a
  whole number of shares. Cash can never go negative because the quantity is derived from
  the cash actually available.
* **Position book** — long only, one position at a time, no pyramiding, no shorting.

Per-bar order of operations
---------------------------

1. fill a pending order at this bar's open (if one exists);
2. check stop-loss and take-profit against this bar's high/low;
3. mark the position to this bar's close and append an equity point;
4. evaluate the strategy on this bar's close to create the pending order for bar N+1.

Because step 4 uses only bar N data and step 1 of the *next* iteration executes it, no
future information can influence a decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as date_type
from decimal import ROUND_DOWN, Decimal
from typing import List, Optional, Sequence

from app.core.logging import logger
from app.schemas.analytics import IndicatorContext
from app.services.backtest.models import (
    Bar,
    EquityPoint,
    ExitReason,
    Position,
    Trade,
    quantize_money,
    quantize_percent,
)
from app.services.backtest.strategy import Phase7TechnicalStrategy, StrategyAction

_ONE_HUNDRED = Decimal("100")
_ZERO = Decimal("0")


@dataclass
class ExecutionConfig:
    """Deterministic execution parameters for one backtest run."""

    initial_capital: Decimal
    position_size_pct: Decimal
    commission_pct: Decimal
    slippage_pct: Decimal
    stop_loss_pct: Optional[Decimal] = None
    take_profit_pct: Optional[Decimal] = None

    def validate(self) -> None:
        """Guard against impossible configurations reaching the simulator."""
        if self.initial_capital <= 0:
            raise ValueError("initial_capital must be greater than zero")
        if not _ZERO < self.position_size_pct <= _ONE_HUNDRED:
            raise ValueError("position_size_pct must be in (0, 100]")
        if self.commission_pct < 0:
            raise ValueError("commission_pct must not be negative")
        if self.slippage_pct < 0:
            raise ValueError("slippage_pct must not be negative")

    def stop_price_for(self, entry_reference_price: Decimal) -> Optional[Decimal]:
        """Stop-loss price for an entry, or ``None`` when no stop is configured."""
        if self.stop_loss_pct is None:
            return None
        return entry_reference_price * (Decimal("1") - self.stop_loss_pct / _ONE_HUNDRED)

    def target_price_for(self, entry_reference_price: Decimal) -> Optional[Decimal]:
        """Take-profit price for an entry, or ``None`` when no target is configured."""
        if self.take_profit_pct is None:
            return None
        return entry_reference_price * (Decimal("1") + self.take_profit_pct / _ONE_HUNDRED)


@dataclass
class SimulationResult:
    """Everything the simulator produced, before metrics are computed."""

    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[EquityPoint] = field(default_factory=list)
    final_equity: Decimal = _ZERO
    final_cash: Decimal = _ZERO
    skipped_entries: int = 0
    warnings: List[str] = field(default_factory=list)


class ExecutionSimulator:
    """Turns strategy decisions into orders, fills, positions and cash movements."""

    def __init__(self, config: ExecutionConfig, strategy: Phase7TechnicalStrategy):
        config.validate()
        self.config = config
        self.strategy = strategy

    # --- Public entry point ---

    def run(
        self,
        stock_id: int,
        bars: Sequence[Bar],
        contexts: Sequence[IndicatorContext],
        record_start: Optional[date_type] = None,
        record_end: Optional[date_type] = None,
    ) -> SimulationResult:
        """Simulate the strategy over ``bars``.

        Args:
            stock_id: internal stock id recorded on each trade.
            bars: chronologically ordered, already validated bars.
            contexts: point-in-time indicator contexts, aligned with ``bars``.
            record_start: first bar date to include in the equity curve and eligible for
                new entries. Bars before this date still warm the indicators up.
            record_end: last bar date to record. Bars after it are ignored entirely.

        Returns:
            A :class:`SimulationResult` with trades and the equity curve.
        """
        if len(bars) != len(contexts):
            raise ValueError("bars and contexts must be the same length")

        result = SimulationResult()
        cash = self.config.initial_capital
        position: Optional[Position] = None
        pending_action = StrategyAction.HOLD
        pending_score = 0
        bars = list(bars)
        contexts = list(contexts)

        for index, bar in enumerate(bars):
            recording = _is_recorded(bar.date, record_start, record_end)

            # --- 1. Execute the order decided on the previous bar, at this bar's open ---
            if pending_action is StrategyAction.ENTER and position is None and recording:
                position, cash, result = self._open_position(
                    stock_id, bar, cash, pending_score, result
                )
            elif pending_action is StrategyAction.EXIT and position is not None and recording:
                trade, cash = self._execute_sell(
                    position,
                    bar.date,
                    Decimal(str(bar.open)),
                    cash,
                    ExitReason.SIGNAL,
                    pending_score,
                )
                result.trades.append(trade)
                position = None

            # --- 2. Intrabar stop-loss / take-profit (uses only this bar) ---
            if position is not None:
                trade, cash = self._protective_exit(bar, position, cash)
                if trade is not None:
                    result.trades.append(trade)
                    position = None

            # --- 3. Mark to market on this bar's close ---
            if recording:
                close = Decimal(str(bar.close))
                position_value = position.market_value(close) if position else _ZERO
                result.equity_curve.append(
                    EquityPoint(
                        date=bar.date,
                        cash=quantize_money(cash),
                        position_value=position_value,
                        equity=quantize_money(cash + position_value),
                    )
                )

            # --- 4. Decide at this bar's close for the next bar's open ---
            if recording and index < len(bars) - 1:
                pending_action, pending_score, _ = self.strategy.decide(
                    is_long=position is not None, context=contexts[index]
                )

        # --- End of window: liquidate any open position for reporting ---
        if position is not None and result.equity_curve:
            last_bar = _last_recorded_bar(bars, record_start, record_end)
            if last_bar is not None:
                trade, cash = self._execute_sell(
                    position,
                    last_bar.date,
                    Decimal(str(last_bar.close)),
                    cash,
                    ExitReason.END_OF_PERIOD,
                    pending_score,
                )
                result.trades.append(trade)
                position = None
                # Restate the final equity point so it reflects the realised exit.
                final_point = result.equity_curve[-1]
                result.equity_curve[-1] = EquityPoint(
                    date=final_point.date,
                    cash=quantize_money(cash),
                    position_value=_ZERO,
                    equity=quantize_money(cash),
                )

        result.final_cash = quantize_money(cash)
        result.final_equity = (
            result.equity_curve[-1].equity if result.equity_curve else result.final_cash
        )
        if result.skipped_entries:
            result.warnings.append(
                f"{result.skipped_entries} entry signal(s) could not be executed because the "
                "available cash could not buy a whole share at the execution price"
            )
        logger.info(
            "Simulation complete: %d trade(s), final equity %s", len(result.trades), result.final_equity
        )
        return result

    # --- Order helpers ---

    def _open_position(
        self,
        stock_id: int,
        bar: Bar,
        cash: Decimal,
        signal_score: int,
        result: SimulationResult,
    ) -> "tuple[Optional[Position], Decimal, SimulationResult]":
        """Fill a buy order at this bar's open and return the new position and cash."""
        reference_price = Decimal(str(bar.open))
        fill_price = reference_price * (Decimal("1") + self.config.slippage_pct / _ONE_HUNDRED)

        budget = cash * self.config.position_size_pct / _ONE_HUNDRED
        quantity = _whole_shares(budget, fill_price, self.config.commission_pct)
        if quantity <= 0:
            result.skipped_entries += 1
            return None, cash, result

        notional = fill_price * Decimal(quantity)
        commission = notional * self.config.commission_pct / _ONE_HUNDRED
        # Rounded once, as a single amount, because this is the figure the cash ledger
        # uses. Quantising the parts separately and adding them would drift by a cent.
        cash_out = quantize_money(notional + commission)

        position = Position(
            stock_id=stock_id,
            quantity=quantity,
            entry_date=bar.date,
            entry_price=quantize_money(fill_price),
            entry_reference_price=reference_price,
            entry_value=quantize_money(notional),
            entry_commission=quantize_money(commission),
            # Slippage is a positive cost: a buy always fills above the reference price.
            entry_slippage=quantize_money((fill_price - reference_price) * Decimal(quantity)),
            entry_cash_out=cash_out,
            entry_signal_score=signal_score,
        )
        return position, quantize_money(cash - cash_out), result

    def _execute_sell(
        self,
        position: Position,
        bar_date: date_type,
        reference_price: Decimal,
        cash: Decimal,
        reason: ExitReason,
        signal_score: int,
    ) -> "tuple[Trade, Decimal]":
        """Apply slippage and commission to a sell and return the trade plus updated cash.

        Cost decomposition
        ------------------
        ``net_pnl`` is defined as the actual change in cash caused by the round trip
        (``proceeds - entry_cash_out``) so that the ledger and the reported P&L can never
        disagree. ``commission`` and ``slippage`` are the real costs charged on both sides,
        and ``gross_pnl`` is the price move implied once those costs are removed. That
        makes both of the following identities exact by construction rather than
        approximately true:

            net_pnl == gross_pnl - commission - slippage
            final_equity == initial_capital + sum(trade.net_pnl)   [when flat at the end]
        """
        fill_price = reference_price * (Decimal("1") - self.config.slippage_pct / _ONE_HUNDRED)
        quantity = Decimal(position.quantity)

        exit_notional = fill_price * quantity
        exit_commission = exit_notional * self.config.commission_pct / _ONE_HUNDRED
        proceeds = quantize_money(exit_notional - exit_commission)

        entry_notional = position.entry_reference_price * quantity
        net_pnl = quantize_money(proceeds - position.entry_cash_out)

        total_commission = quantize_money(position.entry_commission + exit_commission)
        # Slippage is always reported as a positive cost: on the entry the buy fills above
        # the reference price, on the exit the sell fills below it.
        exit_slippage = reference_price - fill_price
        total_slippage = quantize_money(position.entry_slippage + exit_slippage * quantity)
        gross_pnl = quantize_money(net_pnl + total_commission + total_slippage)

        trade = Trade(
            stock_id=position.stock_id,
            entry_date=position.entry_date,
            entry_price=position.entry_price,
            entry_reference_price=position.entry_reference_price,
            exit_date=bar_date,
            exit_price=quantize_money(fill_price),
            exit_reference_price=quantize_money(reference_price),
            quantity=position.quantity,
            gross_pnl=gross_pnl,
            commission=total_commission,
            slippage=total_slippage,
            net_pnl=net_pnl,
            return_pct=quantize_percent(
                (net_pnl / entry_notional) * _ONE_HUNDRED if entry_notional > 0 else _ZERO
            ),
            holding_period=(bar_date - position.entry_date).days,
            exit_reason=reason,
            entry_signal_score=position.entry_signal_score,
            exit_signal_score=signal_score if reason is ExitReason.SIGNAL else None,
        )
        return trade, quantize_money(cash + proceeds)

    def _protective_exit(
        self, bar: Bar, position: Position, cash: Decimal
    ) -> "tuple[Optional[Trade], Decimal]":
        """Evaluate stop-loss then take-profit against this bar only.

        Stop-loss is checked first so a bar that spans both levels is treated as the
        loss (the conservative choice). A gap straight through a level fills at the bar
        open rather than at the level, because the level was never available.
        """
        open_price = Decimal(str(bar.open))
        low = Decimal(str(bar.low))
        high = Decimal(str(bar.high))

        stop_price = self.config.stop_price_for(position.entry_reference_price)
        if stop_price is not None and low <= stop_price:
            reference = open_price if open_price <= stop_price else stop_price
            trade, cash = self._execute_sell(
                position, bar.date, reference, cash, ExitReason.STOP_LOSS, 0
            )
            return trade, cash

        target_price = self.config.target_price_for(position.entry_reference_price)
        if target_price is not None and high >= target_price:
            reference = open_price if open_price >= target_price else target_price
            trade, cash = self._execute_sell(
                position, bar.date, reference, cash, ExitReason.TAKE_PROFIT, 0
            )
            return trade, cash

        return None, cash


def _whole_shares(budget: Decimal, fill_price: Decimal, commission_pct: Decimal) -> int:
    """Largest whole-share quantity affordable with ``budget`` including commission.

    Deriving the quantity from the available budget is what guarantees Rule 6: the
    simulator can never spend cash it does not have, so cash never goes negative.
    """
    if fill_price <= 0:
        return 0
    unit_cost = fill_price * (Decimal("1") + commission_pct / _ONE_HUNDRED)
    return int((budget / unit_cost).to_integral_value(rounding=ROUND_DOWN))


def _is_recorded(
    bar_date: date_type, record_start: Optional[date_type], record_end: Optional[date_type]
) -> bool:
    """Whether a bar falls inside the recorded backtest window."""
    if record_start is not None and bar_date < record_start:
        return False
    if record_end is not None and bar_date > record_end:
        return False
    return True


def _last_recorded_bar(
    bars: Sequence[Bar], record_start: Optional[date_type], record_end: Optional[date_type]
) -> Optional[Bar]:
    """Final bar inside the recorded window, or ``None`` when the window is empty."""
    for bar in reversed(list(bars)):
        if _is_recorded(bar.date, record_start, record_end):
            return bar
    return None
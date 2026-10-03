"""Buy-and-hold benchmark for Phase 8.

The benchmark answers one question: *did the strategy do better than simply owning the
stock for the whole window?* It deliberately uses the **same** initial capital and the
**same** transaction-cost model as the strategy, so the comparison isolates timing
decisions rather than cost assumptions.

Convention
----------
* Buy at the **open of the first recorded bar**.
* Hold with no further decisions.
* Sell at the **close of the last recorded bar**.
* Commission and slippage are charged exactly as the strategy charges them.

A window with fewer than two bars cannot be both entered and exited, so the benchmark is
``None`` in that case rather than a made-up number.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from typing import Optional, Sequence

from app.services.backtest.models import Bar, BenchmarkResult, quantize_money, quantize_percent
from app.services.backtest.simulator import ExecutionConfig

_ONE_HUNDRED = Decimal("100")


def run_buy_and_hold(
    bars: Sequence[Bar], config: ExecutionConfig
) -> Optional[BenchmarkResult]:
    """Simulate buying on the first bar and holding to the last.

    Args:
        bars: the *recorded* bar window (already restricted to the backtest period).
        config: the same execution configuration the strategy used.

    Returns:
        A :class:`BenchmarkResult`, or ``None`` when the window is too short.
    """
    if len(bars) < 2:
        return None

    first_open = Decimal(str(bars[0].open))
    last_close = Decimal(str(bars[-1].close))
    capital = config.initial_capital

    # Buy at the first open with the same adverse slippage the strategy would face.
    entry_price = first_open * (Decimal("1") + config.slippage_pct / _ONE_HUNDRED)
    unit_cost = entry_price * (Decimal("1") + config.commission_pct / _ONE_HUNDRED)
    quantity = int((capital / unit_cost).to_integral_value(rounding=ROUND_DOWN))
    if quantity <= 0:
        # Not enough capital for a single share: report the (zero) outcome honestly.
        return BenchmarkResult(
            initial_capital=quantize_money(capital),
            final_equity=quantize_money(capital),
            benchmark_return_pct=quantize_percent(Decimal("0")),
            benchmark_final_equity=quantize_money(capital),
        )

    cash = capital - entry_price * Decimal(quantity) - (
        entry_price * Decimal(quantity) * config.commission_pct / _ONE_HUNDRED
    )

    exit_price = last_close * (Decimal("1") - config.slippage_pct / _ONE_HUNDRED)
    proceeds = exit_price * Decimal(quantity)
    commission = proceeds * config.commission_pct / _ONE_HUNDRED
    final_equity = quantize_money(cash + proceeds - commission)

    return BenchmarkResult(
        initial_capital=quantize_money(capital),
        final_equity=final_equity,
        benchmark_return_pct=quantize_percent(
            (final_equity / capital - Decimal("1")) * _ONE_HUNDRED
        ),
        benchmark_final_equity=final_equity,
    )
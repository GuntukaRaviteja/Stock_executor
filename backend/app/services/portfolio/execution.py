"""Execution-price policy: which price of which bar a simulated fill is taken from.

Normative convention (inherited from the Phase 8 simulator, not redefined here)
---------------------------------------------------------------------------
Phase 8 executes a decision taken from the **close of bar N** at the **open of bar N+1**. A
signal therefore never fills on the close that produced it, which removes same-candle
look-ahead, and the portfolio paper engine reuses exactly that rule by default.

The two policies
----------------
``OPEN`` (default)
    ``fill = execution_bar.open`` where ``execution_bar`` is the bar **after** the signal bar.
    The decision sees nothing from the execution bar, not even its open. This is the Phase 8
    convention.

``CLOSE``
    ``fill = signal_bar.close`` — executed on the bar that produced the decision. The close
    of bar N is fully known at the moment the decision is taken, so this is **not**
    look-ahead either; it simply fills a bar earlier and therefore never pays the overnight
    gap between the two closes.

Both policies only ever read a price that was knowable at the decision timestamp. The guard
:func:`assert_no_lookahead` makes that explicit and is what the look-ahead regression test
asserts: for ``OPEN`` it requires ``execution_bar.date > signal_bar.date``, and it rejects a
``CLOSE`` fill taken from any bar later than the signal bar.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from app.core.logging import logger
from app.core.exceptions import ExecutionPriceUnavailableError
from app.schemas.portfolio import ExecutionPricePolicy
from app.services.backtest.models import Bar, quantize_money


@dataclass(frozen=True)
class ExecutionReference:
    """The bar a fill was taken from and the price it produced."""

    policy: ExecutionPricePolicy
    signal_bar: Bar
    execution_bar: Bar
    price: Decimal

    @property
    def signal_date(self):
        return self.signal_bar.date

    @property
    def execution_date(self):
        return self.execution_bar.date


def reference_price(bar: Bar, policy: ExecutionPricePolicy) -> Decimal:
    """The price this policy takes from ``bar``."""
    if bar is None:
        raise ExecutionPriceUnavailableError(
            "No bar available to take an execution price from.",
            {"policy": policy.value},
        )
    if policy is ExecutionPricePolicy.OPEN:
        candidate = bar.open
        label = "open"
    else:
        candidate = bar.close
        label = "close"
    if candidate is None or Decimal(candidate) <= 0:
        raise ExecutionPriceUnavailableError(
            f"Bar {bar.date} has no usable {label} price, so no execution price could be "
            "resolved from it.",
            {"policy": policy.value, "bar_date": bar.date.isoformat(), "field": label},
        )
    # A fill price is money, so it carries the same 2-decimal precision as every other monetary
    # value in the system; otherwise cash arithmetic accumulates sub-paise noise.
    return quantize_money(Decimal(str(candidate)))


def resolve_execution_price(
    signal_bar: Bar,
    execution_bar: Optional[Bar],
    policy: ExecutionPricePolicy,
) -> ExecutionReference:
    """Resolve the fill reference for a decision taken from ``signal_bar``.

    ``execution_bar`` is the bar the order is filled on. It is ignored by the ``CLOSE``
    policy, which always fills on the signal bar itself.
    """
    if policy is ExecutionPricePolicy.CLOSE:
        bar = signal_bar
    else:
        if execution_bar is None:
            raise ExecutionPriceUnavailableError(
                f"The {policy.value} execution policy fills on the bar after the signal "
                f"bar, but {signal_bar.date} is the last bar in the window.",
                {"policy": policy.value, "signal_date": signal_bar.date.isoformat()},
            )
        assert_no_lookahead(signal_bar, execution_bar, policy)
        bar = execution_bar

    price = reference_price(bar, policy)
    logger.debug(
        "Execution price resolved from %s bar %s: %s (policy=%s)",
        policy.value.lower(),
        bar.date.isoformat(),
        price,
        policy.value,
    )
    return ExecutionReference(
        policy=policy, signal_bar=signal_bar, execution_bar=bar, price=price
    )


def assert_no_lookahead(
    signal_bar: Bar, execution_bar: Bar, policy: ExecutionPricePolicy
) -> None:
    """Fail loudly if a resolution would have used an unknowable price.

    For ``OPEN`` the execution bar must be strictly later than the signal bar. For ``CLOSE``
    the engine resolves the price on the signal bar itself, so any attempt to pass a later
    bar is a bug in the caller rather than a policy choice.
    """
    if policy is ExecutionPricePolicy.OPEN:
        if execution_bar.date <= signal_bar.date:
            raise ExecutionPriceUnavailableError(
                f"Execution bar {execution_bar.date} is not after the signal bar "
                f"{signal_bar.date}; an OPEN fill may only use a later bar.",
                {
                    "policy": policy.value,
                    "signal_date": signal_bar.date.isoformat(),
                    "execution_date": execution_bar.date.isoformat(),
                },
            )
        return
    if execution_bar.date > signal_bar.date:
        raise ExecutionPriceUnavailableError(
            f"The CLOSE policy fills on the signal bar {signal_bar.date}, so a later bar "
            f"({execution_bar.date}) must not be used for it.",
            {
                "policy": policy.value,
                "signal_date": signal_bar.date.isoformat(),
                "execution_date": execution_bar.date.isoformat(),
            },
        )


def default_policy() -> ExecutionPricePolicy:
    """The application default: the Phase 8 convention."""
    from app.core.config import settings

    return ExecutionPricePolicy(str(settings.PAPER_TRADING_EXECUTION_PRICE_POLICY).upper())
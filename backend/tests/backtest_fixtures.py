"""Deterministic OHLCV helpers shared by the Phase 8 backtest tests.

Nothing here contacts a provider or the network. Every series is generated from a fixed
formula so a test failure always points at the engine, never at the fixture.

Two invariants hold for every bar produced here, because the engine relies on both:

* **Continuity** — a bar's open is the previous bar's close, so appending a move to a
  series produces a realistic gap-free tape instead of a discontinuity.
* **Valid OHLC** — ``low <= min(open, close)`` and ``high >= max(open, close)`` always
  hold, so the data-quality validator never rejects a fixture by accident.

Tests that need to study a genuine overnight gap build bars explicitly with
:func:`bar` and set ``open`` themselves.
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import List, Optional, Sequence

from app.services.backtest.models import Bar

#: Fixed anchor date for every generated series. Real calendar dates keep weekday gaps
#: realistic while staying fully deterministic.
ANCHOR = date(2024, 1, 1)

#: Half-width of the high/low envelope around each bar's body.
DEFAULT_SPREAD = Decimal("1")

#: Default synthetic volume, held constant so it never perturbs a test.
DEFAULT_VOLUME = 1_000_000


def bar(
    day: date,
    close: Decimal,
    *,
    spread: Decimal = DEFAULT_SPREAD,
    open_price: Optional[Decimal] = None,
    volume: int = DEFAULT_VOLUME,
) -> Bar:
    """Build one valid bar, defaulting ``open`` to ``close`` (no overnight gap)."""
    spread = Decimal(str(spread))
    open_price = Decimal(str(close)) if open_price is None else Decimal(str(open_price))
    body_low = min(open_price, Decimal(str(close)))
    body_high = max(open_price, Decimal(str(close)))
    return Bar(
        date=day,
        open=open_price,
        high=body_high + spread,
        low=body_low - spread,
        close=Decimal(str(close)),
        volume=volume,
    )


def bars_from_closes(
    closes: Sequence[Decimal],
    *,
    start: date = ANCHOR,
    step_days: int = 1,
    spread: Decimal = DEFAULT_SPREAD,
    volume: int = DEFAULT_VOLUME,
) -> List[Bar]:
    """Turn closes into a continuous bar series starting at ``start``.

    Each bar opens where the previous one closed, which is what makes the next-bar-open
    execution model observable: an entry fills at the previous close, not at the close
    that produced the signal.
    """
    closes = [Decimal(str(value)) for value in closes]
    bars: List[Bar] = []
    previous_close: Optional[Decimal] = None
    for index, close in enumerate(closes):
        day = start + timedelta(days=index * step_days)
        bars.append(
            bar(
                day,
                close,
                spread=spread,
                open_price=previous_close,
                volume=volume,
            )
        )
        previous_close = close
    return bars


# --- Close-series shapes --------------------------------------------------------------


def _step(base: Decimal, step: Decimal, count: int) -> List[Decimal]:
    return [base + step * index for index in range(count)]


def rising(count: int, *, start: str = "100", step: str = "1") -> List[Decimal]:
    """A linear uptrend."""
    return _step(Decimal(start), Decimal(step), count)


def falling(count: int, *, start: str = "200", step: str = "1") -> List[Decimal]:
    """A linear downtrend."""
    return _step(Decimal(start), Decimal(step) * -1, count)


def flat(count: int, *, price: str = "100") -> List[Decimal]:
    """A constant series: zero volatility and zero drawdown by construction."""
    return [Decimal(price)] * count


def append_rally(closes: Sequence[Decimal], count: int, *, step: str = "3") -> List[Decimal]:
    """Continue ``closes`` with ``count`` rising bars, resuming from the last close."""
    return list(closes) + _step(closes[-1], Decimal(step), count)


def append_decline(closes: Sequence[Decimal], count: int, *, step: str = "3") -> List[Decimal]:
    """Continue ``closes`` with ``count`` falling bars, resuming from the last close."""
    return list(closes) + _step(closes[-1], Decimal(step) * -1, count)


def decline_then_rally(down: int, up: int) -> List[Decimal]:
    """A sustained decline followed by a rally: the shape that produces a bullish crossover."""
    return append_rally(falling(down), up)


def rally_then_decline(up: int, down: int) -> List[Decimal]:
    """A rally followed by a decline: the shape that produces a bearish crossover."""
    return append_decline(rising(up), down)

"""Phase 8 strategy definition and Phase 7 signal adapter.

The strategy is a thin, transparent rule layer on top of the **existing** Phase 7
``SignalEngine``. No indicator is recalculated here: the engine receives a
:class:`~app.schemas.analytics.IndicatorContext` and produces
:class:`~app.schemas.signals.SignalItem` objects plus a composite score, and this module
only decides what those signals mean for a position.

Strategy: ``phase7_technical`` (version 1.0)
---------------------------------------------

ENTRY (only when flat, and only from a signal produced on bar *N*):

1. **Trend confirmation** — at least one bullish ``TREND`` signal, i.e. the close is above
   its moving average or the short average is above the long average.
2. **Momentum confirmation** — at least one bullish ``CROSSOVER`` or ``MOMENTUM`` signal,
   i.e. a moving-average/MACD crossover or positive period momentum.
3. **Composite strength** — the Phase 7 composite score is at least
   ``entry_score_threshold``.

EXIT (only when long, evaluated on bar *N*):

1. the composite score falls to or below ``exit_score_threshold``, **or**
2. a bearish ``CROSSOVER`` signal appears (bearish trend reversal), **or**
3. the configured stop-loss level is breached on a later bar, **or**
4. the configured take-profit level is reached on a later bar.

Because all three gates are independent ``and`` conditions, a crossover in isolation is
never enough to open a position: the trend, momentum and composite-score gates must all
agree. That is deliberate — it prevents a single noisy indicator from producing trades.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional

from app.core.logging import logger
from app.schemas.analytics import IndicatorContext
from app.schemas.signals import (
    CompositeSignalResponse,
    SignalDirection,
    SignalItem,
    SignalType,
)
from app.services.signal_service import SignalEngine

#: Stable identifier recorded with every backtest for reproducibility.
STRATEGY_NAME = "phase7_technical"
#: Version of the rule set. Bump whenever the entry/exit rules below change.
STRATEGY_VERSION = "1.0"

#: Signal types that count as "trend confirmation" for an entry.
ENTRY_TREND_SIGNAL_TYPES = frozenset({SignalType.TREND})
#: Signal types that count as "momentum confirmation" for an entry.
ENTRY_MOMENTUM_SIGNAL_TYPES = frozenset({SignalType.CROSSOVER, SignalType.MOMENTUM})
#: Signal types that count as a bearish reversal for an exit.
EXIT_REVERSAL_SIGNAL_TYPES = frozenset({SignalType.CROSSOVER})


class StrategyAction(str, Enum):
    """What the strategy wants to happen at the next executable bar."""

    HOLD = "hold"
    ENTER = "enter"
    EXIT = "exit"


@dataclass
class StrategyParams:
    """Deterministic parameters of :class:`Phase7TechnicalStrategy`.

    These are persisted verbatim with every backtest so a run can be reproduced.
    """

    entry_score_threshold: int = 20
    exit_score_threshold: int = 0
    min_entry_strength: int = 0
    require_trend_confirmation: bool = True
    require_momentum_confirmation: bool = True

    def to_dict(self) -> Dict[str, object]:
        """Serialisable representation used for persistence and reproducibility."""
        return {
            "entry_score_threshold": self.entry_score_threshold,
            "exit_score_threshold": self.exit_score_threshold,
            "min_entry_strength": self.min_entry_strength,
            "require_trend_confirmation": self.require_trend_confirmation,
            "require_momentum_confirmation": self.require_momentum_confirmation,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, object]) -> "StrategyParams":
        """Rebuild parameters from a persisted dict, ignoring unknown keys."""
        defaults = cls()
        return cls(
            entry_score_threshold=int(
                payload.get("entry_score_threshold", defaults.entry_score_threshold)
            ),
            exit_score_threshold=int(
                payload.get("exit_score_threshold", defaults.exit_score_threshold)
            ),
            min_entry_strength=int(payload.get("min_entry_strength", defaults.min_entry_strength)),
            require_trend_confirmation=bool(
                payload.get("require_trend_confirmation", defaults.require_trend_confirmation)
            ),
            require_momentum_confirmation=bool(
                payload.get(
                    "require_momentum_confirmation", defaults.require_momentum_confirmation
                )
            ),
        )


class Phase7TechnicalStrategy:
    """Rule-based long-only strategy driven entirely by Phase 7 signals.

    The strategy is stateless and pure: for a given indicator context it always returns
    the same action. All execution timing is handled by the simulator, which is what keeps
    the strategy free of look-ahead bias.
    """

    strategy_name = STRATEGY_NAME
    strategy_version = STRATEGY_VERSION

    def __init__(
        self,
        engine: Optional[SignalEngine] = None,
        params: Optional[StrategyParams] = None,
    ):
        self.engine = engine or SignalEngine()
        self.params = params or StrategyParams()

    # --- Signal evaluation (delegates every calculation to Phase 7) ---

    def evaluate(self, stock_id: int, symbol: str, context: IndicatorContext) -> CompositeSignalResponse:
        """Run the Phase 7 signal engine over a point-in-time indicator context.

        ``context`` must only contain data available on or before its ``as_of`` date.
        """
        return self.engine.evaluate(stock_id, symbol, context=context)

    def composite_score(self, context: IndicatorContext) -> int:
        """Phase 7 composite alignment score (-100..+100) for a context."""
        return self.engine.calculate_composite_score(self.engine.build_signals(context))

    # --- Rules ---

    def entry_signal(self, signals: List[SignalItem], score: int) -> bool:
        """Whether the entry conditions are satisfied on this bar."""
        if score < self.params.entry_score_threshold:
            return False

        if self.params.min_entry_strength > 0 and not any(
            signal.strength >= self.params.min_entry_strength for signal in signals
        ):
            return False

        if self.params.require_trend_confirmation and not _has_signal_of_types(
            signals, SignalDirection.BULLISH, ENTRY_TREND_SIGNAL_TYPES
        ):
            return False

        if self.params.require_momentum_confirmation and not _has_signal_of_types(
            signals, SignalDirection.BULLISH, ENTRY_MOMENTUM_SIGNAL_TYPES
        ):
            return False

        return True

    def exit_signal(self, signals: List[SignalItem], score: int) -> bool:
        """Whether the exit conditions are satisfied on this bar."""
        if score <= self.params.exit_score_threshold:
            return True
        return _has_signal_of_types(
            signals, SignalDirection.BEARISH, EXIT_REVERSAL_SIGNAL_TYPES
        )

    def decide(
        self,
        is_long: bool,
        context: IndicatorContext,
    ) -> "tuple[StrategyAction, int, List[SignalItem]]":
        """Decide what to do at the close of a bar.

        Returns the action, the composite score and the underlying signals so the caller
        can log exactly which conditions fired. The action applies at the **next** bar's
        open; nothing in this method reads a future price.
        """
        signals = self.engine.build_signals(context)
        score = self.engine.calculate_composite_score(signals)

        if is_long:
            action = StrategyAction.EXIT if self.exit_signal(signals, score) else StrategyAction.HOLD
        else:
            action = (
                StrategyAction.ENTER if self.entry_signal(signals, score) else StrategyAction.HOLD
            )
        return action, score, signals


def _has_signal_of_types(
    signals: List[SignalItem], direction: SignalDirection, types: frozenset
) -> bool:
    """True when any signal has the requested direction and one of the requested types."""
    return any(signal.direction is direction and signal.signal_type in types for signal in signals)


@dataclass
class StrategyDecision:
    """A recorded strategy decision, kept for diagnostics."""

    date: object
    action: StrategyAction
    composite_score: int
    signal_indicators: List[str] = field(default_factory=list)


class StrategyRegistry:
    """Maps the public ``strategy`` request field onto a concrete strategy instance.

    Keeping the mapping in one place means adding a strategy later is a single registry
    entry plus an implementation of the entry/exit interface, with no changes to the API
    layer or the simulator.
    """

    def __init__(self, engine: Optional[SignalEngine] = None):
        self._strategies = {
            STRATEGY_NAME: Phase7TechnicalStrategy(engine=engine),
        }

    def get(self, name: str) -> Optional[Phase7TechnicalStrategy]:
        """Return the strategy registered under ``name``, or ``None`` when unknown."""
        strategy = self._strategies.get(name)
        if strategy is None:
            logger.warning("Backtest requested unknown strategy '%s'", name)
        return strategy

    @property
    def available(self) -> List[str]:
        """Names of every registered strategy."""
        return sorted(self._strategies)


def parse_optional_percent(value: Optional[Decimal]) -> Optional[Decimal]:
    """Normalise an optional percentage parameter (``None`` stays ``None``)."""
    if value is None:
        return None
    return Decimal(str(value)) / Decimal("100")


strategy_registry = StrategyRegistry()
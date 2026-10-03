"""Phase 16 audit trail — an append-only record of everything an execution did.

Why an event log, when the orders and fills are already tables
--------------------------------------------------------------
The order and fill tables answer "what happened". They cannot answer "what was refused, and why",
because a refusal books no fill and moves no cash — a rejected order leaves a row, but a plan
that never got as far as creating an order, a risk gate that said no, and a reconciliation that
found a discrepancy all leave nothing at all in those tables. The event log is where those
answers live.

Immutability
------------
:class:`EventRecorder` can only append. There is no update, no delete, and no method that
rewrites a recorded event, because the value of an audit trail is exactly the property that a
recorded event cannot be changed afterwards — a mutable one records only what is currently
believed, which is the same thing a comment would record.

The order of events is the value of the log
--------------------------------------------
A run records the stages in the order they happened — plan created, validated, risk approved,
order submitted, partially filled, filled, position updated, cash updated, reconciliation — so
reading the log top to bottom for one correlation id replays the story of the run. Every event
carries that correlation id, which is why the log can be read as a narrative rather than as a
pile of rows.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence

from app.services.execution.models import (
    ExecutionEntityType,
    ExecutionEventType,
)


@dataclass(frozen=True)
class ExecutionEvent:
    """One immutable audit entry.

    ``details`` holds the figures behind the event. They are serialised with ``Decimal`` as a
    string, so a value read back months later is the value that was recorded rather than a
    float's approximation of it.
    """

    event_type: ExecutionEventType
    entity_type: ExecutionEntityType
    entity_id: str
    portfolio_id: int
    timestamp: datetime
    correlation_id: Optional[str] = None
    execution_plan_id: Optional[str] = None
    details: Dict[str, Any] = None


class EventRecorder:
    """Collects a run's events in the order they happened.

    Deliberately append-only and deliberately local to one run: a recorder is created per
    execution, so events from concurrent runs can never interleave in one object's history, and a
    recorder that outlived its run would make the ordering above meaningless.
    """

    def __init__(self, portfolio_id: int, correlation_id: Optional[str] = None):
        self.portfolio_id = portfolio_id
        self.correlation_id = correlation_id
        self._events: List[ExecutionEvent] = []

    def record(
        self,
        event_type: ExecutionEventType,
        entity_type: ExecutionEntityType,
        entity_id: str,
        timestamp: datetime,
        *,
        execution_plan_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> ExecutionEvent:
        """Append one event and return it."""
        event = ExecutionEvent(
            event_type=event_type,
            entity_type=entity_type,
            entity_id=str(entity_id),
            portfolio_id=self.portfolio_id,
            timestamp=timestamp,
            correlation_id=self.correlation_id,
            execution_plan_id=execution_plan_id,
            details=details or {},
        )
        self._events.append(event)
        return event

    @property
    def events(self) -> Sequence[ExecutionEvent]:
        """Everything recorded so far, in order."""
        return tuple(self._events)

    def types(self) -> Sequence[str]:
        """The event names in order — what an assertion on a lifecycle reads."""
        return tuple(event.event_type.value for event in self._events)

    def of_type(self, event_type: ExecutionEventType) -> Sequence[ExecutionEvent]:
        """Every recorded event of one type, in order."""
        return tuple(event for event in self._events if event.event_type is event_type)

    def clear(self) -> None:
        """Drop the collected events.

        Only called when the run they describe was rolled back, so nothing half-written is ever
        offered to be persisted. A run that committed never clears.
        """
        self._events.clear()

    def __len__(self) -> int:
        return len(self._events)


def serialize_details(details: Optional[Dict[str, Any]]) -> Optional[str]:
    """Serialise an event's details for storage, or ``None`` when there are none.

    Sorted keys, so two runs recording the same event produce byte-identical rows and a test can
    compare them.
    """
    if not details:
        return None
    return json.dumps(details, sort_keys=True, default=_json_default)


def _json_default(value: Any) -> str:
    """Render a value JSON has no type for, without losing information.

    Decimals become strings (exact), enums and dates become their text form, and anything else
    falls back to ``str``. Falling back rather than raising is deliberate: an audit entry that
    cannot be written because one detail was an unusual type would lose the event, and losing the
    event is the one outcome this log exists to prevent.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
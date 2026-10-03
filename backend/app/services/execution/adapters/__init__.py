"""Execution adapters — the broker-agnostic seam of Phase 16.

Two names are exported, and only two:

:class:`~app.services.execution.adapters.base.ExecutionAdapter`
    the contract every venue implements

:class:`~app.services.execution.adapters.simulated.SimulatedExecutionAdapter`
    the deterministic stand-in that ships with this phase

**Phase 16 connects to no broker.** There is no vendor SDK, no session, no credential store and
no network call anywhere under this package. A future real adapter is a new module implementing
the same six methods; portfolio planning, validation, risk gating, position accounting and
reconciliation above this seam do not change.
"""

from app.services.execution.adapters.base import (
    AdapterAccountState,
    ExecutionAdapter,
    OrderState,
    OrderSubmission,
)
from app.services.execution.adapters.simulated import SimulatedExecutionAdapter

__all__ = [
    "AdapterAccountState",
    "ExecutionAdapter",
    "OrderState",
    "OrderSubmission",
    "SimulatedExecutionAdapter",
]
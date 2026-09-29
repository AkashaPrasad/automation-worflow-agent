"""Engine exceptions.

Two families:

* **API-facing errors** (raised by the public Orchestrator methods). They subclass the builtin types the
  API layer already maps to HTTP codes, so the API never imports engine internals:
  ``LookupError`` → 404, ``ValueError`` → 400, ``ConflictError`` (core contract) → 409.
* **Control-flow signals** (``RunInterrupted`` and friends) used inside a run's task to unwind cleanly when
  the kill switch, a cancel, or a budget limit fires. They are not errors: the driver catches them and puts
  the run into the right state.
"""
from __future__ import annotations

from ..core.interfaces import ConflictError
from ..core.models import ErrorKind


class RunNotFound(LookupError):
    """Unknown run id."""


class ApprovalNotFound(LookupError):
    """Unknown approval id, or an approval that belongs to another run."""


class InvalidRunState(ConflictError):
    """The action conflicts with the run's current status (API → 409)."""


# ---------------------------------------------------------------------------
# Control flow inside a run task
# ---------------------------------------------------------------------------


class RunInterrupted(Exception):
    """Stop dispatching new work; in-flight calls are allowed to finish."""


class Paused(RunInterrupted):
    """Kill switch engaged (run.status == paused)."""


class Cancelled(RunInterrupted):
    """The user cancelled the run."""


class BudgetExceeded(RunInterrupted):
    def __init__(self, limit: str, value: float, maximum: float) -> None:
        super().__init__(f"budget exceeded: {limit} ({value:g} of {maximum:g})")
        self.limit = limit
        self.value = value
        self.maximum = maximum


class RunAborted(RunInterrupted):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# Internal failures
# ---------------------------------------------------------------------------


class EngineFailure(Exception):
    """A failure that ends the run as FAILED with a user-readable reason."""


class PlannerUnavailable(EngineFailure):
    """Muse could not be reached or returned unusable output. ``transient`` outages (overload, rate limit,
    timeouts) pause the run so it can be resumed; anything else fails it."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


class PlanInvalid(EngineFailure):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors[:6]))
        self.errors = errors


class BindingError(Exception):
    """Arguments could not be bound (template path missing, required value empty...).

    Detected by code, so its cause is certain: recovery can act on ``kind`` without asking Jev."""

    def __init__(self, kind: ErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message

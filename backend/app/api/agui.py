"""Map Adjutant RunEvents to AG-UI protocol events."""
from __future__ import annotations

from typing import Any

from ag_ui.core import (
    CustomEvent,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StateSnapshotEvent,
    StepFinishedEvent,
    StepStartedEvent,
)
from ag_ui.encoder import EventEncoder

from app.core.models import TERMINAL_STATUSES, Run, RunEvent, RunStatus

_encoder = EventEncoder()


def _step_name(ev: RunEvent) -> str:
    return ev.node_id or ev.type


def map_event(ev: RunEvent) -> Any:
    ts = ev.ts
    if ev.type == "node.started":
        return StepStartedEvent(step_name=_step_name(ev), timestamp=ts)
    if ev.type == "node.result":
        return StepFinishedEvent(step_name=_step_name(ev), timestamp=ts)
    if ev.type in ("plan.created", "plan.revised"):
        return StateSnapshotEvent(snapshot={"plan": ev.data.get("plan")}, timestamp=ts)
    return CustomEvent(name=ev.type, value=ev.data, timestamp=ts)


def run_started(run_id: str) -> Any:
    return RunStartedEvent(thread_id=run_id, run_id=run_id)


def terminal_event(run_id: str, status: RunStatus, run: Run | None) -> Any:
    if status in (RunStatus.COMPLETED, RunStatus.ROLLED_BACK):
        return RunFinishedEvent(thread_id=run_id, run_id=run_id,
                                result={"status": status.value, "summary": run.summary if run else ""})
    msg = (run.error if run and run.error else "") or f"run {status.value}"
    return RunErrorEvent(message=msg, code=status.value)


def is_terminal(ev: RunEvent) -> RunStatus | None:
    if ev.type != "run.status":
        return None
    try:
        st = RunStatus(ev.data.get("status"))
    except ValueError:
        return None
    return st if st in TERMINAL_STATUSES else None


def encode(event: Any) -> dict[str, str]:
    """sse-starlette message dict; `data` is the AG-UI event JSON (same as EventEncoder's payload)."""
    payload = _encoder.encode(event)  # "data: {...}\n\n"
    data = payload[len("data: "):].strip() if payload.startswith("data: ") else payload.strip()
    return {"event": str(event.type.value), "data": data}

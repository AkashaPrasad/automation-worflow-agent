"""Ordered event publication for one run, and agent-lane attribution.

Trace callbacks fire synchronously inside the Muse/Jev clients, while ``EventBus.publish`` is async. Every event
of a run -- engine events and traces alike -- goes through one FIFO queue drained by a single task, so the
timeline order is the order in which things happened, whichever side emitted them. The drainer exits when the
queue is empty and is restarted by the next emit, so no task outlives the work.
"""
from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar
from typing import Any, Literal

from ..core.interfaces import EventBus
from ..core.models import RunEvent
from .prompts import PLANNER_PURPOSES
from .util import jsonable

log = logging.getLogger("adjutant.engine.events")

Agent = Literal["planner", "executor", "evaluator", "guardian", "system"]

#: the plan step a coroutine is working for; lets traces from llm.* tools and Jev land on the right node
current_node: ContextVar[str | None] = ContextVar("adjutant_current_node", default=None)

_EVALUATOR_HINTS = ("verif", "rank", "memor", "clarif")


def llm_agent(purpose: str) -> Agent:
    return "planner" if purpose in PLANNER_PURPOSES else "executor"


def judgment_agent(purpose: str) -> Agent:
    """Jev purposes: gate / scan_untrusted / classify_failure / infer_tool_effect are the guardian's;
    verify / rank_memories / needs_clarification are the evaluator's (SPEC §4)."""
    p = purpose.lower()
    return "evaluator" if any(h in p for h in _EVALUATOR_HINTS) else "guardian"


class EventEmitter:
    def __init__(self, bus: EventBus, run_id: str) -> None:
        self._bus = bus
        self._run_id = run_id
        self._queue: asyncio.Queue[RunEvent] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._drainer: asyncio.Task[None] | None = None

    def emit(self, type_: str, *, agent: Agent = "system", node_id: str | None = None,
             data: dict[str, Any] | None = None) -> None:
        event = RunEvent(run_id=self._run_id, type=type_, agent=agent, node_id=node_id,  # type: ignore[arg-type]
                         data=jsonable(data or {}))
        loop = asyncio.get_running_loop()
        if self._queue is None or self._loop is not loop:
            self._loop, self._queue, self._drainer = loop, asyncio.Queue(), None
        self._queue.put_nowait(event)
        if self._drainer is None or self._drainer.done():
            self._drainer = loop.create_task(self._drain(), name=f"adjutant-events:{self._run_id}")

    async def _drain(self) -> None:
        queue = self._queue
        assert queue is not None
        while not queue.empty():
            event = queue.get_nowait()
            try:
                await self._bus.publish(event)
            except Exception:  # an unpublishable event must not stall the run
                log.exception("failed to publish %s for run %s", event.type, self._run_id)
            finally:
                queue.task_done()

    async def flush(self) -> None:
        if self._queue is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._loop is loop:
            await self._queue.join()

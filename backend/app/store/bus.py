"""In-process EventBus: persist-then-fan-out with gapless replay + live streaming."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from app.core.interfaces import RunStore
from app.core.models import RunEvent

log = logging.getLogger("adjutant.bus")


class _Sub:
    __slots__ = ("queue", "dropped")

    def __init__(self, maxsize: int) -> None:
        self.queue: asyncio.Queue[RunEvent] = asyncio.Queue(maxsize=maxsize)
        self.dropped = False


class LocalEventBus:
    def __init__(self, store: RunStore, queue_size: int = 1000) -> None:
        self.store = store
        self.queue_size = queue_size
        self._subs: dict[str, set[_Sub]] = {}

    async def publish(self, event: RunEvent) -> RunEvent:
        # No awaits between persist and fan-out: fan-out order always equals seq order.
        event = self.store.append_event(event)
        for sub in list(self._subs.get(event.run_id, ())):
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                # slow consumer: drop it; it ends after draining and can reconnect with Last-Event-ID
                sub.dropped = True
                self._subs[event.run_id].discard(sub)
                log.warning("dropping slow subscriber for run %s", event.run_id)
        return event

    async def subscribe(self, run_id: str, after_seq: int = 0) -> AsyncIterator[RunEvent]:
        sub = _Sub(self.queue_size)
        self._subs.setdefault(run_id, set()).add(sub)  # live queue first, then replay
        try:
            last = after_seq
            for ev in self.store.events(run_id, after_seq):
                last = max(last, ev.seq)
                yield ev
            while True:
                if sub.dropped and sub.queue.empty():
                    return
                try:
                    ev = await asyncio.wait_for(sub.queue.get(), timeout=1.0 if sub.dropped else None)
                except asyncio.TimeoutError:
                    return
                if ev.seq <= last:
                    continue
                last = ev.seq
                yield ev
        finally:
            subs = self._subs.get(run_id)
            if subs is not None:
                subs.discard(sub)
                if not subs:
                    self._subs.pop(run_id, None)

"""Per-run trace sink, carried in a ContextVar so the LLM and Judge clients can report
calls without knowing about runs. The engine sets the sink inside each run's task:

    token = trace_sink.set(lambda kind, data: ...)   # kind: "llm.call" | "judgment.call"
"""
from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

TraceSink = Callable[[str, dict[str, Any]], None]
trace_sink: ContextVar[TraceSink | None] = ContextVar("adjutant_trace_sink", default=None)


def emit_trace(kind: str, data: dict[str, Any]) -> None:
    sink = trace_sink.get()
    if sink is not None:
        try:
            sink(kind, data)
        except Exception:  # tracing must never break execution
            pass

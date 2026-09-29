"""DAG scheduler: runs ready steps concurrently with bounded parallelism.

The candidate set is recomputed every round, so steps added by a re-plan mid-phase are picked up. Before every
dispatch it passes the kill-switch checkpoint and the budget check; once either fires it stops dispatching,
lets in-flight steps finish (their results are kept), and then re-raises the signal to the driver.

A phase ends when nothing is running and nothing can start. Steps still waiting at that point are waiting on
something outside the phase (a human approval, a step that needs the shadow run first); the pipeline decides
what comes next.
"""
from __future__ import annotations

import asyncio
import logging

from ..core.models import ErrorKind, NodeStatus, PlanNode
from .binding import Mode, has_output, is_write
from .context import RunContext, action_nodes
from .errors import RunInterrupted
from .executor import NodeExecutor
from .plan import dead_dependency, effective_deps, ref_closure
from .recovery import skip

log = logging.getLogger("adjutant.engine.scheduler")


class DagScheduler:
    def __init__(self, ctx: RunContext, executor: NodeExecutor, mode: Mode, *, max_parallel: int) -> None:
        self.ctx = ctx
        self.executor = executor
        self.mode = mode
        self.max_parallel = max(1, max_parallel)
        self.processed: set[str] = set()
        self.running: dict[str, asyncio.Task[None]] = {}

    # -- which steps this phase handles ------------------------------------------
    def _candidates(self) -> list[PlanNode]:
        ctx, out = self.ctx, []
        for node in action_nodes(ctx.plan):
            if node.id in self.processed or node.id in self.running:
                continue
            write = is_write(ctx.spec(node.tool))
            if self.mode is Mode.SHADOW:
                if node.status is NodeStatus.PENDING:
                    out.append(node)
            elif write and node.status in (NodeStatus.READY, NodeStatus.SIMULATED):
                out.append(node)
            elif not write and node.status is NodeStatus.SUCCEEDED and self._consumes_writes(node):
                out.append(node)  # may have read a simulated value; the executor re-checks cheaply
        return out

    def _consumes_writes(self, node: PlanNode) -> bool:
        return any(is_write(self.ctx.spec(self.ctx.plan.nodes[n].tool)) for n in ref_closure(self.ctx.plan, node.id)
                   if n in self.ctx.plan.nodes)

    def _dep_ready(self, dep_id: str, pending: set[str]) -> bool:
        dep = self.ctx.plan.nodes.get(dep_id)
        if dep is None:
            return True  # removed by a re-plan; references were re-pointed
        if dep_id in pending or dep_id in self.running:
            return False
        if dep.status is NodeStatus.SKIPPED:
            return True  # dead_dependency() already vetted skipped deps
        return has_output(dep, self.mode)

    # -- main loop -------------------------------------------------------------------
    async def run(self) -> None:
        stop: RunInterrupted | None = None
        while True:
            candidates = self._candidates()
            progressed = self._skip_doomed(candidates)
            if progressed:
                candidates = self._candidates()
            if stop is None:
                try:
                    self._dispatch(candidates)
                except RunInterrupted as signal:
                    stop = signal
            if not self.running:
                if progressed and stop is None:
                    continue
                break
            done, _ = await asyncio.wait(self.running.values(), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                node_id = next(k for k, v in self.running.items() if v is task)
                del self.running[node_id]
                exc = task.exception()
                if exc is None:
                    continue
                if isinstance(exc, RunInterrupted):
                    stop = stop or exc
                elif isinstance(exc, Exception):
                    self._internal_failure(node_id, exc)
                else:
                    raise exc  # BaseException (process-level): propagate as-is
        if stop is not None:
            raise stop

    def _skip_doomed(self, candidates: list[PlanNode]) -> bool:
        progressed = False
        for node in candidates:
            dead = dead_dependency(self.ctx.plan, node)
            if dead is not None:
                skip(self.ctx, node, f"Skipped: depends on {dead.dep_id} ({dead.status.value})", dead.kind)
                self.processed.add(node.id)
                progressed = True
        return progressed

    def _dispatch(self, candidates: list[PlanNode]) -> None:
        pending = {n.id for n in candidates}
        for node in candidates:
            if len(self.running) >= self.max_parallel:
                return
            if not all(self._dep_ready(d, pending) for d in effective_deps(self.ctx.plan, node.id)):
                continue
            self.ctx.checkpoint()
            self.ctx.check_budget()
            self.processed.add(node.id)
            self.running[node.id] = asyncio.get_running_loop().create_task(
                self.executor.run_node(self.ctx, node.id, self.mode), name=f"{self.ctx.run.id}:{node.id}")

    def _internal_failure(self, node_id: str, exc: Exception) -> None:
        log.exception("step %s crashed inside the engine", node_id, exc_info=exc)
        node = self.ctx.plan.nodes.get(node_id)
        if node is not None and node.status not in (NodeStatus.SUCCEEDED, NodeStatus.SKIPPED):
            self.executor.fail(self.ctx, node, ErrorKind.UNKNOWN, f"internal engine error: {type(exc).__name__}: {exc}")

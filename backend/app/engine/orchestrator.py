"""AdjutantOrchestrator: the public face of the engine (implements ``core.interfaces.Orchestrator``).

API calls return quickly; the work of a run happens in one background asyncio task at a time. A run that
waits for a human (approval, clarification) or is paused holds no task and no concurrency slot; answering it
spawns the next phase task. Concurrency across runs is capped by ``settings.max_concurrent_runs``.

Control semantics:
  * pause    -- kill switch. Flag + status persisted immediately; the scheduler checks it before every dispatch
                and every tool call; in-flight calls finish, nothing new starts.
  * resume   -- restores the status the run was paused in and re-enters that phase (phases are idempotent).
  * cancel   -- stops at the next checkpoint; unfinished steps become cancelled; effects stay (use rollback).
  * rollback -- compensates applied, compensable effects newest-first; communications are reported as not
                reversible.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any

from ..core.interfaces import LLM, EventBus, Judge, RunStore, ToolRegistry, WorkspaceStore
from ..core.models import (
    TERMINAL_STATUSES,
    Autonomy,
    ErrorKind,
    NodeKind,
    NodeStatus,
    Run,
    RunStatus,
    ToolError,
    ToolResult,
    Verdict,
    now_ms,
)
from ..core.tracing import trace_sink
from .binding import is_write
from .completion import Finisher
from .context import EngineConfig, RunContext, Services, action_nodes
from .errors import (
    ApprovalNotFound,
    BudgetExceeded,
    Cancelled,
    EngineFailure,
    InvalidRunState,
    Paused,
    PlanInvalid,
    PlannerUnavailable,
    RunAborted,
    RunNotFound,
)
from .executor import NodeExecutor
from .gate import Gatekeeper
from .ledger import EffectLedger, RollbackReport
from .pipeline import PHASE_FOR_STATUS, Phase, RunPipeline
from .planner import Planner
from .recovery import RecoveryManager
from .util import truncate
from .verify import Verifier

log = logging.getLogger("adjutant.engine")

WAITING_STATUSES = frozenset({RunStatus.AWAITING_APPROVAL, RunStatus.CLARIFYING, RunStatus.PAUSED})
ROLLBACK_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.PAUSED,
                               RunStatus.AWAITING_APPROVAL})


class AdjutantOrchestrator:
    def __init__(self, store: RunStore, bus: EventBus, llm: LLM, judge: Judge, registry: ToolRegistry,
                 workspaces: WorkspaceStore, *, config: EngineConfig | None = None) -> None:
        self.svc = Services(store=store, bus=bus, llm=llm, judge=judge, registry=registry, workspaces=workspaces,
                            config=config or EngineConfig())
        self.planner = Planner(self.svc)
        self.ledger = EffectLedger()
        self.gatekeeper = Gatekeeper(self.svc)
        self.recovery = RecoveryManager(self.svc, self.planner)
        self.executor = NodeExecutor(self.svc, self.ledger, self.gatekeeper, self.recovery)
        self.verifier = Verifier(self.svc, self.planner)
        self.finisher = Finisher(self.svc, self.planner)
        self.pipeline = RunPipeline(self.svc, self.planner, self.executor, self.gatekeeper, self.verifier,
                                    self.finisher)
        self._contexts: dict[str, RunContext] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._slots: asyncio.Semaphore | None = None

    # ======================================================================
    # Orchestrator protocol
    # ======================================================================
    async def start_run(self, workspace_id: str, request: str, autonomy: Autonomy | str = Autonomy.BALANCED) -> Run:
        request = (request or "").strip()
        if not request:
            raise ValueError("request must not be empty")
        run = Run(workspace_id=workspace_id, request=request, autonomy=Autonomy(autonomy),
                  title=truncate(" ".join(request.split()), 80))
        if self.svc.config.budget is not None:
            run.budget = self.svc.config.budget.model_copy()
        created = self.svc.store.create_run(run)
        if isinstance(created, Run):
            run = created
        ctx = RunContext(run, self.svc)
        self._contexts[run.id] = ctx
        ctx.emit("run.created", run={"id": run.id, "request": run.request, "autonomy": run.autonomy.value,
                                     "budget": run.budget.model_dump(), "title": run.title})
        await ctx.flush()
        self._spawn(ctx, Phase.INTAKE)
        return ctx.snapshot()

    async def resolve_approval(self, run_id: str, approval_id: str, decisions: dict[str, str],
                               edits: dict[str, dict[str, Any]], note: str = "") -> Run:
        ctx = self._ctx(run_id)
        approval = self.svc.store.get_approval(approval_id)
        if approval is None or approval.run_id != run_id:
            raise ApprovalNotFound(f"approval {approval_id} not found for run {run_id}")
        if approval.status != "pending":
            raise InvalidRunState("approval already resolved")
        if ctx.run.status is not RunStatus.AWAITING_APPROVAL:
            raise InvalidRunState(f"run is not awaiting approval (status: {ctx.run.status.value})")
        known = {i.node_id for i in approval.items}
        unknown = (set(decisions or {}) | set(edits or {})) - known
        if unknown:
            raise ValueError(f"unknown node ids in decisions/edits: {sorted(unknown)}")
        # No "busy" check: the task that requested this approval may still be flushing its last events; the
        # next phase task is chained behind it (see _spawn), and that task no longer touches the plan.
        self.gatekeeper.apply_decisions(ctx, approval, dict(decisions or {}), dict(edits or {}))
        approval.note = note or approval.note
        self.svc.store.save_approval(approval)
        ctx.emit("approval.resolved", approval=approval)
        needs_shadow = any(n.status is NodeStatus.PENDING for n in action_nodes(ctx.plan))
        ctx.set_status(RunStatus.SHADOWING if needs_shadow else RunStatus.EXECUTING)
        await ctx.flush()
        self._spawn(ctx, Phase.SHADOW if needs_shadow else Phase.COMMIT)
        return ctx.snapshot()

    async def answer_clarification(self, run_id: str, answer: str) -> Run:
        ctx = self._ctx(run_id)
        if ctx.run.status is not RunStatus.CLARIFYING:
            raise InvalidRunState(f"run is not waiting for clarification (status: {ctx.run.status.value})")
        answer = (answer or "").strip()
        if not answer:
            raise ValueError("answer must not be empty")
        ctx.run.clarification = {**(ctx.run.clarification or {}), "answer": answer}
        ctx.emit("clarification.answered", answer=answer)
        ctx.set_status(RunStatus.PLANNING)
        await ctx.flush()
        self._spawn(ctx, Phase.INTAKE)
        return ctx.snapshot()

    async def pause(self, run_id: str) -> Run:
        ctx = self._ctx(run_id)
        status = ctx.run.status
        if status in TERMINAL_STATUSES:
            raise InvalidRunState(f"run is already {status.value}")
        if status is RunStatus.PAUSED:
            raise InvalidRunState("run is already paused")
        ctx.resume_status = status
        ctx.pause_requested = True
        ctx.pause_observed = False
        ctx.set_status(RunStatus.PAUSED)
        ctx.log("Kill switch engaged: in-flight calls finish, nothing new starts", level="warning")
        await ctx.flush()
        return ctx.snapshot()

    async def resume(self, run_id: str) -> Run:
        ctx = self._ctx(run_id)
        if ctx.run.status is not RunStatus.PAUSED:
            raise InvalidRunState(f"run is not paused (status: {ctx.run.status.value})")
        target = ctx.resume_status or RunStatus.PLANNING
        ctx.pause_requested = False
        ctx.resume_status = None
        ctx.run.error = ""
        ctx.set_status(target)
        await ctx.flush()
        task = self._tasks.get(run_id)
        still_running = task is not None and not task.done() and not ctx.pause_observed
        if not still_running and target in PHASE_FOR_STATUS:
            self._spawn(ctx, PHASE_FOR_STATUS[target])
        return ctx.snapshot()

    async def cancel(self, run_id: str) -> Run:
        ctx = self._ctx(run_id)
        if ctx.run.status in TERMINAL_STATUSES:
            raise InvalidRunState(f"run is already {ctx.run.status.value}")
        ctx.cancel_requested = True
        task = self._tasks.get(run_id)
        if task is None or task.done():
            self._finalize_cancel(ctx)
        else:
            # Visible to the API at once; the task publishes the terminal run.status after its in-flight
            # calls finish, so that event stays the last one of the run.
            ctx.cancelled_from = ctx.cancelled_from or ctx.run.status
            ctx.run.status = RunStatus.CANCELLED
            ctx.persist()
        await ctx.flush()
        return ctx.snapshot()

    async def rollback(self, run_id: str) -> Run:
        ctx = self._ctx(run_id)
        status = ctx.run.status
        if status not in ROLLBACK_STATUSES:
            raise InvalidRunState(f"cannot roll back a run that is {status.value}; pause or cancel it first")
        if not any(e.status == "applied" for e in self.svc.store.effects(run_id)):
            raise InvalidRunState("nothing to roll back: no applied effects")
        task = self._tasks.get(run_id)
        if task is not None and not task.done():  # e.g. paused with calls still finishing
            ctx.cancel_requested = True
            with suppress(BaseException):
                await asyncio.wait({task})
        token = trace_sink.set(ctx.on_trace)
        try:
            report = await self.ledger.compensate_all(ctx)
        finally:
            trace_sink.reset(token)
        self._after_rollback(ctx, report)
        await ctx.flush()
        return ctx.snapshot()

    async def recover_unfinished(self) -> None:
        for run in self.svc.store.unfinished_runs():
            task = self._tasks.get(run.id)
            if task is not None and not task.done():
                continue
            self._contexts.pop(run.id, None)
            try:
                ctx = self._ctx(run.id)
                await self._recover(ctx)
                await ctx.flush()
            except Exception:
                log.exception("could not recover run %s", run.id)

    # ======================================================================
    # Extras (not in the protocol; used by tests and graceful shutdown)
    # ======================================================================
    async def wait_idle(self, run_id: str, timeout: float | None = 30.0) -> Run:
        """Wait until the run has no background task (it is waiting for a human, paused or terminal)."""
        async def _wait() -> None:
            while (task := self._tasks.get(run_id)) is not None and not task.done():
                await asyncio.wait({task})
        await asyncio.wait_for(_wait(), timeout)
        task = self._tasks.get(run_id)
        if task is not None and task.done() and not task.cancelled() and task.exception() is not None:
            raise task.exception()  # type: ignore[misc]
        return self._ctx(run_id).snapshot()

    async def shutdown(self) -> None:
        """Engage the kill switch on every active run and let in-flight calls finish."""
        for run_id, task in list(self._tasks.items()):
            if not task.done():
                ctx = self._contexts.get(run_id)
                if ctx is not None:
                    ctx.pause_requested = True
        with suppress(BaseException):
            await asyncio.gather(*(t for t in self._tasks.values() if not t.done()), return_exceptions=True)

    # ======================================================================
    # Internals
    # ======================================================================
    def _ctx(self, run_id: str) -> RunContext:
        ctx = self._contexts.get(run_id)
        if ctx is not None:
            return ctx
        run = self.svc.store.get_run(run_id)
        if run is None:
            raise RunNotFound(f"run {run_id} not found")
        ctx = RunContext(run, self.svc)
        ctx.hydrate()
        self._contexts[run_id] = ctx
        return ctx

    def _slot(self) -> asyncio.Semaphore:
        if self._slots is None:
            self._slots = asyncio.Semaphore(max(1, self.svc.config.max_concurrent_runs))
        return self._slots

    def _spawn(self, ctx: RunContext, phase: Phase) -> None:
        previous = self._tasks.get(ctx.run.id)

        async def runner() -> None:
            if previous is not None and not previous.done():
                with suppress(BaseException):
                    await asyncio.wait({previous})
            if ctx.run.status in TERMINAL_STATUSES or ctx.run.status in WAITING_STATUSES:
                return  # the previous task already moved the run somewhere this phase must not touch
            await self._drive(ctx, phase)

        task = asyncio.get_running_loop().create_task(runner(), name=f"adjutant-run:{ctx.run.id}:{phase.value}")
        task.add_done_callback(_retrieve_exception)
        self._tasks[ctx.run.id] = task

    async def _drive(self, ctx: RunContext, phase: Phase) -> None:
        token = trace_sink.set(ctx.on_trace)
        try:
            async with self._slot():
                try:
                    await self.pipeline.drive(ctx, phase)
                except Paused:
                    ctx.persist()
                except Cancelled:
                    self._finalize_cancel(ctx)
                except BudgetExceeded as e:
                    await self.finisher.stop_for_budget(ctx, e)
                except RunAborted as e:
                    await self.finisher.fail(ctx, f"Aborted: {e.reason}", use_llm=False)
                except PlannerUnavailable as e:
                    if e.transient:
                        self._pause_for_outage(ctx, e)
                    else:
                        await self.finisher.fail(ctx, str(e), use_llm=False)
                except PlanInvalid as e:
                    await self.finisher.fail(ctx, f"The planner could not produce a valid plan: {e}", use_llm=False)
                except EngineFailure as e:
                    await self.finisher.fail(ctx, str(e), use_llm=False)
                except Exception as e:
                    log.exception("run %s crashed", ctx.run.id)
                    await self.finisher.fail(ctx, f"Internal error: {type(e).__name__}: {truncate(str(e), 300)}",
                                             use_llm=False)
        except (Cancelled, Paused):  # raised by the finisher's last checkpoint
            if ctx.cancel_requested:
                self._finalize_cancel(ctx)
            else:
                ctx.persist()
        finally:
            await ctx.flush()
            trace_sink.reset(token)

    def _pause_for_outage(self, ctx: RunContext, exc: PlannerUnavailable) -> None:
        """A transient Muse outage parks the run instead of failing it: everything done so far is persisted and
        phases are idempotent, so Resume simply retries from where it stopped."""
        ctx.resume_status = ctx.run.status if ctx.run.status is not RunStatus.PAUSED else ctx.resume_status
        ctx.pause_requested = True
        ctx.pause_observed = True
        ctx.run.error = f"{exc} (paused; resume to retry)"
        ctx.log("Muse is temporarily unavailable; the run is paused and can be resumed", level="warning",
                agent="planner", error=str(exc))
        ctx.set_status(RunStatus.PAUSED)

    def _finalize_cancel(self, ctx: RunContext) -> None:
        if ctx.cancel_finalized:
            return
        ctx.cancel_finalized = True
        if ctx.run.plan is not None:
            for node in ctx.plan.nodes.values():
                if node.status in (NodeStatus.PENDING, NodeStatus.READY, NodeStatus.SIMULATED,
                                   NodeStatus.AWAITING_APPROVAL, NodeStatus.RUNNING):
                    ctx.set_node_status(node, NodeStatus.CANCELLED,
                                        agent="executor" if node.kind is NodeKind.ACTION else "evaluator")
        self._close_pending_approval(ctx, "run cancelled")
        previous = ctx.cancelled_from or ctx.run.status
        ctx.run.status = RunStatus.CANCELLED
        ctx.persist()
        ctx.emit("run.status", status=RunStatus.CANCELLED.value, previous=previous.value)

    def _close_pending_approval(self, ctx: RunContext, note: str) -> None:
        pending = self.svc.store.pending_approval(ctx.run.id)
        if pending is None:
            return
        for item in pending.items:
            if item.decision == "pending":
                item.decision = "rejected"
        pending.status = "resolved"
        pending.resolved_at = now_ms()
        pending.note = note
        self.svc.store.save_approval(pending)
        ctx.emit("approval.resolved", approval=pending)

    def _after_rollback(self, ctx: RunContext, report: RollbackReport) -> None:
        compensated = {e.node_id for e in report.compensated}
        if ctx.run.plan is not None:
            for node in ctx.plan.nodes.values():
                if node.id in compensated:
                    ctx.set_node_status(node, NodeStatus.COMPENSATED)
                elif node.status in (NodeStatus.PENDING, NodeStatus.READY, NodeStatus.SIMULATED,
                                     NodeStatus.AWAITING_APPROVAL, NodeStatus.RUNNING):
                    ctx.set_node_status(node, NodeStatus.CANCELLED)
        self._close_pending_approval(ctx, "run rolled back")
        lines = ["", "**Rollback**"]
        lines += [f"- Undone: {e.summary}" for e in report.compensated if e not in report.audience_notified]
        lines += [f"- Undone, but recipients were already notified: {e.summary}" for e in report.audience_notified]
        lines += [f"- Not reversible: {e.summary}" for e in report.not_reversible]
        lines += [f"- Could not undo: {e.summary} ({why})" for e, why in report.failed]
        ctx.run.summary = (ctx.run.summary + "\n" + "\n".join(lines)).strip()
        ctx.log("Rollback finished", level="warning" if report.not_reversible or report.failed else "info",
                agent="executor", compensated=[e.id for e in report.compensated],
                not_reversible=[{"id": e.id, "summary": e.summary, "effect": e.effect.value}
                                for e in report.not_reversible],
                audience_notified=[e.id for e in report.audience_notified],
                failed=[{"id": e.id, "error": why} for e, why in report.failed])
        ctx.set_status(RunStatus.ROLLED_BACK)

    # -- crash recovery ------------------------------------------------------------
    async def _recover(self, ctx: RunContext) -> None:
        """Reconcile half-done effects by reading the world back, then resume from the persisted status."""
        ctx.log("Recovering run after a restart", level="warning")
        token = trace_sink.set(ctx.on_trace)
        try:
            unknown = await self._reconcile_intents(ctx)
        finally:
            trace_sink.reset(token)
        self._reset_interrupted_steps(ctx)
        ctx.persist()
        status = ctx.run.status
        if unknown and status not in (RunStatus.PAUSED, RunStatus.CLARIFYING) \
                and self.pipeline.request_approval(ctx):
            return
        if status in WAITING_STATUSES:
            return
        self._spawn(ctx, PHASE_FOR_STATUS.get(status, Phase.INTAKE))

    async def _reconcile_intents(self, ctx: RunContext) -> bool:
        unknown_any = False
        for record in [e for e in self.svc.store.effects(ctx.run.id) if e.status == "intent"]:
            verdict = await self.ledger.reconcile(ctx, record)
            node = ctx.plan.nodes.get(record.node_id) if ctx.run.plan is not None else None
            ctx.log(f"Reconciled in-flight effect: {record.summary} -> "
                    f"{'applied' if verdict else 'not applied' if verdict is False else 'unknown'}",
                    agent="executor", node_id=record.node_id or None, idempotency_key=record.idempotency_key)
            if verdict is True:
                record = self.ledger.mark(ctx, record, "applied")
                if node is not None:
                    node.result = ToolResult(ok=True, output={**record.target, "reconciled": True}, effect=record,
                                             tainted=node.tainted)
                    ctx.set_node_status(node, NodeStatus.SUCCEEDED)
            elif verdict is False:
                self.ledger.mark(ctx, record, "failed")
                if node is not None:
                    gated = node.gate is not None and node.gate.verdict is not Verdict.BLOCK
                    ctx.set_node_status(node, NodeStatus.READY if gated else NodeStatus.SIMULATED)
            else:
                record = self.ledger.mark(ctx, record, "unknown")
                unknown_any = True
                if node is not None:
                    node.result = ToolResult(ok=False, effect=record, error=ToolError(
                        kind=ErrorKind.UNKNOWN,
                        message="Adjutant restarted while this action was in flight; its outcome is unknown"))
                    ctx.set_node_status(node, NodeStatus.AWAITING_APPROVAL, agent="guardian")
        return unknown_any

    def _reset_interrupted_steps(self, ctx: RunContext) -> None:
        """Steps persisted as RUNNING were cut off mid-call. Reads are safe to redo; writes go back to their
        cleared state (the ledger + idempotency key protect a re-run)."""
        if ctx.run.plan is None:
            return
        for node in action_nodes(ctx.plan):
            if node.status is not NodeStatus.RUNNING:
                continue
            if is_write(ctx.spec(node.tool)):
                cleared = node.gate is not None and node.gate.verdict is not Verdict.BLOCK and \
                    ctx.run.status in (RunStatus.EXECUTING, RunStatus.VERIFYING)
                ctx.set_node_status(node, NodeStatus.READY if cleared else NodeStatus.PENDING)
            else:
                ok = node.result is not None and node.result.ok
                ctx.set_node_status(node, NodeStatus.SUCCEEDED if ok else NodeStatus.PENDING)


def _retrieve_exception(task: asyncio.Task[None]) -> None:
    if not task.cancelled():
        exc = task.exception()
        if exc is not None and not isinstance(exc, Exception):
            log.error("run task %s died: %r", task.get_name(), exc)


def build_orchestrator(store: RunStore, bus: EventBus, llm: LLM, judge: Judge, registry: ToolRegistry,
                       workspaces: WorkspaceStore, *, config: EngineConfig | None = None) -> AdjutantOrchestrator:
    return AdjutantOrchestrator(store, bus, llm, judge, registry, workspaces, config=config)

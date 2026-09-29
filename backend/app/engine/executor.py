"""Executes one plan step in shadow or commit mode.

Shadow:  reads and llm.* steps run for real and their outputs are cached on the node; writes are simulated
         (``tool.simulate``) and produce a ledger row with status ``simulated`` and a preview.
Commit:  cached read outputs are REUSED -- "what you approved is what gets sent" -- unless the read consumed a
         simulated write output (then it re-runs on the real value). Writes go through the effect ledger, and
         only after checking that the binding hash still matches what was gated/approved; otherwise they are
         re-simulated and re-gated first.

Every real call passes three guards first: the kill switch / cancel checkpoint, the budget, and loop detection
(the same tool with the same args hash attempted more than twice in a run → the step fails).
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..core.interfaces import Tool
from ..core.models import (
    ErrorKind,
    NodeKind,
    NodeStatus,
    PlanNode,
    RecoveryDecision,
    RecoveryStrategy,
    ToolContext,
    ToolError,
    ToolResult,
    ToolSpec,
    args_hash,
    now_ms,
)
from .binding import BoundArgs, Mode, available_outputs, bind, is_write
from .context import RunContext, Services
from .errors import BindingError, BudgetExceeded, Paused
from .events import current_node
from .gate import Clearance, Gatekeeper
from .ledger import EffectLedger, idempotency_key
from .recovery import Outcome, RecoveryManager
from .taint import is_untrusted_source
from .templates import argument_taint
from .util import compact

ToolCall = Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]]


def failed(kind: ErrorKind, message: str, *, retryable: bool = False) -> ToolResult:
    return ToolResult(ok=False, error=ToolError(kind=kind, message=message, retryable=retryable))


class NodeExecutor:
    def __init__(self, svc: Services, ledger: EffectLedger, gatekeeper: Gatekeeper, recovery: RecoveryManager) -> None:
        self.svc = svc
        self.cfg = svc.config
        self.ledger = ledger
        self.gatekeeper = gatekeeper
        self.recovery = recovery

    # -- entry point -------------------------------------------------------------
    async def run_node(self, ctx: RunContext, node_id: str, mode: Mode) -> None:
        node = ctx.plan.nodes.get(node_id)
        if node is None or node.kind is not NodeKind.ACTION:
            return
        token = current_node.set(node_id)
        entry_status = node.status
        try:
            if mode is Mode.COMMIT and not is_write(ctx.spec(node.tool)):
                if not self._stale(ctx, node):
                    return  # cached shadow output is still exactly right
                ctx.log("Input changed since the shadow run (a simulated value became real); re-running",
                        agent="executor", node_id=node.id)
            await self._execute(ctx, node, mode)
        except (Paused, BudgetExceeded):
            # Nothing started: put the step back so a resume dispatches it again.
            if ctx.plan.nodes.get(node_id) is node and node.status is NodeStatus.RUNNING:
                ctx.set_node_status(node, entry_status)
                ctx.persist()
            raise
        finally:
            current_node.reset(token)

    def _stale(self, ctx: RunContext, node: PlanNode) -> bool:
        if node.status is not NodeStatus.SUCCEEDED:
            return True
        try:
            return bind(ctx.plan, node, ctx.spec, Mode.COMMIT).actual != node.resolved_args
        except BindingError:
            return True

    # -- attempt loop ---------------------------------------------------------------
    async def _execute(self, ctx: RunContext, node: PlanNode, mode: Mode) -> None:
        self._mark_running(ctx, node, mode)
        while True:
            if ctx.plan.nodes.get(node.id) is not node:
                return  # replaced by a re-plan while we were waiting
            spec, tool = ctx.spec(node.tool), ctx.tool(node.tool)
            if spec is None or tool is None:
                self.fail(ctx, node, ErrorKind.NOT_FOUND, f"tool '{node.tool}' is not available")
                return
            try:
                bound = bind(ctx.plan, node, ctx.spec, mode)
            except BindingError as e:
                if await self.recovery.handle(ctx, node, spec, failed(e.kind, e.message),
                                              engine_kind=e.kind) is Outcome.RETRY:
                    continue
                return
            self._record_binding(ctx, node, spec, bound, mode)
            if not is_write(spec):
                result = await self._run_read(ctx, node, spec, tool, bound)
            elif mode is Mode.SHADOW:
                result = await self._simulate(ctx, node, spec, tool, bound)
            else:
                result = await self._commit_write(ctx, node, spec, tool, bound)
            if result is None or result.ok:
                return
            if await self.recovery.handle(ctx, node, spec, result) is not Outcome.RETRY:
                return

    def _mark_running(self, ctx: RunContext, node: PlanNode, mode: Mode) -> None:
        node.started_at = node.started_at or now_ms()
        ctx.set_node_status(node, NodeStatus.RUNNING)
        for gid in _ancestor_goals(ctx, node):
            goal = ctx.plan.nodes[gid]
            if goal.status is NodeStatus.PENDING:
                ctx.set_node_status(goal, NodeStatus.RUNNING)
        ctx.persist()

    def _record_binding(self, ctx: RunContext, node: PlanNode, spec: ToolSpec, bound: BoundArgs, mode: Mode) -> None:
        prov = ctx.provenance()
        node.resolved_args = bound.actual
        # node.tainted: any arg derives from untrusted content, or the step itself returns external content.
        # node.tainted_args: the args that are still tainted after endorsement (what the gate cares about most:
        # untrusted data in a CONTROL arg such as a recipient means the action itself may be hijacked).
        node.tainted = prov.args_tainted(node.id) or is_untrusted_source(spec)
        node.tainted_args = argument_taint(node.args, available_outputs(ctx.plan, mode),
                                           tainted=prov.output_tainted, endorsed=ctx.endorser())
        ctx.emit("node.started", agent="executor", node_id=node.id, tool=spec.name, args=compact(bound.actual),
                 mode="shadow" if mode is Mode.SHADOW and is_write(spec) else "live", args_hash=bound.hash,
                 tainted=node.tainted, tainted_args=node.tainted_args)

    # -- one guarded call ------------------------------------------------------------
    async def _invoke(self, ctx: RunContext, node: PlanNode, spec: ToolSpec, call: ToolCall, bound: BoundArgs, *,
                      real: bool, before_call: Callable[[], Any] | None = None) -> ToolResult | None:
        """Checkpoint → budget → loop detection → (ledger intent) → call. None means the step was settled
        here (loop detected) and no result is to be handled."""
        ctx.checkpoint()
        ctx.check_budget()
        if real:
            signature = (spec.name, args_hash(spec.name, bound.actual))
            if ctx.call_counts[signature] >= self.cfg.max_identical_calls:
                self._fail_loop(ctx, node, spec, ctx.call_counts[signature])
                return None
            ctx.call_counts[signature] += 1
        node.attempts += 1
        ctx.run.metrics.tool_calls += 1
        if before_call is not None:
            before_call()  # synchronous on purpose: nothing may interleave between intent and call
        key = idempotency_key(ctx.run.id, node.id, bound.hash)
        tctx = ctx.tool_context(node.id, key, "live" if real else "shadow")
        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(call(dict(bound.actual), tctx), timeout=self.cfg.tool_timeout_s)
        except TimeoutError:
            result = failed(ErrorKind.TRANSIENT, f"{spec.name} timed out after {self.cfg.tool_timeout_s:g}s",
                            retryable=True)
        except Exception as e:  # noqa: BLE001 - a tool must never take the engine down
            result = failed(ErrorKind.UNKNOWN, f"{type(e).__name__}: {e}")
        if not isinstance(result, ToolResult):
            result = failed(ErrorKind.UNKNOWN, f"{spec.name} returned no ToolResult")
        if not result.latency_ms:
            result.latency_ms = int((time.perf_counter() - started) * 1000)
        return result

    def _succeed(self, ctx: RunContext, node: PlanNode, result: ToolResult, status: NodeStatus) -> None:
        if result.output is None:
            result.output = {}
        result.tainted = node.tainted
        node.result = result
        ctx.set_node_status(node, status)
        ctx.emit("node.result", agent="executor", node_id=node.id, result=compact(result))
        ctx.persist()

    def fail(self, ctx: RunContext, node: PlanNode, kind: ErrorKind, message: str) -> None:
        node.result = failed(kind, message)
        ctx.set_node_status(node, NodeStatus.FAILED)
        ctx.emit("node.result", agent="executor", node_id=node.id, result=compact(node.result))
        ctx.persist()

    def _fail_loop(self, ctx: RunContext, node: PlanNode, spec: ToolSpec, count: int) -> None:
        decision = RecoveryDecision(cause=ErrorKind.UNKNOWN, strategy=RecoveryStrategy.ABORT, confidence=1.0,
                                    note=f"loop detected: {spec.name} with identical arguments was already "
                                         f"attempted {count} times in this run")
        node.recovery.append(decision)
        ctx.emit("recovery.decided", agent="guardian", node_id=node.id, decision=decision, suggested="abort")
        self.fail(ctx, node, ErrorKind.UNKNOWN, decision.note)

    # -- kinds of steps --------------------------------------------------------------
    async def _run_read(self, ctx: RunContext, node: PlanNode, spec: ToolSpec, tool: Tool,
                        bound: BoundArgs) -> ToolResult | None:
        result = await self._invoke(ctx, node, spec, tool.run, bound, real=True)
        if result is not None and result.ok:
            self._succeed(ctx, node, result, NodeStatus.SUCCEEDED)
        return result

    async def _simulate(self, ctx: RunContext, node: PlanNode, spec: ToolSpec, tool: Tool,
                        bound: BoundArgs) -> ToolResult | None:
        result = await self._invoke(ctx, node, spec, tool.simulate, bound, real=False)
        if result is not None and result.ok:
            result.simulated = True
            result.effect = self.ledger.record_simulated(ctx, node, spec, bound, result)
            node.gate = None  # a new preview must be gated afresh
            self._succeed(ctx, node, result, NodeStatus.SIMULATED)
        return result

    async def _commit_write(self, ctx: RunContext, node: PlanNode, spec: ToolSpec, tool: Tool,
                            bound: BoundArgs) -> ToolResult | None:
        clearance = self.gatekeeper.clearance(ctx, node, bound)
        if clearance is None:
            # Approval is bound to the args hash: new content means a new simulation and a new gate.
            sim = await self._invoke(ctx, node, spec, tool.simulate, bound, real=False)
            if sim is None or not sim.ok:
                return sim
            sim.simulated = True
            sim.effect = self.ledger.record_simulated(ctx, node, spec, bound, sim)
            sim.tainted = node.tainted
            node.result = sim
            clearance = await self.gatekeeper.regate(ctx, node, spec, bound, sim.effect.preview)
            if clearance is Clearance.CLEARED:
                ctx.set_node_status(node, NodeStatus.RUNNING)
        if clearance is Clearance.BLOCKED:
            ctx.set_node_status(node, NodeStatus.BLOCKED, agent="guardian")
            ctx.persist()
            return None
        if clearance is Clearance.WAITING:
            ctx.persist()
            return None

        key = idempotency_key(ctx.run.id, node.id, bound.hash)
        prior = ctx.svc.store.effect_by_key(key)
        if prior is not None and prior.status == "applied":
            # The ledger says this exact call already happened (e.g. reconciled after a crash): never repeat it.
            ctx.log("Effect already applied for this idempotency key; not calling the tool again",
                    agent="executor", node_id=node.id, idempotency_key=key)
            self._succeed(ctx, node, ToolResult(ok=True, output={**prior.target, "reconciled": True},
                                                effect=prior), NodeStatus.SUCCEEDED)
            return None

        intent_box: dict[str, Any] = {}

        def record_intent() -> None:
            intent_box["record"] = self.ledger.record_intent(ctx, node, spec, bound)
            ctx.persist()

        result = await self._invoke(ctx, node, spec, tool.run, bound, real=True, before_call=record_intent)
        record = intent_box.get("record")
        if result is None or record is None:
            return result
        if result.ok:
            result.effect = self.ledger.mark_applied(ctx, record, result)
            result.simulated = False
            self._succeed(ctx, node, result, NodeStatus.SUCCEEDED)
        else:
            self.ledger.mark_failed(ctx, record, result.error)
        return result


def _ancestor_goals(ctx: RunContext, node: PlanNode) -> list[str]:
    out = []
    cur = node.parent_id
    while cur and cur in ctx.plan.nodes:
        out.append(cur)
        cur = ctx.plan.nodes[cur].parent_id
    return out

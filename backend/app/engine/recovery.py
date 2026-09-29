"""Failure recovery (SPEC §3): Jev classifies, code decides.

``RecoveryPolicy`` is a pure function from (Jev's classification, the tool's own error, the step's history,
budgets) to a strategy. Jev's suggested strategy is advisory: code owns the mapping, so a confident-but-wrong
classification cannot, say, retry a permission error forever. Errors detected by code itself (a template path
that does not exist) skip Jev entirely because their cause is certain.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum

from ..core.models import (
    ErrorKind,
    NodeStatus,
    PlanNode,
    RecoveryDecision,
    RecoveryStrategy,
    ToolError,
    ToolResult,
    ToolSpec,
)
from . import argschema
from .context import EngineConfig, RunContext, Services
from .errors import EngineFailure, RunAborted
from .planner import Planner
from .util import compact, redact_quoted

S = RecoveryStrategy


class Outcome(str, Enum):
    RETRY = "retry"  # run the step again (same or repaired args / other tool)
    SETTLED = "settled"  # the step reached a resting state (skipped, failed, awaiting a human, replaced)


@dataclass(frozen=True)
class StepHistory:
    attempts: int
    repairs: int
    optional: bool
    alternatives: list[str]
    replans_left: bool


class RecoveryPolicy:
    def __init__(self, config: EngineConfig) -> None:
        self.cfg = config

    def choose(self, decision: RecoveryDecision, error: ToolError, h: StepHistory) -> tuple[RecoveryStrategy, str]:
        can_retry = h.attempts < self.cfg.max_attempts
        fallback = (S.SKIP, "optional step: continue without it") if h.optional else \
            (S.ASK_HUMAN, "needs a human decision")

        # A tool that says "transient, retry me" is a fact, not a guess.
        if error.retryable and error.kind is ErrorKind.TRANSIENT and can_retry:
            return S.RETRY_SAME, f"tool marked the error retryable (attempt {h.attempts} of {self.cfg.max_attempts})"
        if decision.confidence < self.cfg.min_recovery_confidence:
            return fallback[0], (f"classification confidence {decision.confidence:.2f} < "
                                 f"{self.cfg.min_recovery_confidence}: {fallback[1]}")

        cause = decision.cause
        if cause is ErrorKind.TRANSIENT:
            if can_retry:
                return S.RETRY_SAME, f"transient: back off and retry (attempt {h.attempts} of {self.cfg.max_attempts})"
            return fallback[0], f"still failing after {h.attempts} attempts: {fallback[1]}"
        if h.optional:
            return S.SKIP, f"{cause.value} on an optional step: skip it"
        if cause is ErrorKind.INVALID_ARGS:
            if h.repairs < self.cfg.max_arg_repairs:
                return S.REPAIR_ARGS, "invalid arguments: ask Muse to repair them"
            return S.ASK_HUMAN, f"arguments still invalid after {h.repairs} repairs"
        if cause in (ErrorKind.NOT_FOUND, ErrorKind.PRECONDITION):
            if h.alternatives and decision.strategy is S.SWITCH_TOOL:
                return S.SWITCH_TOOL, f"{cause.value}: try {h.alternatives[0]}"
            if h.replans_left:
                return S.REPLAN, f"{cause.value}: re-plan the parent goal"
            if h.alternatives:
                return S.SWITCH_TOOL, f"{cause.value}: no re-plans left, try {h.alternatives[0]}"
            return S.ASK_HUMAN, f"{cause.value} and no re-plans left"
        if cause in (ErrorKind.AUTH, ErrorKind.PERMISSION):
            return S.ASK_HUMAN, f"{cause.value}: only a human can fix this"
        # UNKNOWN: follow Jev's suggestion only where code can bound it.
        if decision.strategy is S.RETRY_SAME and can_retry:
            return S.RETRY_SAME, "unknown cause; Jev suggests a retry"
        if decision.strategy is S.REPAIR_ARGS and h.repairs < self.cfg.max_arg_repairs:
            return S.REPAIR_ARGS, "unknown cause; Jev suggests repairing the arguments"
        if decision.strategy is S.ABORT and decision.confidence >= 0.7:
            return S.ABORT, "unknown cause; Jev is confident the run cannot continue"
        return S.ASK_HUMAN, "unknown cause"


class RecoveryManager:
    def __init__(self, svc: Services, planner: Planner) -> None:
        self.svc = svc
        self.planner = planner
        self.policy = RecoveryPolicy(svc.config)

    def alternatives(self, ctx: RunContext, node: PlanNode, spec: ToolSpec) -> list[str]:
        tried = ctx.switched_tools.get(node.id, set()) | {spec.name}
        return sorted(s.name for s in ctx.specs().values()
                      if s.app == spec.app and s.effect is spec.effect and s.name not in tried)

    async def handle(self, ctx: RunContext, node: PlanNode, spec: ToolSpec, result: ToolResult, *,
                     engine_kind: ErrorKind | None = None) -> Outcome:
        """Record the failure on the node, decide, apply. Returns whether the executor should try again."""
        error = result.error or ToolError(message="the tool reported failure without details")
        node.result = result
        ctx.emit("node.result", agent="executor", node_id=node.id, result=compact(result))

        alternatives = self.alternatives(ctx, node, spec)
        if engine_kind is not None:
            strategy = S.REPAIR_ARGS if engine_kind is ErrorKind.INVALID_ARGS else S.REPLAN
            decision = RecoveryDecision(cause=engine_kind, strategy=strategy, confidence=1.0,
                                        note="detected by code")
        else:
            decision = await self._classify(ctx, node, spec, error, alternatives)
        history = StepHistory(
            attempts=node.attempts,
            repairs=sum(1 for d in node.recovery if d.strategy is S.REPAIR_ARGS),
            optional=node.optional,
            alternatives=alternatives,
            replans_left=ctx.can_replan(),
        )
        strategy, why = self.policy.choose(decision, error, history)
        return await self._apply(ctx, node, spec, error, decision, strategy, why)

    async def _classify(self, ctx: RunContext, node: PlanNode, spec: ToolSpec, error: ToolError,
                        alternatives: list[str]) -> RecoveryDecision:
        try:
            return await self.svc.judge.classify_failure(
                tool=spec, args=node.resolved_args or node.args, error=error, attempts=node.attempts,
                optional=node.optional, alternatives=alternatives)
        except Exception as e:  # noqa: BLE001 - the judge is supposed to fail safe; if it raises, we do
            return RecoveryDecision(cause=error.kind, strategy=S.ASK_HUMAN, confidence=0.0,
                                    note=f"judge unavailable ({type(e).__name__}), failing safe")

    def _record(self, ctx: RunContext, node: PlanNode, decision: RecoveryDecision, strategy: RecoveryStrategy,
                why: str) -> RecoveryDecision:
        note = why if not decision.note or decision.note == why else f"{why} (judge: {decision.note})"
        final = decision.model_copy(update={"strategy": strategy, "note": note})
        node.recovery.append(final)
        ctx.emit("recovery.decided", agent="guardian", node_id=node.id, decision=final,
                 suggested=decision.strategy.value)
        ctx.persist()
        return final

    async def _apply(self, ctx: RunContext, node: PlanNode, spec: ToolSpec, error: ToolError,
                     decision: RecoveryDecision, strategy: RecoveryStrategy, why: str) -> Outcome:
        self._record(ctx, node, decision, strategy, why)
        cfg = self.svc.config
        if strategy is S.RETRY_SAME:
            delay = min(cfg.retry_base_delay_s * (2 ** max(0, node.attempts - 1)), cfg.retry_max_delay_s)
            if delay > 0:
                await asyncio.sleep(delay)
            return Outcome.RETRY
        if strategy is S.REPAIR_ARGS:
            try:
                new_args = await self.planner.repair_args(ctx, node, spec, self._error_for_muse(ctx, node, error))
            except EngineFailure as e:
                return await self._escalate(ctx, node, decision, f"argument repair unavailable: {e}")
            if new_args is not None and new_args != node.args:
                ctx.log("Arguments repaired", agent="executor", node_id=node.id, before=node.args, after=new_args)
                node.args = new_args
                return Outcome.RETRY
            return await self._escalate(ctx, node, decision, "Muse could not produce different valid arguments")
        if strategy is S.SWITCH_TOOL:
            try:
                switched = await self._switch_tool(ctx, node, spec, error)
            except EngineFailure:
                switched = False
            if switched:
                return Outcome.RETRY
            return await self._escalate(ctx, node, decision, "no usable alternative tool")
        if strategy is S.REPLAN:
            try:
                if await self._replan(ctx, node, error):
                    return Outcome.SETTLED
            except EngineFailure as e:
                return await self._escalate(ctx, node, decision, f"re-planning unavailable: {e}")
            return await self._escalate(ctx, node, decision, "re-plan produced no usable steps")
        if strategy is S.ASK_HUMAN:
            ctx.set_node_status(node, NodeStatus.AWAITING_APPROVAL, agent="guardian")
            ctx.persist()
            return Outcome.SETTLED
        if strategy is S.SKIP:
            skip(ctx, node, f"Skipped after failure: {error.message}", ErrorKind.PRECONDITION)
            return Outcome.SETTLED
        ctx.set_node_status(node, NodeStatus.FAILED)
        ctx.persist()
        raise RunAborted(f"{node.title}: {error.message}")

    @staticmethod
    def _error_for_muse(ctx: RunContext, node: PlanNode, error: ToolError) -> str:
        """Engine-detected errors are ours (structure only). A *tool* error for a step with tainted args may
        echo untrusted values, which must not reach the planner lane."""
        message = error.message
        if node.recovery and node.recovery[-1].note != "detected by code" and ctx.provenance().args_tainted(node.id):
            message = redact_quoted(message)
        return f"{error.kind.value}: {message}"

    async def _escalate(self, ctx: RunContext, node: PlanNode, decision: RecoveryDecision, why: str) -> Outcome:
        strategy = S.SKIP if node.optional else S.ASK_HUMAN
        self._record(ctx, node, decision, strategy, why)
        if strategy is S.SKIP:
            skip(ctx, node, f"Skipped: {why}", ErrorKind.PRECONDITION)
        else:
            ctx.set_node_status(node, NodeStatus.AWAITING_APPROVAL, agent="guardian")
            ctx.persist()
        return Outcome.SETTLED

    async def _switch_tool(self, ctx: RunContext, node: PlanNode, spec: ToolSpec, error: ToolError) -> bool:
        for name in self.alternatives(ctx, node, spec):
            alt = ctx.spec(name)
            if alt is None or ctx.tool(name) is None:
                continue
            ctx.switched_tools.setdefault(node.id, set()).add(spec.name)
            props = argschema.properties(alt.input_schema)
            node.tool = name
            node.args = {k: v for k, v in node.args.items() if not props or k in props}
            if argschema.arg_problems(node.args, alt.input_schema, allow_templates=True):
                repaired = await self.planner.repair_args(ctx, node, alt, f"switched from {spec.name} after: "
                                                                          f"{error.message}")
                if repaired is None:
                    continue
                node.args = repaired
            ctx.log(f"Switched {spec.name} -> {name}", agent="executor", node_id=node.id)
            return True
        return False

    async def _replan(self, ctx: RunContext, node: PlanNode, error: ToolError) -> bool:
        goal_id = node.parent_id or ctx.plan.root_id
        async with ctx.goal_lock(goal_id):
            if ctx.plan.nodes.get(node.id) is not node:
                return True  # a sibling's re-plan already replaced this step
            ctx.set_node_status(node, NodeStatus.FAILED)
            if not ctx.can_replan():
                return False
            reason = f"Step {node.id} '{node.title}' ({node.tool}) failed: {self._error_for_muse(ctx, node, error)}"
            revision = await self.planner.replan_goal(ctx, goal_id, reason=reason)
            return revision is not None


def skip(ctx: RunContext, node: PlanNode, reason: str, kind: ErrorKind) -> None:
    """Skip with the reason recorded on the node (kind PERMISSION = a human/policy decision, which
    verification treats as "not done on purpose")."""
    prior = node.result
    node.result = ToolResult(ok=False, error=ToolError(kind=kind, message=reason),
                             simulated=bool(prior and prior.simulated), effect=prior.effect if prior else None,
                             tainted=node.tainted)
    ctx.set_node_status(node, NodeStatus.SKIPPED)
    ctx.persist()

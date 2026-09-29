"""The run lifecycle as a small state machine of idempotent phases (SPEC §2):

    INTAKE → PLAN → SHADOW → GATE → COMMIT → VERIFY → COMPLETE
                      ↑___________________________|   (re-plan: new steps are shadowed and gated again)

Each phase returns the next phase, or None when the run must wait (for a human, or because it is terminal).
Phases are idempotent: each derives its work from persisted node state, so resuming after a pause, an approval
or a process restart is simply "run the phase for the current status again".
"""
from __future__ import annotations

from enum import Enum

from ..core.models import NodeKind, NodeStatus, RunStatus
from .binding import Mode
from .completion import Finisher
from .context import RunContext, Services, action_nodes
from .executor import NodeExecutor
from .gate import Gatekeeper
from .planner import Planner
from .scheduler import DagScheduler
from .util import compact
from .verify import Verifier, VerifyOutcome


class Phase(str, Enum):
    INTAKE = "intake"
    PLAN = "plan"
    SHADOW = "shadow"
    GATE = "gate"
    COMMIT = "commit"
    VERIFY = "verify"
    COMPLETE = "complete"


#: where to pick a run up from its persisted status (resume, approval, crash recovery)
PHASE_FOR_STATUS: dict[RunStatus, Phase] = {
    RunStatus.CREATED: Phase.INTAKE,
    RunStatus.PLANNING: Phase.INTAKE,
    RunStatus.SHADOWING: Phase.SHADOW,
    RunStatus.EXECUTING: Phase.COMMIT,
    RunStatus.VERIFYING: Phase.VERIFY,
}


class RunPipeline:
    def __init__(self, svc: Services, planner: Planner, executor: NodeExecutor, gatekeeper: Gatekeeper,
                 verifier: Verifier, finisher: Finisher) -> None:
        self.svc = svc
        self.cfg = svc.config
        self.planner = planner
        self.executor = executor
        self.gatekeeper = gatekeeper
        self.verifier = verifier
        self.finisher = finisher

    async def drive(self, ctx: RunContext, phase: Phase | None) -> None:
        handlers = {
            Phase.INTAKE: self.intake, Phase.PLAN: self.plan, Phase.SHADOW: self.shadow, Phase.GATE: self.gate,
            Phase.COMMIT: self.commit, Phase.VERIFY: self.verify, Phase.COMPLETE: self.complete,
        }
        while phase is not None:
            phase = await handlers[phase](ctx)

    # -- 1. intent + clarification -------------------------------------------------
    async def intake(self, ctx: RunContext) -> Phase | None:
        run = ctx.run
        if run.plan is not None:
            return Phase.SHADOW
        ctx.transition(RunStatus.PLANNING)
        clar = run.clarification
        if clar is not None and clar.get("answer") is None:
            ctx.set_status(RunStatus.CLARIFYING)
            return None
        if run.intent is None or (clar is not None and not clar.get("intent_updated")):
            run.intent = await self.planner.parse_intent(ctx)
            if clar is not None:
                clar["intent_updated"] = True
            ctx.persist()
            ctx.emit("intent.parsed", agent="planner", intent=run.intent)
        if clar is None and await self._needs_clarification(ctx):
            return None
        return Phase.PLAN

    async def _needs_clarification(self, ctx: RunContext) -> bool:
        """Ask at most once per run, and only when Jev is confident something essential is missing."""
        try:
            p, which = await self.svc.judge.needs_clarification(
                ctx.run.request, ctx.intent(),
                {"profile": ctx.profile(), "apps": sorted({s.app for s in ctx.specs().values()})})
        except Exception as e:  # noqa: BLE001
            ctx.log(f"Clarification check unavailable ({type(e).__name__}); proceeding", level="warning",
                    agent="evaluator")
            return False
        if p <= self.cfg.clarification_threshold:
            return False
        question = await self.planner.phrase_clarification(ctx, which)
        ctx.checkpoint()
        ctx.run.clarification = {"question": question, "answer": None, "which": which, "p": round(float(p), 3)}
        ctx.set_status(RunStatus.CLARIFYING)
        ctx.emit("clarification.requested", agent="planner", question=question, which=which, p=round(float(p), 3))
        return True

    # -- 2-3. recall + plan --------------------------------------------------------
    async def plan(self, ctx: RunContext) -> Phase | None:
        if ctx.run.plan is not None:
            return Phase.SHADOW
        ctx.transition(RunStatus.PLANNING)
        memories, scores = await self._recall(ctx)
        plan, title = await self.planner.draft_plan(ctx, memories, scores)
        ctx.checkpoint()
        ctx.run.plan = plan
        if title:
            ctx.run.title = title[:120]
        ctx.persist()
        ctx.emit("plan.created", agent="planner", plan=plan)
        return Phase.SHADOW

    async def _recall(self, ctx: RunContext):
        items = ctx.svc.store.memories(ctx.workspace_id)
        ranked: list = []
        if items:
            try:
                ranked = await self.svc.judge.rank_memories(ctx.request_text(), items,
                                                            limit=self.cfg.memory_recall_limit)
            except Exception as e:  # noqa: BLE001
                ctx.log(f"Memory ranking unavailable ({type(e).__name__})", level="warning", agent="evaluator")
        kept = [(m, float(p)) for m, p in ranked if float(p) >= self.cfg.memory_min_relevance]
        ctx.emit("memory.recalled", agent="evaluator",
                 items=[{"id": m.id, "kind": m.kind, "text": m.text, "p": round(p, 3)} for m, p in kept])
        return [m for m, _ in kept], {m.id: p for m, p in kept}

    # -- 4. shadow run -------------------------------------------------------------
    async def shadow(self, ctx: RunContext) -> Phase | None:
        ctx.transition(RunStatus.SHADOWING)
        for node in ctx.plan.nodes.values():
            if node.kind is NodeKind.GOAL and node.status is NodeStatus.PENDING:
                ctx.set_node_status(node, NodeStatus.RUNNING, agent="planner")
        await DagScheduler(ctx, self.executor, Mode.SHADOW, max_parallel=self.cfg.max_parallel_steps).run()
        await self.gatekeeper.prescan(ctx)
        ctx.checkpoint()
        simulated = [e for e in ctx.svc.store.effects(ctx.run.id) if e.status == "simulated"]
        ctx.emit("shadow.completed", agent="executor", effects=[compact(e) for e in simulated])
        ctx.persist()
        return Phase.GATE

    # -- 5-6. gate + approval --------------------------------------------------------
    async def gate(self, ctx: RunContext) -> Phase | None:
        ctx.checkpoint()
        await self.gatekeeper.gate_simulated(ctx)
        self.gatekeeper.cascade_skips(ctx)
        ctx.persist()
        if self.request_approval(ctx):
            return None
        return Phase.COMMIT

    def request_approval(self, ctx: RunContext) -> bool:
        """One batched review for everything that needs a human right now."""
        ctx.checkpoint()
        if ctx.svc.store.pending_approval(ctx.run.id) is not None:
            ctx.set_status(RunStatus.AWAITING_APPROVAL)
            return True
        approval = self.gatekeeper.build_approval(ctx)
        if approval is None:
            return False
        ctx.svc.store.save_approval(approval)
        ctx.set_status(RunStatus.AWAITING_APPROVAL)
        ctx.emit("approval.requested", agent="guardian", approval=approval)
        return True

    # -- 7. commit -----------------------------------------------------------------
    async def commit(self, ctx: RunContext) -> Phase | None:
        ctx.transition(RunStatus.EXECUTING)
        await DagScheduler(ctx, self.executor, Mode.COMMIT, max_parallel=self.cfg.max_parallel_steps).run()
        self.gatekeeper.cascade_skips(ctx)
        ctx.persist()
        if self.request_approval(ctx):
            return None
        if any(n.status is NodeStatus.PENDING for n in action_nodes(ctx.plan)):
            return Phase.SHADOW  # steps added by a re-plan during commit still need their shadow run + gate
        return Phase.VERIFY

    # -- 8. verify -----------------------------------------------------------------
    async def verify(self, ctx: RunContext) -> Phase | None:
        ctx.transition(RunStatus.VERIFYING)
        outcome = await self.verifier.run(ctx)
        return Phase.SHADOW if outcome is VerifyOutcome.REPLANNED else Phase.COMPLETE

    # -- 9. complete -----------------------------------------------------------------
    async def complete(self, ctx: RunContext) -> Phase | None:
        ctx.checkpoint()
        await self.finisher.finish(ctx)
        return None

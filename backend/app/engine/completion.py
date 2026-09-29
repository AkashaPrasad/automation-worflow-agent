"""Ending a run: the final report, durable memories, and the terminal status.

Ordering matters for clients: the terminal ``run.status`` event is always the LAST event of a run (the SSE
stream closes on it), so memory logs and ``run.completed`` / ``run.failed`` are emitted before it.

When Muse cannot or must not be used (it is down, or the budget is exhausted) the report is written by code from
the plan and the ledger, so a run never ends without an honest account of what happened.
"""
from __future__ import annotations

from ..core.models import (
    MemoryItem,
    NodeKind,
    NodeStatus,
    RunStatus,
    Verdict,
)
from .context import RunContext, Services
from .errors import BudgetExceeded, Cancelled, EngineFailure, RunInterrupted
from .plan import SETTLED_STATUSES, plan_outline
from .planner import Planner
from .util import similarity, truncate
from .verify import excused, unverified_goals

DUPLICATE_SIMILARITY = 0.8


class Finisher:
    def __init__(self, svc: Services, planner: Planner) -> None:
        self.svc = svc
        self.planner = planner

    # -- outcomes ----------------------------------------------------------------
    async def finish(self, ctx: RunContext) -> None:
        """After verification: COMPLETED if every goal is verified (or incomplete only by a human/policy
        decision), FAILED otherwise. Never report "done" for something the verifier did not see."""
        pending = unverified_goals(ctx) if ctx.run.plan is not None else []
        root = ctx.plan.nodes.get(ctx.plan.root_id) if ctx.run.plan is not None else None
        if pending and root is not None and root.status is NodeStatus.SUCCEEDED and root not in pending:
            # The root goal IS the user's request and it verified against observed results. A failed
            # criterion on an intermediate sub-goal (e.g. an extraction step a later step made redundant)
            # is reported, not fatal: proof-of-done is judged at the level the user asked for.
            titles = "; ".join(g.title for g in pending[:3])
            ctx.emit("log", agent="evaluator", level="warning",
                     message=f"Request verified; intermediate steps not verified: {titles}")
            await self.complete(ctx, note=f"completed; request verified, but intermediate steps were not: {titles}")
            return
        if pending:
            titles = "; ".join(g.title for g in pending[:3])
            await self.fail(ctx, f"Could not verify: {titles}")
            return
        await self.complete(ctx)

    async def complete(self, ctx: RunContext, note: str = "") -> None:
        ctx.run.summary = await self._summary(ctx, outcome=note or "completed")
        await self._remember(ctx)
        ctx.checkpoint()  # a cancel that raced the last step wins over "completed"
        self._close(ctx)
        ctx.persist()
        ctx.emit("run.completed", summary=ctx.run.summary)
        ctx.set_status(RunStatus.COMPLETED)

    async def fail(self, ctx: RunContext, error: str, *, use_llm: bool = True) -> None:
        if ctx.cancel_requested:
            raise Cancelled()  # the user's cancel wins over whatever went wrong meanwhile
        ctx.run.error = truncate(error, 1000)
        ctx.run.summary = await self._summary(ctx, outcome=f"failed: {error}") if use_llm else \
            self.fallback_summary(ctx, outcome=f"Stopped: {error}")
        self._close(ctx)
        ctx.persist()
        ctx.emit("run.failed", error=ctx.run.error)
        ctx.set_status(RunStatus.FAILED)

    async def stop_for_budget(self, ctx: RunContext, exc: BudgetExceeded) -> None:
        ctx.emit("budget.exceeded", limit=exc.limit, value=exc.value, max=exc.maximum)
        await self.fail(ctx, str(exc), use_llm=False)

    def _close(self, ctx: RunContext) -> None:
        """Steps that never ran end as cancelled so the tree has no dangling 'pending' boxes."""
        if ctx.run.plan is None:
            return
        for node in ctx.plan.nodes.values():
            if node.kind is NodeKind.ACTION and node.status not in SETTLED_STATUSES:
                ctx.set_node_status(node, NodeStatus.CANCELLED)
            elif node.kind is NodeKind.GOAL and node.status in (NodeStatus.PENDING, NodeStatus.RUNNING):
                ctx.set_node_status(node, NodeStatus.CANCELLED, agent="evaluator")

    # -- summary -----------------------------------------------------------------
    async def _summary(self, ctx: RunContext, *, outcome: str) -> str:
        if ctx.run.plan is None:
            return self.fallback_summary(ctx, outcome=outcome)
        try:
            text = await self.planner.summarize(ctx, outcome=outcome)
        except (EngineFailure, BudgetExceeded, RunInterrupted):
            text = ""
        return text.strip() or self.fallback_summary(ctx, outcome=outcome)

    def fallback_summary(self, ctx: RunContext, *, outcome: str) -> str:
        lines = [f"**Outcome:** {outcome}"]
        if ctx.run.plan is None:
            return "\n".join(lines + ["No plan was executed."])
        effects = ctx.svc.store.effects(ctx.run.id)
        applied = [e for e in effects if e.status == "applied"]
        if applied:
            lines.append("\n**Done**")
            lines.extend(f"- {e.summary}" for e in applied)
        attention = []
        for node in ctx.plan.actions():
            if node.status is NodeStatus.BLOCKED and node.gate is not None:
                attention.append(f"- Blocked: {node.title} ({'; '.join(node.gate.reasons[:2]) or 'policy'})")
            elif node.status in (NodeStatus.SKIPPED, NodeStatus.FAILED) and node.result and node.result.error:
                attention.append(f"- {node.status.value.title()}: {node.title} ({truncate(node.result.error.message, 160)})")
            elif node.status is NodeStatus.CANCELLED:
                attention.append(f"- Not run: {node.title}")
        if attention:
            lines.append("\n**Needs your attention**")
            lines.extend(attention)
        return "\n".join(lines)

    # -- memory ------------------------------------------------------------------
    async def _remember(self, ctx: RunContext) -> None:
        try:
            draft = await self.planner.extract_memories(ctx)
        except (EngineFailure, BudgetExceeded, RunInterrupted) as e:
            ctx.log(f"Memory extraction skipped: {type(e).__name__}", level="warning", agent="planner")
            return
        store = self.svc.store
        existing = store.memories(ctx.workspace_id)
        saved: list[MemoryItem] = []
        for kind, texts in (("fact", draft.facts), ("preference", draft.preferences), ("person", draft.people)):
            for text in texts:
                if any(m.kind == kind and similarity(m.text, text) >= DUPLICATE_SIMILARITY
                       for m in [*existing, *saved]):
                    continue
                saved.append(store.add_memory(MemoryItem(workspace_id=ctx.workspace_id, kind=kind, text=text,
                                                         source_run_id=ctx.run.id)))
        if self._clean_success(ctx):
            outline = draft.playbook_outline or plan_outline(ctx.plan)
            title = draft.playbook_title or ctx.run.title or truncate(ctx.run.request, 80)
            text = f"Playbook: {title}"
            if not any(m.kind == "playbook" and similarity(str(m.data.get("request", "")), ctx.run.request)
                       >= DUPLICATE_SIMILARITY for m in existing):
                saved.append(store.add_memory(MemoryItem(
                    workspace_id=ctx.workspace_id, kind="playbook", text=text, source_run_id=ctx.run.id,
                    data={"request": ctx.run.request, "plan_outline": outline, "title": title})))
        if saved:
            ctx.log(f"Saved {len(saved)} memory item(s)", agent="planner",
                    items=[{"id": m.id, "kind": m.kind, "text": m.text} for m in saved])

    def _clean_success(self, ctx: RunContext) -> bool:
        """A playbook is only worth saving when the plan worked as planned: every goal verified, nothing
        blocked or rejected, nothing needed a human to rescue it."""
        plan = ctx.plan
        if any(n.kind is NodeKind.GOAL and n.status is not NodeStatus.SUCCEEDED for n in plan.nodes.values()):
            return False
        if any(excused(ctx, n) for n in plan.nodes.values() if n.kind is NodeKind.GOAL):
            return False
        return not any(n.gate is not None and n.gate.verdict is Verdict.BLOCK for n in plan.actions())

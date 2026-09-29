"""Proof-of-done (pain point 3: false "done").

Each goal is checked bottom-up: Jev ``verify`` judges every success criterion against *observed* evidence (what
the tools returned and which effects the ledger shows as applied; simulated previews do not count). A failed
goal gets a Muse reflection and a re-plan of its subtree (completed steps are kept), bounded by
``budget.max_replans``. The new steps then go through shadow → gate → approval like any other write.

A goal that failed because a human rejected a step, or the policy blocked one, is "not done on purpose": it is
reported, not re-planned (re-planning would just try the refused action again).
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from ..core.models import ErrorKind, NodeKind, NodeStatus, PlanNode, Verification
from .binding import is_write
from .context import RunContext, Services
from .errors import PlannerUnavailable
from .events import current_node
from .plan import goals_bottom_up, subtree_actions
from .planner import Planner
from .util import compact_to_budget, shape


class VerifyOutcome(str, Enum):
    DONE = "done"
    REPLANNED = "replanned"


def excused(ctx: RunContext, goal: PlanNode) -> bool:
    """True when the goal's shortfall comes from a human or policy decision (blocked / rejected steps)."""
    for aid in subtree_actions(ctx.plan, goal.id):
        node = ctx.plan.nodes[aid]
        if node.status is NodeStatus.BLOCKED:
            return True
        if node.status is NodeStatus.SKIPPED and node.result and node.result.error \
                and node.result.error.kind is ErrorKind.PERMISSION:
            return True
    return False


def unverified_goals(ctx: RunContext) -> list[PlanNode]:
    return [ctx.plan.nodes[g] for g in goals_bottom_up(ctx.plan)
            if ctx.plan.nodes[g].status is not NodeStatus.SUCCEEDED and not excused(ctx, ctx.plan.nodes[g])]


class Verifier:
    def __init__(self, svc: Services, planner: Planner) -> None:
        self.svc = svc
        self.planner = planner

    def evidence(self, ctx: RunContext, goal: PlanNode, *, privileged: bool = False) -> dict[str, Any]:
        """Observed results for a goal's subtree, most probative first.

        * ``writes``: every write step with its ledger status and the content it actually delivered (the effect
          preview: an email body, a doc's Markdown). A created doc's *output* is only its id, so without the
          preview a verifier cannot tell a real summary doc from an empty shell.
        * ``subgoal_results``: children already verified; proof composes bottom-up instead of re-judging the
          whole tree from raw data.
        * ``reads``: what was read, compressed hardest.
        Jev (a classifier) gets values; Muse's reflection (``privileged=True``) gets untrusted data as shapes."""
        prov = ctx.provenance()
        effects = {e.node_id: e for e in ctx.svc.store.effects(ctx.run.id)}
        writes: list[dict[str, Any]] = []
        reads: list[dict[str, Any]] = []
        for aid in subtree_actions(ctx.plan, goal.id):
            node = ctx.plan.nodes[aid]
            entry: dict[str, Any] = {"step": aid, "title": node.title, "tool": node.tool,
                                     "status": node.status.value}
            if node.result is not None and node.result.error is not None:
                entry["error"] = node.result.error.message
            if node.status is NodeStatus.BLOCKED and node.gate is not None:
                entry["blocked_because"] = node.gate.reasons[:3]
            effect = effects.get(aid)
            if is_write(ctx.spec(node.tool)):
                if effect is not None:
                    content = effect.preview
                    if privileged and prov.args_tainted(aid):
                        content = shape(content)
                    entry["effect"] = {"applied": effect.status == "applied", "status": effect.status,
                                       "summary": effect.summary, "target": effect.target, "content": content}
                if node.result is not None and node.result.ok and node.status is NodeStatus.SUCCEEDED:
                    entry["output"] = node.result.output
                writes.append(entry)
            else:
                if node.result is not None and node.result.ok and node.status is NodeStatus.SUCCEEDED:
                    entry["output"] = self.planner.privileged_view(ctx, aid, node.result.output, prov) \
                        if privileged else node.result.output
                reads.append(entry)
        subgoals = [{"goal": c.title, "passed": c.verification.passed,
                     "checks": [{"criterion": k.criterion, "p": round(k.p, 2)} for k in c.verification.checks]}
                    for c in ctx.plan.children_of(goal.id) if c.kind is NodeKind.GOAL and c.verification]
        budget = self.svc.config.evidence_chars
        return {
            "request": ctx.request_text(),
            "writes": compact_to_budget(writes, int(budget * 0.55)),
            "subgoal_results": subgoals,
            "reads": compact_to_budget(reads, int(budget * 0.35)),
        }

    async def _verify(self, ctx: RunContext, goal: PlanNode) -> Verification:
        if not goal.success_criteria:
            return Verification(passed=True, checks=[], model="none: no criteria")
        token = current_node.set(goal.id)
        try:
            return await self.svc.judge.verify(goal=goal.title, criteria=goal.success_criteria,
                                               evidence=self.evidence(ctx, goal))
        except Exception as e:  # noqa: BLE001
            return Verification(passed=False, checks=[], model=f"unavailable:{type(e).__name__}")
        finally:
            current_node.reset(token)

    async def run(self, ctx: RunContext) -> VerifyOutcome:
        plan = ctx.plan
        for gid in goals_bottom_up(plan):
            goal = plan.nodes.get(gid)
            if goal is None or goal.kind is not NodeKind.GOAL:
                continue
            if goal.status is NodeStatus.SUCCEEDED or (goal.status is NodeStatus.FAILED and goal.verification):
                continue  # already judged in an earlier pass
            ctx.checkpoint()
            verification = await self._verify(ctx, goal)
            goal.verification = verification
            ctx.emit("node.verified", agent="evaluator", node_id=gid, verification=verification)
            if verification.passed:
                ctx.set_node_status(goal, NodeStatus.SUCCEEDED, agent="evaluator")
                ctx.persist()
                continue
            ctx.set_node_status(goal, NodeStatus.FAILED, agent="evaluator")
            ctx.persist()
            if excused(ctx, goal):
                ctx.log(f"'{goal.title}' is incomplete because a step was blocked or rejected; not re-planning",
                        agent="evaluator", node_id=gid)
                continue
            if not ctx.can_replan():
                continue
            try:
                replanned = await self._replan(ctx, goal, verification)
            except PlannerUnavailable:
                # Muse is down: leave the goal unjudged so resuming the run re-verifies and re-plans it.
                goal.verification = None
                ctx.set_node_status(goal, NodeStatus.RUNNING, agent="evaluator")
                ctx.persist()
                raise
            if replanned:
                return VerifyOutcome.REPLANNED
        return VerifyOutcome.DONE

    async def _replan(self, ctx: RunContext, goal: PlanNode, verification: Verification) -> bool:
        evidence = self.evidence(ctx, goal, privileged=True)
        reflection = await self.planner.reflect(ctx, goal, verification, evidence)
        ctx.emit("reflection", agent="planner", node_id=goal.id, payload={"text": reflection, "agent": "planner"})
        failed = [c.criterion for c in verification.checks if not c.passed] or goal.success_criteria
        reason = "Success criteria not met by the observed results: " + "; ".join(failed)
        revision = await self.planner.replan_goal(ctx, goal.id, reason=reason, reflection=reflection)
        return revision is not None

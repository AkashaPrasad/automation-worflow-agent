"""Muse, the System 2 side: intent, plans, re-plans, reflection, argument repair, summaries and memories.

Every call goes through ``Planner._call`` which enforces the run budget *before* spending, picks the reasoning
effort per purpose (high for planning, medium for replan/reflection, low for drafting/repair) and converts
transport failures into ``PlannerUnavailable`` so the run fails with a readable reason instead of a traceback.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..core.models import (
    EffectClass,
    Intent,
    MemoryItem,
    NodeKind,
    NodeStatus,
    PlanNode,
    ToolSpec,
    Verification,
)
from . import prompts
from .binding import Mode, available_outputs
from .context import RunContext, Services
from .errors import BudgetExceeded, PlanInvalid, PlannerUnavailable, RunInterrupted
from .plan import DraftPlan, DraftReplan, PlanBuilder, Revision, descendants, plan_outline, upstream
from .templates import referenced_nodes
from .timeutil import date_context, now_for
from .util import compact, jsonable, redact_quoted, shape, truncate

_TRANSIENT_HINTS = ("after retries", "429", "500", "502", "503", "504", "529", "overloaded", "timeout",
                    "timed out", "connection")


@dataclass
class MemoryDraft:
    facts: list[str] = field(default_factory=list)
    preferences: list[str] = field(default_factory=list)
    people: list[str] = field(default_factory=list)
    playbook_title: str = ""
    playbook_outline: list[str] = field(default_factory=list)


class Planner:
    def __init__(self, svc: Services) -> None:
        self.svc = svc

    # -- plumbing --------------------------------------------------------------
    async def _call(self, ctx: RunContext, purpose: prompts.Purpose, system: str, user: str,
                    schema: dict[str, Any] | None = None) -> Any:
        ctx.check_budget()
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        try:
            result = await self.svc.llm.complete(messages, purpose=purpose.name, schema=schema,
                                                 effort=purpose.effort, max_tokens=purpose.max_tokens)
        except (RunInterrupted, BudgetExceeded):
            raise
        except Exception as e:
            text = str(e)
            transient = any(k in text.lower() for k in _TRANSIENT_HINTS)
            raise PlannerUnavailable(f"Muse call '{purpose.name}' failed: {truncate(text, 300)}",
                                     transient=transient) from e
        if schema is None:
            return (result.text or "").strip()
        data = result.data
        if data is None and result.text:
            try:
                data = json.loads(result.text)
            except (TypeError, ValueError):
                data = None
        if not isinstance(data, dict):
            raise PlannerUnavailable(f"Muse returned no JSON object for '{purpose.name}'")
        return data

    def _date_ctx(self, ctx: RunContext) -> str:
        return date_context(now_for(ctx.profile()), ctx.run.intent.time_refs if ctx.run.intent else [])

    def builder(self, ctx: RunContext) -> PlanBuilder:
        return PlanBuilder(ctx.spec)

    # -- intake ------------------------------------------------------------------
    async def parse_intent(self, ctx: RunContext) -> Intent:
        apps = sorted({s.app for s in ctx.specs().values()})
        user = prompts.intent_brief(request=ctx.run.request, clarification=ctx.run.clarification,
                                    profile=ctx.profile(), date_ctx=date_context(now_for(ctx.profile())), apps=apps)
        data = await self._call(ctx, prompts.INTENT, prompts.INTENT_SYSTEM, user, prompts.INTENT_SCHEMA)
        try:
            intent = Intent.model_validate({k: v for k, v in data.items() if k in Intent.model_fields})
        except Exception:  # noqa: BLE001
            intent = Intent(goal=ctx.run.request)
        if not intent.goal.strip():
            intent.goal = truncate(ctx.run.request, 200)
        return intent

    async def phrase_clarification(self, ctx: RunContext, missing: str | None) -> str:
        user = prompts.clarify_brief(request=ctx.run.request, intent=jsonable(ctx.intent()), missing=missing)
        try:
            data = await self._call(ctx, prompts.CLARIFY, prompts.CLARIFY_SYSTEM, user, prompts.CLARIFY_SCHEMA)
            question = str(data.get("question") or "").strip()
        except PlannerUnavailable:
            question = ""
        return question or f"Before I start: could you tell me more about {missing or 'what you need'}?"

    # -- planning -----------------------------------------------------------------
    def _planning_brief(self, ctx: RunContext, memories: list[MemoryItem], scores: dict[str, float]) -> str:
        facts = [(m.kind, m.text, scores.get(m.id, 0.0)) for m in memories if m.kind != "playbook"]
        playbooks = [m.data for m in memories if m.kind == "playbook" and m.data]
        return prompts.planning_brief(
            request=ctx.run.request, clarification=ctx.run.clarification, intent=jsonable(ctx.intent()),
            profile=ctx.profile(), date_ctx=self._date_ctx(ctx),
            catalogue=prompts.tool_catalogue(list(ctx.specs().values())),
            memories=prompts.memory_block(facts, playbooks))

    async def draft_plan(self, ctx: RunContext, memories: list[MemoryItem], scores: dict[str, float]):
        """Muse drafts; code builds and validates; one repair round with the exact errors."""
        brief = self._planning_brief(ctx, memories, scores)
        builder = self.builder(ctx)
        data = await self._call(ctx, prompts.PLAN, prompts.PLANNER_SYSTEM, brief, prompts.PLAN_SCHEMA)
        draft = DraftPlan.model_validate(data)
        plan, warnings = builder.from_draft(draft)
        errors = builder.validate(plan)
        if errors:
            ctx.log("Plan failed validation; asking Muse to repair it", level="warning", agent="planner",
                    errors=errors[:20])
            repair = prompts.plan_repair_brief(original_brief=brief, draft=data, errors=errors)
            data = await self._call(ctx, prompts.PLAN_REPAIR, prompts.PLANNER_SYSTEM, repair, prompts.PLAN_SCHEMA)
            draft = DraftPlan.model_validate(data)
            plan, warnings = builder.from_draft(draft)
            errors = builder.validate(plan)
            if errors:
                raise PlanInvalid(errors)
        if warnings:
            ctx.log("Plan normalised by code", agent="planner", warnings=warnings[:20])
        return plan, (draft.title or draft.goal)

    # -- re-planning ----------------------------------------------------------------
    @staticmethod
    def privileged_view(ctx: RunContext, node_id: str, output: Any, prov=None) -> Any:
        """What the planner lane (Muse deciding actions) may see of a step's output: values when the output is
        trusted, only its *shape* when it derives from untrusted content. The privileged planner never reads
        external text, even while repairing or re-planning (CaMeL); it points at it with templates instead."""
        prov = prov or ctx.provenance()
        if prov.output_tainted(node_id):
            return {"untrusted_shape": shape(output)}
        return compact(output, max_str=240, max_list=5)

    def _node_view(self, ctx: RunContext, node: PlanNode, *, with_output: bool) -> dict[str, Any]:
        view: dict[str, Any] = {"id": node.id, "title": node.title, "status": node.status.value}
        if node.kind is NodeKind.ACTION:
            view.update(tool=node.tool, args=node.args)
            if with_output and node.result is not None:
                if node.result.ok:
                    view["output"] = self.privileged_view(ctx, node.id, node.result.output)
                elif node.result.error:
                    message = node.result.error.message
                    if ctx.provenance().args_tainted(node.id):
                        message = redact_quoted(message)
                    view["error"] = f"{node.result.error.kind.value}: {message}"
            if node.status is NodeStatus.BLOCKED and node.gate:
                view["blocked_because"] = node.gate.reasons[:3]
        return view

    async def replan_goal(self, ctx: RunContext, goal_id: str, *, reason: str, reflection: str = "") -> Revision | None:
        """Rebuild the unfinished part of ``goal_id``. Returns None when Muse finds nothing more to do or the
        repaired draft still fails validation (the caller then escalates to a human or gives up)."""
        plan = ctx.plan
        goal = plan.nodes[goal_id]
        sub = set(descendants(plan, goal_id))
        kept = [self._node_view(ctx, plan.nodes[n], with_output=True) for n in sub
                if plan.nodes[n].kind is NodeKind.ACTION and plan.nodes[n].status in (NodeStatus.SUCCEEDED,
                                                                                         NodeStatus.RUNNING)]
        removed = [self._node_view(ctx, plan.nodes[n], with_output=True) for n in sub
                   if plan.nodes[n].kind is NodeKind.ACTION
                   and plan.nodes[n].status not in (NodeStatus.SUCCEEDED, NodeStatus.RUNNING)]
        removed_ids = {r["id"] for r in removed}
        outputs = available_outputs(plan, Mode.SHADOW)
        outside = [self._node_view(ctx, n, with_output=n.id in outputs) for n in plan.actions()
                   if n.id not in sub][:20]
        external_refs = [f"{n.id} uses {', '.join(sorted(referenced_nodes(n.args) & removed_ids))}"
                         for n in plan.actions() if n.id not in sub and referenced_nodes(n.args) & removed_ids]
        brief = prompts.replan_brief(
            request=ctx.run.request, clarification=ctx.run.clarification, profile=ctx.profile(),
            date_ctx=self._date_ctx(ctx),
            goal={"id": goal.id, "title": goal.title, "success_criteria": goal.success_criteria},
            reason=reason, reflection=reflection, kept=kept, removed=removed, outside=outside,
            external_refs=external_refs, catalogue=prompts.tool_catalogue(list(ctx.specs().values())))
        builder = self.builder(ctx)
        data = await self._call(ctx, prompts.REPLAN, prompts.REPLAN_SYSTEM, brief, prompts.REPLAN_SCHEMA)
        draft = DraftReplan.model_validate(data)
        if draft.empty:
            return None
        errors = self._trial(builder, ctx, goal_id, draft)
        if errors:
            repair = prompts.plan_repair_brief(original_brief=brief, draft=data, errors=errors)
            data = await self._call(ctx, prompts.PLAN_REPAIR, prompts.REPLAN_SYSTEM, repair, prompts.REPLAN_SCHEMA)
            draft = DraftReplan.model_validate(data)
            if draft.empty or self._trial(builder, ctx, goal_id, draft):
                ctx.log("Re-plan rejected by validation", level="warning", agent="planner", node_id=goal_id,
                        errors=errors[:10])
                return None
        revision = builder.apply_replan(plan, goal_id, draft)  # identical to the validated trial
        ctx.run.metrics.replans += 1
        ctx.persist()
        ctx.emit("plan.revised", agent="planner", node_id=goal_id, plan=plan, reason=draft.reason or reason,
                 replaced=revision.removed, added=revision.added, rewired=revision.rewired,
                 revision=revision.revision)
        return revision

    @staticmethod
    def _trial(builder: PlanBuilder, ctx: RunContext, goal_id: str, draft: DraftReplan) -> list[str]:
        scratch = ctx.plan.model_copy(deep=True)
        revision = builder.apply_replan(scratch, goal_id, draft)
        return builder.validate(scratch, scope=revision.added)

    async def reflect(self, ctx: RunContext, goal: PlanNode, verification: Verification,
                      evidence: dict[str, Any]) -> str:
        user = prompts.reflect_brief(
            goal={"id": goal.id, "title": goal.title, "success_criteria": goal.success_criteria},
            verification=jsonable(verification), evidence=evidence)
        data = await self._call(ctx, prompts.REFLECT, prompts.REFLECT_SYSTEM, user, prompts.REFLECT_SCHEMA)
        diagnosis = str(data.get("diagnosis") or "").strip()
        change = str(data.get("change") or "").strip()
        return (diagnosis + (f" Next: {change}" if change else "")).strip() or "Verification failed."

    # -- executor-side repair ----------------------------------------------------------
    async def repair_args(self, ctx: RunContext, node: PlanNode, spec: ToolSpec, error: str) -> dict[str, Any] | None:
        """New args for the same step, validated in code (references must be to steps with outputs that the
        step may depend on). Returns None when Muse cannot produce valid, different args."""
        plan = ctx.plan
        outputs = available_outputs(plan, Mode.SHADOW)
        # Only upstream steps: referencing anything downstream would create a cycle.
        allowed = {d for d in upstream(plan, node.id) if d in outputs}
        prov = ctx.provenance()
        available = [{"id": d, "title": plan.nodes[d].title, "tool": plan.nodes[d].tool,
                      "output": self.privileged_view(ctx, d, outputs[d], prov)} for d in sorted(allowed)][:15]
        resolved = None
        if node.resolved_args is not None:  # values that came from untrusted steps are shown as shapes
            resolved = {k: shape(v) if any(prov.output_tainted(r) for r in referenced_nodes(node.args.get(k)))
                        else v for k, v in node.resolved_args.items()}
        user = prompts.repair_args_brief(spec=spec, title=node.title, rationale=node.rationale, args=node.args,
                                         resolved=resolved, error=error, available=available)
        data = await self._call(ctx, prompts.REPAIR_ARGS, prompts.REPAIR_ARGS_SYSTEM, user, prompts.REPAIR_ARGS_SCHEMA)
        args = data.get("args")
        if not isinstance(args, dict) or not args:
            return None
        refs = referenced_nodes(args)
        if refs - allowed:
            ctx.log(f"Repaired args reference unavailable steps {sorted(refs - allowed)}", level="warning",
                    agent="executor", node_id=node.id)
            return None
        candidate = node.model_copy(update={"args": args})
        if self.builder(ctx).validate_action(plan, candidate):
            return None
        return args

    # -- completion ------------------------------------------------------------------
    async def summarize(self, ctx: RunContext, *, outcome: str) -> str:
        plan = ctx.plan
        steps = []
        for n in plan.actions():
            entry: dict[str, Any] = {"id": n.id, "title": n.title, "tool": n.tool, "status": n.status.value}
            if n.result is not None and n.result.ok and n.status is NodeStatus.SUCCEEDED:
                entry["output"] = compact(n.result.output, max_str=200, max_list=4)
            if n.result is not None and n.result.error:
                entry["error"] = n.result.error.message
            if n.gate is not None and n.gate.verdict.value != "auto":
                entry["gate"] = {"verdict": n.gate.verdict.value, "reasons": n.gate.reasons[:2]}
            steps.append(entry)
        effects = [{"summary": e.summary, "status": e.status, "target": e.target}
                   for e in ctx.svc.store.effects(ctx.run.id) if e.status != "simulated"]
        verification = [{"goal": g.title, "passed": g.verification.passed,
                         "checks": [c.model_dump() for c in g.verification.checks]}
                        for g in plan.nodes.values() if g.kind is NodeKind.GOAL and g.verification]
        user = prompts.summary_brief(request=ctx.request_text(), outcome=outcome, steps=steps, effects=effects,
                                     verification=verification)
        return await self._call(ctx, prompts.SUMMARY, prompts.SUMMARY_SYSTEM, user)

    async def extract_memories(self, ctx: RunContext) -> MemoryDraft:
        """Only trusted material goes in: the user's words, the plan, what Adjutant did, and outputs of
        *untainted* steps. A hostile email therefore cannot plant a durable "preference"."""
        plan = ctx.plan
        prov = ctx.provenance()
        trusted = [{"step": n.title, "output": compact(n.result.output, max_str=200, max_list=4)}
                   for n in plan.actions()
                   if n.status is NodeStatus.SUCCEEDED and n.result and n.result.ok and not prov.output_tainted(n.id)
                   and (s := ctx.spec(n.tool)) is not None and s.effect is EffectClass.READ][:10]
        effects = [e.summary for e in ctx.svc.store.effects(ctx.run.id) if e.status == "applied"]
        user = prompts.memory_brief(request=ctx.run.request, clarification=ctx.run.clarification,
                                    intent=jsonable(ctx.intent()), outline=plan_outline(plan), effects=effects,
                                    trusted_outputs=trusted)
        data = await self._call(ctx, prompts.MEMORY, prompts.MEMORY_SYSTEM, user, prompts.MEMORY_SCHEMA)

        def strs(key: str) -> list[str]:
            v = data.get(key)
            return [str(x).strip() for x in v if str(x).strip()][:4] if isinstance(v, list) else []

        return MemoryDraft(facts=strs("facts"), preferences=strs("preferences"), people=strs("people"),
                           playbook_title=str(data.get("playbook_title") or "").strip(),
                           playbook_outline=[str(x) for x in (data.get("playbook_outline") or [])][:10]
                           if isinstance(data.get("playbook_outline"), list) else [])

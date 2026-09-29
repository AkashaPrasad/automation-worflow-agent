"""The gate and the plan-diff review.

After the shadow run every simulated write is gated by Jev (``judge.gate_action``; one request, parallel
questions) and turned into AUTO / ASK / BLOCK by code policy (app/judgment/policy.py). All ASK items are batched
into ONE plan-diff approval (pain point 2: approval fatigue), BLOCK items are shown but cannot be approved, and
approvals bind to the step's binding hash, never to the step id alone.

The engine adds one defence-in-depth rule of its own: untrusted texts are pre-scanned once per run with
``judge.scan_untrusted``. The scores order and annotate the ``untrusted_context`` Jev sees, and a tainted write
whose sources pre-scan as likely injection is escalated AUTO → ASK. The engine only ever escalates; it never
downgrades a verdict.
"""
from __future__ import annotations

import asyncio
from enum import Enum
from typing import Any, Literal

from ..core.models import (
    Approval,
    ApprovalItem,
    EffectClass,
    ErrorKind,
    GateDecision,
    NodeStatus,
    PlanNode,
    ToolSpec,
    Verdict,
    args_hash,
    now_ms,
)
from .binding import BoundArgs, Mode, bind, edit_symbols, is_write
from .context import RunContext, Services, action_nodes
from .errors import BindingError
from .events import current_node
from .plan import dead_dependency
from .recovery import skip
from .taint import is_untrusted_source, untrusted_texts
from .templates import resymbolize
from .util import compact, truncate

WaitingKind = Literal["gate", "failure", "unknown_effect"]


class Clearance(str, Enum):
    CLEARED = "cleared"  # may execute now
    WAITING = "waiting"  # needs a human
    BLOCKED = "blocked"  # refused by policy


def waiting_kind(node: PlanNode) -> WaitingKind:
    """Why a step is awaiting a human, derived from node state alone (so approvals can be rebuilt after a
    restart without any extra bookkeeping)."""
    result = node.result
    if result is not None and result.effect is not None and result.effect.status == "unknown":
        return "unknown_effect"
    if result is not None and result.ok and result.simulated:
        return "gate"
    return "failure"


class Gatekeeper:
    def __init__(self, svc: Services) -> None:
        self.svc = svc
        self.cfg = svc.config

    # -- injection pre-scan (once per text per run) ------------------------------
    def _untrusted_corpus(self, ctx: RunContext) -> dict[str, str]:
        texts: dict[str, str] = {}
        for node in action_nodes(ctx.plan):
            if node.result is not None and node.result.ok and is_untrusted_source(ctx.spec(node.tool)):
                texts.update(untrusted_texts(node.id, node.result.output, limit=self.cfg.scan_text_chars))
        return texts

    async def prescan(self, ctx: RunContext) -> None:
        fresh = {k: v for k, v in self._untrusted_corpus(ctx).items() if k not in ctx.injection_scan}
        if not fresh:
            return
        batch = dict(list(fresh.items())[: self.cfg.max_scan_texts])
        try:
            scores = await self.svc.judge.scan_untrusted(batch)
        except Exception as e:  # noqa: BLE001
            ctx.log(f"Injection pre-scan unavailable ({type(e).__name__}); gating continues without it",
                    level="warning", agent="guardian")
            scores = {}
        for text_id in batch:
            ctx.injection_scan[text_id] = float(scores.get(text_id, 0.0) or 0.0)
        flagged = sorted(((tid, p) for tid, p in ctx.injection_scan.items() if tid in batch and p >= 0.5),
                         key=lambda x: -x[1])
        ctx.log(f"Scanned {len(batch)} untrusted text(s); {len(flagged)} look like prompt injection",
                level="warning" if flagged else "info", agent="guardian",
                flagged=[{"id": tid, "p": round(p, 3), "excerpt": truncate(batch[tid], 160)} for tid, p in flagged])

    def untrusted_context(self, ctx: RunContext, node: PlanNode) -> tuple[list[str], float]:
        """The external texts behind a write, most suspicious first, annotated with their pre-scan score."""
        plan = ctx.plan
        prov = ctx.provenance()
        wanted = prov.arg_texts(node.id)
        texts: dict[str, str] = {}
        for src in prov.arg_sources(node.id):
            src_node = plan.nodes.get(src)
            if src_node is not None and src_node.result is not None and src_node.result.ok:
                texts.update({tid: text for tid, text in untrusted_texts(
                    src, src_node.result.output, limit=self.cfg.scan_text_chars).items() if tid in wanted})
        ranked = sorted(texts.items(), key=lambda kv: -ctx.injection_scan.get(kv[0], 0.0))
        context = [f"[{tid} | pre-scan injection p={ctx.injection_scan.get(tid, 0.0):.2f}]\n"
                   f"{truncate(text, self.cfg.gate_context_chars)}"
                   for tid, text in ranked[: self.cfg.gate_context_texts]]
        top = max((ctx.injection_scan.get(tid, 0.0) for tid in texts), default=0.0)
        return context, top

    # -- gating --------------------------------------------------------------------
    async def gate(self, ctx: RunContext, node: PlanNode, spec: ToolSpec, bound: BoundArgs,
                   preview: dict[str, Any]) -> GateDecision:
        token = current_node.set(node.id)
        try:
            profile = ctx.profile()
            context, top_injection = self.untrusted_context(ctx, node)
            try:
                decision = await self.svc.judge.gate_action(
                    user_request=ctx.request_text(), intent=ctx.intent(), tool=spec, args=bound.actual,
                    preview=preview, untrusted_context=context, tainted=node.tainted,
                    tainted_args=list(node.tainted_args), autonomy=ctx.run.autonomy,
                    known_contacts=_known_contacts(profile),
                    internal_domain=str(profile.get("internal_domain") or ""))
            except Exception as e:  # noqa: BLE001
                decision = GateDecision(verdict=Verdict.ASK, reasons=["judge unavailable, failing safe"],
                                        autonomy=ctx.run.autonomy.value, model=f"engine-failsafe:{type(e).__name__}")
            decision = self._escalate(decision.model_copy(update={"args_hash": bound.hash}), node, top_injection)
        finally:
            current_node.reset(token)
        node.gate = decision
        ctx.emit("node.gated", agent="guardian", node_id=node.id, gate=decision)
        return decision

    def _escalate(self, decision: GateDecision, node: PlanNode, top_injection: float) -> GateDecision:
        if not node.tainted:
            return decision
        signals = {**decision.signals, "injection_prescan": round(top_injection, 3)}
        if decision.verdict is Verdict.AUTO and top_injection >= self.cfg.prescan_escalation:
            return decision.model_copy(update={
                "verdict": Verdict.ASK, "signals": signals,
                "reasons": [*decision.reasons, (f"Content that flowed into this action pre-scanned as likely prompt "
                                                f"injection (p={top_injection:.2f}); a human should confirm")]})
        return decision.model_copy(update={"signals": signals})

    def _apply_verdict(self, ctx: RunContext, node: PlanNode, decision: GateDecision, *, fresh: bool) -> Clearance:
        if decision.verdict is Verdict.BLOCK:
            ctx.set_node_status(node, NodeStatus.BLOCKED, agent="guardian")
            return Clearance.BLOCKED
        if decision.verdict is Verdict.AUTO:
            if fresh:
                ctx.run.metrics.auto_approved += 1
            ctx.set_node_status(node, NodeStatus.READY, agent="guardian")
            return Clearance.CLEARED
        if ctx.is_approved(node.id, decision.args_hash):
            # The user already approved exactly these arguments (e.g. an edit they approved): authoritative
            # unless policy BLOCKs, which it did not.
            ctx.set_node_status(node, NodeStatus.READY, agent="guardian")
            return Clearance.CLEARED
        ctx.set_node_status(node, NodeStatus.AWAITING_APPROVAL, agent="guardian")
        return Clearance.WAITING

    async def gate_simulated(self, ctx: RunContext) -> None:
        """Gate every simulated write whose gate is missing or stale, in parallel."""
        pending = [n for n in action_nodes(ctx.plan)
                   if n.status is NodeStatus.SIMULATED and is_write(ctx.spec(n.tool))]
        if not pending:
            return
        await asyncio.gather(*(self._gate_simulated_one(ctx, n) for n in pending))
        ctx.persist()

    async def _gate_simulated_one(self, ctx: RunContext, node: PlanNode) -> None:
        spec = ctx.spec(node.tool)
        assert spec is not None
        try:
            bound = bind(ctx.plan, node, ctx.spec, Mode.SHADOW)
        except BindingError:
            bound = BoundArgs(actual=node.resolved_args or node.args,
                              hash=args_hash(node.tool or "", node.resolved_args or node.args))
        fresh = node.gate is None or node.gate.args_hash != bound.hash
        if fresh:
            preview = node.result.effect.preview if node.result and node.result.effect else {}
            await self.gate(ctx, node, spec, bound, preview)
        assert node.gate is not None
        self._apply_verdict(ctx, node, node.gate, fresh=fresh)

    async def regate(self, ctx: RunContext, node: PlanNode, spec: ToolSpec, bound: BoundArgs,
                     preview: dict[str, Any]) -> Clearance:
        """Commit-time gate for a write whose resolved args no longer match what was gated/approved."""
        ctx.log("Arguments changed since they were gated; re-gating before execution", agent="guardian",
                node_id=node.id, previous_hash=node.gate.args_hash if node.gate else None, new_hash=bound.hash)
        decision = await self.gate(ctx, node, spec, bound, preview)
        return self._apply_verdict(ctx, node, decision, fresh=True)

    def clearance(self, ctx: RunContext, node: PlanNode, bound: BoundArgs) -> Clearance | None:
        """Is the existing gate still valid for these exact args? None → it must be re-gated."""
        gate = node.gate
        if gate is None or gate.args_hash != bound.hash:
            return None
        if gate.verdict is Verdict.BLOCK:
            return Clearance.BLOCKED
        if gate.verdict is Verdict.AUTO or ctx.is_approved(node.id, bound.hash):
            return Clearance.CLEARED
        return None

    # -- approvals -------------------------------------------------------------------
    def cascade_skips(self, ctx: RunContext) -> None:
        """Skip every unfinished step that can no longer run because a dependency is dead."""
        changed = True
        while changed:
            changed = False
            for node in action_nodes(ctx.plan):
                if node.status not in (NodeStatus.PENDING, NodeStatus.READY, NodeStatus.SIMULATED,
                                       NodeStatus.AWAITING_APPROVAL):
                    continue
                dead = dead_dependency(ctx.plan, node)
                if dead is not None:
                    skip(ctx, node, f"Skipped: depends on {dead.dep_id} ({dead.status.value})", dead.kind)
                    changed = True

    def build_approval(self, ctx: RunContext) -> Approval | None:
        waiting = [n for n in action_nodes(ctx.plan) if n.status is NodeStatus.AWAITING_APPROVAL]
        if not waiting:
            return None
        items = [self._item(ctx, n) for n in waiting]
        blocked = [n for n in action_nodes(ctx.plan)
                   if n.status is NodeStatus.BLOCKED and n.gate is not None and n.result is not None]
        items += [self._item(ctx, n, decision="rejected") for n in blocked]
        kind = "plan_diff" if any(waiting_kind(n) == "gate" for n in waiting) else "action"
        ctx.run.metrics.approvals_requested += len(waiting)
        return Approval(run_id=ctx.run.id, kind=kind, items=items)

    def _item(self, ctx: RunContext, node: PlanNode, decision: str = "pending") -> ApprovalItem:
        spec = ctx.spec(node.tool)
        effect_class = spec.effect if spec else EffectClass.WRITE_IRREVERSIBLE
        args = node.resolved_args if node.resolved_args is not None else node.args
        kind = waiting_kind(node) if node.status is NodeStatus.AWAITING_APPROVAL else "gate"
        result = node.result
        effect = result.effect if result is not None else None
        if kind == "gate" and node.gate is not None:
            return ApprovalItem(node_id=node.id, tool=node.tool or "", effect=effect_class, args=args,
                                summary=effect.summary if effect and effect.summary else node.title,
                                args_hash=node.gate.args_hash, preview=effect.preview if effect else {},
                                gate=node.gate, decision=decision)  # type: ignore[arg-type]
        h = node.gate.args_hash if node.gate else args_hash(node.tool or "", args)
        if kind == "unknown_effect" and effect is not None:
            gate = GateDecision(verdict=Verdict.ASK, autonomy=ctx.run.autonomy.value, model="engine:reconcile",
                                args_hash=effect.args_hash or h, reasons=[
                                    ("Adjutant restarted while this action was in flight, and reading the world "
                                     "back could not confirm whether it happened."),
                                    ("Approve to retry with the same idempotency key (a provider that honours it "
                                     "will not act twice); reject to leave it as it is.")])
            return ApprovalItem(node_id=node.id, tool=node.tool or "", effect=effect_class, args=args,
                                summary=f"Unconfirmed: {effect.summary}", args_hash=gate.args_hash,
                                preview=effect.preview, gate=gate, decision=decision)  # type: ignore[arg-type]
        error = result.error if result is not None else None
        last = node.recovery[-1] if node.recovery else None
        message = error.message if error else "the step failed"
        reasons = [f"Step failed ({error.kind.value if error else 'unknown'}): {truncate(message, 300)}"]
        if last is not None:
            reasons.append(f"Recovery: {last.strategy.value} - {last.note}")
        reasons.append("Approve to retry (you may edit the arguments); reject to skip this step.")
        gate = GateDecision(verdict=Verdict.ASK, autonomy=ctx.run.autonomy.value, model="engine:recovery",
                            args_hash=h, reasons=reasons)
        return ApprovalItem(node_id=node.id, tool=node.tool or "", effect=effect_class, args=args,
                            summary=f"Retry '{node.title}'? {truncate(message, 140)}", args_hash=h,
                            preview={"error": message, "attempts": node.attempts, "args": compact(args)},
                            gate=gate, decision=decision)  # type: ignore[arg-type]

    def apply_decisions(self, ctx: RunContext, approval: Approval, decisions: dict[str, str],
                        edits: dict[str, dict[str, Any]]) -> None:
        """Apply the human's review to the plan. Unlisted items count as rejected (fail safe); BLOCK items can
        never be approved; an edit is an approval of the edited arguments, which get a new binding hash and are
        re-simulated and re-gated before they run."""
        plan = ctx.plan
        for item in approval.items:
            node = plan.nodes.get(item.node_id)
            if item.gate.verdict is Verdict.BLOCK or node is None or node.status is not NodeStatus.AWAITING_APPROVAL:
                if item.decision == "pending":
                    item.decision = "rejected"
                continue
            kind = waiting_kind(node)
            edit = edits.get(item.node_id) or {}
            choice = _normalize_decision(decisions.get(item.node_id), bool(edit))
            if choice == "rejected":
                item.decision = "rejected"
                reason = "Rejected by the user in review" if kind != "unknown_effect" else \
                    "The user chose not to retry an action whose outcome is unknown"
                skip(ctx, node, reason, ErrorKind.PERMISSION)
                continue
            item.decision = "edited" if edit else "approved"
            if edit:
                self._apply_edit(ctx, node, edit, item)
            if kind == "gate":
                if edit:
                    ctx.set_node_status(node, NodeStatus.SIMULATED)  # gate is stale: commit re-simulates + re-gates
                else:
                    ctx.approve_hash(node.id, item.args_hash)
                    ctx.set_node_status(node, NodeStatus.READY, agent="system")
            elif kind == "unknown_effect":
                ctx.approve_hash(node.id, item.args_hash)
                ctx.set_node_status(node, NodeStatus.READY, agent="system")
            else:  # failure: the human asked for another attempt
                node.attempts = 0
                for sig in [s for s in ctx.call_counts if s[0] == node.tool]:
                    ctx.call_counts.pop(sig, None)
                write_with_gate = is_write(ctx.spec(node.tool)) and node.gate is not None \
                    and node.gate.verdict is not Verdict.BLOCK
                if write_with_gate:
                    ctx.approve_hash(node.id, item.args_hash)
                ctx.set_node_status(node, NodeStatus.READY if write_with_gate else NodeStatus.PENDING,
                                    agent="system")
        approval.status = "resolved"
        approval.resolved_at = now_ms()
        self.cascade_skips(ctx)

    def _apply_edit(self, ctx: RunContext, node: PlanNode, edit: dict[str, Any], item: ApprovalItem) -> None:
        symbols = edit_symbols(ctx.plan, node, ctx.spec)
        for key, value in edit.items():
            node.args[key] = resymbolize(value, symbols)
        try:
            bound = bind(ctx.plan, node, ctx.spec, Mode.SHADOW)
        except BindingError as e:
            ctx.log(f"Edited arguments do not bind yet: {e.message}", level="warning", node_id=node.id)
            return
        node.resolved_args = bound.actual
        ctx.approve_hash(node.id, bound.hash)
        item.args = bound.actual
        item.args_hash = bound.hash
        ctx.log("Arguments edited in review; they will be re-simulated and re-gated", agent="system",
                node_id=node.id, edited=sorted(edit))


def _normalize_decision(value: Any, has_edit: bool) -> str:
    if value is None:
        return "approved" if has_edit else "rejected"
    text = str(value).strip().lower()
    if text in ("approved", "approve", "yes", "true", "edited", "ok"):
        return "approved"
    return "rejected"


def _known_contacts(profile: dict[str, Any]) -> list[str]:
    """Known contacts plus the user themself (a copy to yourself is never an unknown recipient)."""
    contacts = [c for c in profile.get("known_contacts") or [] if isinstance(c, str)]
    me = profile.get("user_email")
    if isinstance(me, str) and me and me not in contacts:
        contacts.append(me)
    return contacts

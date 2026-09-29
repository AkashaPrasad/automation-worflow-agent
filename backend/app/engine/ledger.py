"""The effect ledger: a transactional outbox for side effects.

Order of operations for every real write (SPEC §2.7, pain point 4):

    1. effect row persisted with status ``intent`` and idempotency key ``run:node:args_hash``
    2. tool.run(ctx.idempotency_key)            -- tools must dedupe on the key
    3. row updated to ``applied`` (or ``failed``)

A crash between 1 and 3 leaves an ``intent`` row. On boot the engine asks the tool to *reconcile* by reading the
world back (True → applied, False → safe to re-run, None → unknown → a human decides). Because the key is stable
for the same (run, node, args), a re-run of an approved call can never double-send on a key-honouring provider.

One row per step: the simulated preview, the intent and the applied record are the same row moving through
states, so the UI ledger shows each write once.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..core.models import (
    EffectClass,
    EffectRecord,
    PlanNode,
    ToolError,
    ToolResult,
    ToolSpec,
    now_ms,
)
from .binding import BoundArgs
from .context import RunContext
from .util import compact


def idempotency_key(run_id: str, node_id: str, h: str) -> str:
    return f"{run_id}:{node_id}:{h}"


@dataclass
class RollbackReport:
    compensated: list[EffectRecord] = field(default_factory=list)
    audience_notified: list[EffectRecord] = field(default_factory=list)  # compensated, but people saw it
    not_reversible: list[EffectRecord] = field(default_factory=list)
    failed: list[tuple[EffectRecord, str]] = field(default_factory=list)


class EffectLedger:
    def __init__(self) -> None:
        self._last_applied = 0

    def _applied_now(self) -> int:
        """Strictly increasing within the process, so rollback order is exact even within one millisecond."""
        self._last_applied = max(now_ms(), self._last_applied + 1)
        return self._last_applied

    # -- rows ----------------------------------------------------------------
    def _row_id(self, ctx: RunContext, node_id: str) -> str | None:
        """Reuse the step's latest non-applied row so one write shows up once in the ledger."""
        rows = [e for e in ctx.svc.store.effects(ctx.run.id)
                if e.node_id == node_id and e.status in ("simulated", "intent", "failed", "unknown")]
        return max(rows, key=lambda e: e.created_at).id if rows else None

    def _base(self, ctx: RunContext, node: PlanNode, spec: ToolSpec, bound: BoundArgs,
              template: EffectRecord | None) -> EffectRecord:
        key = idempotency_key(ctx.run.id, node.id, bound.hash)
        record = EffectRecord(
            run_id=ctx.run.id, node_id=node.id, tool=spec.name, app=spec.app, effect=spec.effect,
            idempotency_key=key, args_hash=bound.hash,
            summary=(template.summary if template and template.summary else f"{spec.title or spec.name}: {node.title}"),
            target=dict(template.target) if template else {},
            preview=dict(template.preview) if template and template.preview else {"args": compact(bound.actual)},
            compensation=template.compensation if template else None,
        )
        row = self._row_id(ctx, node.id)
        if row:
            record.id = row
        return record

    def _save(self, ctx: RunContext, record: EffectRecord, *, event: str = "effect.recorded") -> EffectRecord:
        ctx.svc.store.upsert_effect(record)
        ctx.emit(event, agent="executor", node_id=record.node_id or None, effect=record)
        return record

    def record_simulated(self, ctx: RunContext, node: PlanNode, spec: ToolSpec, bound: BoundArgs,
                         result: ToolResult) -> EffectRecord:
        record = self._base(ctx, node, spec, bound, result.effect)
        record.status = "simulated"
        record.simulated = True
        return self._save(ctx, record)

    def record_intent(self, ctx: RunContext, node: PlanNode, spec: ToolSpec, bound: BoundArgs) -> EffectRecord:
        """Persist the intent BEFORE acting. The preview is the approved simulation when there is one."""
        simulated = node.result.effect if node.result and node.result.simulated else None
        record = self._base(ctx, node, spec, bound, simulated)
        record.status = "intent"
        record.simulated = False
        return self._save(ctx, record)

    def mark_applied(self, ctx: RunContext, record: EffectRecord, result: ToolResult) -> EffectRecord:
        actual = result.effect
        if actual is not None:
            record.summary = actual.summary or record.summary
            record.target = dict(actual.target or record.target)
            record.preview = dict(actual.preview or record.preview)
            record.compensation = actual.compensation if actual.compensation is not None else record.compensation
        record.status = "applied"
        record.simulated = False
        record.applied_at = self._applied_now()
        return self._save(ctx, record)

    def mark_failed(self, ctx: RunContext, record: EffectRecord, error: ToolError | None) -> EffectRecord:
        record.status = "failed"
        if error is not None:
            record.preview = {**record.preview, "error": error.message}
        return self._save(ctx, record)

    def mark(self, ctx: RunContext, record: EffectRecord, status: str) -> EffectRecord:
        record.status = status  # type: ignore[assignment]
        if status == "applied":
            record.applied_at = record.applied_at or self._applied_now()
        return self._save(ctx, record)

    # -- crash recovery --------------------------------------------------------
    async def reconcile(self, ctx: RunContext, record: EffectRecord) -> bool | None:
        tool = ctx.tool(record.tool)
        if tool is None:
            return None
        try:
            return await tool.reconcile(record, ctx.tool_context(record.node_id, record.idempotency_key, "live"))
        except Exception as e:  # noqa: BLE001 - unknown is the honest answer when the world cannot be read back
            ctx.log(f"reconcile failed for {record.tool}: {type(e).__name__}", level="warning", agent="executor",
                    node_id=record.node_id or None)
            return None

    # -- rollback ----------------------------------------------------------------
    async def compensate_all(self, ctx: RunContext) -> RollbackReport:
        """Undo applied effects newest-first (later effects may depend on earlier ones, e.g. an invite for an
        event on a doc). Communications cannot be unsent: they are reported, and when a compensation exists
        (delete a Slack message / cancel an invite) it is still applied but flagged as audience-notified."""
        report = RollbackReport()
        applied = [e for e in ctx.svc.store.effects(ctx.run.id) if e.status == "applied"]
        applied.sort(key=lambda e: (e.applied_at or e.created_at, e.created_at))  # stable: ties keep store order
        applied.reverse()
        for record in applied:
            tool = ctx.tool(record.tool)
            spec = tool.spec if tool is not None else ctx.spec(record.tool)
            if tool is None or spec is None or not spec.compensable:
                report.not_reversible.append(record)
                continue
            try:
                res = await tool.compensate(record, ctx.tool_context(record.node_id,
                                                                     f"{record.idempotency_key}:undo", "live"))
            except Exception as e:  # noqa: BLE001
                res = ToolResult(ok=False, error=ToolError(message=f"{type(e).__name__}: {e}"))
            if not res.ok:
                report.failed.append((record, res.error.message if res.error else "compensation failed"))
                continue
            record.status = "compensated"
            record.compensated_at = now_ms()
            self._save(ctx, record, event="effect.compensated")
            report.compensated.append(record)
            if record.effect is EffectClass.COMMUNICATE:
                report.audience_notified.append(record)
        return report

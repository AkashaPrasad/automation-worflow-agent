"""Component boundaries. Each package implements one of these; others depend only on
these Protocols, never on each other's internals.

    engine  --uses-->  LLM (app.llm.muse), Judge (app.judgment), ToolRegistry (app.tools),
                       RunStore + EventBus (app.store)
    api     --uses-->  Orchestrator (app.engine), RunStore, EventBus, ToolRegistry, WorkspaceStore
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from .models import (
    Approval,
    Autonomy,
    EffectRecord,
    GateDecision,
    Intent,
    MemoryItem,
    RecoveryDecision,
    Run,
    RunEvent,
    RunSummary,
    ToolContext,
    ToolError,
    ToolResult,
    ToolSpec,
    Verification,
)

class ConflictError(RuntimeError):
    """Raised by the Orchestrator when an action conflicts with the run's current state
    (e.g. resolving an already-resolved approval, resuming a run that is not paused).
    The API maps it to HTTP 409."""


# ---------------------------------------------------------------------------
# System 2 (Muse) -- app/llm/muse.py
# ---------------------------------------------------------------------------


class LLMResult(Protocol):
    text: str
    data: Any  # parsed JSON when a schema was requested, else None
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost_usd: float
    model: str


class LLM(Protocol):
    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        purpose: str,  # "intent" | "plan" | "replan" | "draft" | "reflect" | "repair_args" | ...
        schema: dict[str, Any] | None = None,  # JSON schema -> structured output
        effort: str = "low",  # minimal | low | medium | high | xhigh
        max_tokens: int = 4000,
    ) -> LLMResult: ...


# ---------------------------------------------------------------------------
# System 1 (Jev / Laya) -- app/judgment/
# ---------------------------------------------------------------------------


class Judge(Protocol):
    """Calibrated, typed judgments. Every method makes ONE System One request with
    parallel questions and returns typed results. Policy (thresholds) lives in code
    in app/judgment/policy.py, not in prompts. On service failure the judge falls
    back to Laya, then to a conservative default (which always routes to a human)."""

    async def needs_clarification(self, request: str, intent: Intent, context: dict[str, Any]) -> tuple[float, str | None]:
        """Returns (p_missing_info, which_missing_item_or_None)."""
        ...

    async def gate_action(
        self,
        *,
        user_request: str,
        intent: Intent,
        tool: ToolSpec,
        args: dict[str, Any],
        preview: dict[str, Any],
        untrusted_context: list[str],  # untrusted texts that flowed into these args
        tainted: bool,
        autonomy: Autonomy,
        tainted_args: list[str] | None = None,  # per-arg provenance; see PlanNode.tainted_args
        known_contacts: list[str],  # emails the user has corresponded with / internal domain
        internal_domain: str,
    ) -> GateDecision: ...

    async def verify(self, *, goal: str, criteria: list[str], evidence: dict[str, Any]) -> Verification: ...

    async def classify_failure(self, *, tool: ToolSpec, args: dict[str, Any], error: ToolError, attempts: int,
                               optional: bool, alternatives: list[str]) -> RecoveryDecision: ...

    async def scan_untrusted(self, texts: dict[str, str]) -> dict[str, float]:
        """Per text id: probability it contains instructions aimed at an AI agent (injection)."""
        ...

    async def rank_memories(self, request: str, items: list[MemoryItem], limit: int = 6) -> list[tuple[MemoryItem, float]]: ...

    async def infer_tool_effect(self, name: str, description: str, annotations: dict[str, Any]) -> tuple[str, float]:
        """For MCP tools without trustworthy annotations: (EffectClass value, confidence)."""
        ...


# ---------------------------------------------------------------------------
# Tools -- app/tools/
# ---------------------------------------------------------------------------


@runtime_checkable
class Tool(Protocol):
    spec: ToolSpec

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Execute for real. Non-read tools MUST populate result.effect (with compensation
        when compensable) and MUST be safe to re-call with the same ctx.idempotency_key
        (return the original result instead of acting twice)."""
        ...

    async def simulate(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Shadow mode. Read tools just run. Write tools validate args, check preconditions
        against current world state, and return the predicted result + effect preview
        WITHOUT changing anything (result.simulated=True, effect.status='simulated')."""
        ...

    async def compensate(self, effect: EffectRecord, ctx: ToolContext) -> ToolResult:
        """Undo a previously applied effect (rollback). Only for compensable tools."""
        ...

    async def reconcile(self, effect: EffectRecord, ctx: ToolContext) -> bool | None:
        """After a crash between 'intent' and 'applied': did the effect actually happen?
        True/False when determinable by reading the world, None when unknown."""
        ...


class ToolRegistry(Protocol):
    def specs(self, workspace_id: str) -> list[ToolSpec]: ...
    def get(self, name: str, workspace_id: str) -> Tool | None: ...
    def integrations(self, workspace_id: str) -> list[dict[str, Any]]:
        """[{app, title, mode: 'sandbox'|'live', connected: bool, detail}]"""
        ...


class WorkspaceStore(Protocol):
    """Sandbox world (simulated Gmail/Calendar/Docs/Sheets/Notion/Slack/Fireflies)."""

    def snapshot(self, workspace_id: str) -> dict[str, Any]: ...
    def reset(self, workspace_id: str) -> dict[str, Any]: ...
    def profile(self, workspace_id: str) -> dict[str, Any]:
        """{user_email, user_name, internal_domain, known_contacts: [...], timezone, now_iso}"""
        ...


# ---------------------------------------------------------------------------
# Persistence + events -- app/store/
# ---------------------------------------------------------------------------


class RunStore(Protocol):
    def create_run(self, run: Run) -> Run: ...
    def save_run(self, run: Run) -> None: ...
    def get_run(self, run_id: str) -> Run | None: ...
    def list_runs(self, workspace_id: str, limit: int = 50) -> list[RunSummary]: ...
    def unfinished_runs(self) -> list[Run]: ...

    def append_event(self, event: RunEvent) -> RunEvent: ...  # assigns seq
    def events(self, run_id: str, after_seq: int = 0) -> list[RunEvent]: ...

    def save_approval(self, approval: Approval) -> None: ...
    def get_approval(self, approval_id: str) -> Approval | None: ...
    def pending_approval(self, run_id: str) -> Approval | None: ...

    def upsert_effect(self, effect: EffectRecord) -> None: ...
    def effects(self, run_id: str) -> list[EffectRecord]: ...
    def effect_by_key(self, idempotency_key: str) -> EffectRecord | None: ...

    def add_memory(self, item: MemoryItem) -> MemoryItem: ...
    def memories(self, workspace_id: str) -> list[MemoryItem]: ...
    def delete_memory(self, memory_id: str) -> None: ...


class EventBus(Protocol):
    """Persist-then-publish. publish() stores via RunStore.append_event and fans out to
    live subscribers (SSE). subscribe() yields events with seq > after_seq, replaying
    stored ones first so reconnecting clients never miss anything."""

    async def publish(self, event: RunEvent) -> RunEvent: ...
    def subscribe(self, run_id: str, after_seq: int = 0) -> AsyncIterator[RunEvent]: ...


# ---------------------------------------------------------------------------
# Orchestrator -- app/engine/
# ---------------------------------------------------------------------------


class Orchestrator(Protocol):
    async def start_run(self, workspace_id: str, request: str, autonomy: Autonomy) -> Run: ...
    async def resolve_approval(self, run_id: str, approval_id: str, decisions: dict[str, str],
                               edits: dict[str, dict[str, Any]], note: str = "") -> Run:
        """decisions: {node_id: 'approved'|'rejected'}; edits: {node_id: {arg: new_value}}.
        Edited args get a fresh args_hash and are re-gated before execution."""
        ...
    async def answer_clarification(self, run_id: str, answer: str) -> Run: ...
    async def pause(self, run_id: str) -> Run: ...  # kill switch: scheduler stops dispatching immediately
    async def resume(self, run_id: str) -> Run: ...
    async def cancel(self, run_id: str) -> Run: ...
    async def rollback(self, run_id: str) -> Run: ...  # compensate applied reversible effects, newest first
    async def recover_unfinished(self) -> None: ...  # on boot: reconcile + resume

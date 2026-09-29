"""Per-run state shared by the engine's components.

One ``RunContext`` exists per active run and owns the single authoritative in-memory ``Run``. API calls
(pause, approve...) and the run's background task mutate the same object, so there are no lost updates between
them; everything else reads the store. All mutation happens on the event loop, so the only interleavings are at
``await`` points, which is why state changes and their persistence are kept free of awaits.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ..config import get_settings
from ..core.interfaces import LLM, EventBus, Judge, RunStore, Tool, ToolRegistry, WorkspaceStore
from ..core.models import (
    Budget,
    Intent,
    NodeKind,
    NodeStatus,
    Plan,
    PlanNode,
    Run,
    RunStatus,
    ToolContext,
    ToolSpec,
    now_ms,
)
from . import prompts
from .errors import BudgetExceeded, Cancelled, Paused
from .events import Agent, EventEmitter, current_node, judgment_agent, llm_agent
from .taint import Endorser, Provenance, slack_channels


@dataclass(frozen=True)
class EngineConfig:
    """Tunables. Policy thresholds for gating live in app/judgment/policy.py; these are engine mechanics."""

    max_concurrent_runs: int = field(default_factory=lambda: get_settings().max_concurrent_runs)
    budget: Budget | None = None  # default per-run budget (None → models.Budget defaults)
    max_parallel_steps: int = 4
    max_attempts: int = 3  # per step, for transient retries (SPEC §3)
    max_identical_calls: int = 3  # loop detection: same tool + args_hash attempted more than 2 times → stop
    max_arg_repairs: int = 2
    retry_base_delay_s: float = 0.5
    retry_max_delay_s: float = 8.0
    tool_timeout_s: float = 90.0
    clarification_threshold: float = 0.75
    min_recovery_confidence: float = 0.5
    memory_recall_limit: int = 6
    memory_min_relevance: float = 0.35
    prescan_escalation: float = 0.7
    scan_text_chars: int = 2000
    max_scan_texts: int = 40
    gate_context_texts: int = 5
    gate_context_chars: int = 1200
    evidence_chars: int = 12000


@dataclass
class Services:
    store: RunStore
    bus: EventBus
    llm: LLM
    judge: Judge
    registry: ToolRegistry
    workspaces: WorkspaceStore
    config: EngineConfig


class RunContext:
    def __init__(self, run: Run, svc: Services) -> None:
        self.run = run
        self.svc = svc
        self.emitter = EventEmitter(svc.bus, run.id)
        self.pause_requested = run.status is RunStatus.PAUSED
        self.pause_observed = False  # set when the running task actually hit the kill switch
        self.cancel_requested = False
        self.cancel_finalized = False
        self.cancelled_from: RunStatus | None = None
        self.resume_status: RunStatus | None = None
        self.approved: dict[str, set[str]] = {}  # node_id → binding hashes a human approved
        self.call_counts: Counter[tuple[str, str]] = Counter()  # (tool, args_hash) → real attempts (loop detection)
        self.injection_scan: dict[str, float] = {}  # untrusted text id → injection probability
        self.switched_tools: dict[str, set[str]] = {}
        self.goal_locks: dict[str, asyncio.Lock] = {}
        self.budget_reported: set[str] = set()
        self._specs: dict[str, ToolSpec] | None = None
        self._profile: dict[str, Any] | None = None
        self._endorser: Endorser | None = None

    # -- accessors -----------------------------------------------------------
    @property
    def plan(self) -> Plan:
        assert self.run.plan is not None, "plan not created yet"
        return self.run.plan

    @property
    def workspace_id(self) -> str:
        return self.run.workspace_id

    def specs(self) -> dict[str, ToolSpec]:
        if self._specs is None:
            self._specs = {s.name: s for s in self.svc.registry.specs(self.workspace_id)}
        return self._specs

    def spec(self, name: str | None) -> ToolSpec | None:
        if not name:
            return None
        found = self.specs().get(name)
        if found is None:  # tools can appear later (MCP servers mounting); refresh once
            self._specs = None
            found = self.specs().get(name)
        return found

    def tool(self, name: str | None) -> Tool | None:
        return self.svc.registry.get(name, self.workspace_id) if name else None

    def profile(self) -> dict[str, Any]:
        if self._profile is None:
            try:
                self._profile = dict(self.svc.workspaces.profile(self.workspace_id) or {})
            except Exception:  # noqa: BLE001
                self._profile = {}
        return self._profile

    def endorser(self) -> Endorser:
        if self._endorser is None:
            try:
                channels = slack_channels(self.svc.workspaces.snapshot(self.workspace_id) or {})
            except Exception:  # noqa: BLE001
                channels = []
            self._endorser = Endorser(self.profile(), channels)
        return self._endorser

    def intent(self) -> Intent:
        return self.run.intent or Intent(goal=self.run.request)

    def request_text(self) -> str:
        """The request plus the clarification exchange: what the user actually asked for."""
        return prompts.request_block(self.run.request, self.run.clarification).removeprefix("Request:\n")

    def provenance(self) -> Provenance:
        return Provenance(self.plan, self.spec)

    def tool_context(self, node_id: str, key: str, mode: str) -> ToolContext:
        return ToolContext(run_id=self.run.id, node_id=node_id, workspace_id=self.workspace_id,
                           idempotency_key=key, mode=mode,  # type: ignore[arg-type]
                           user_email=str(self.profile().get("user_email") or "you@acme.dev"))

    # -- persistence + events ------------------------------------------------
    def persist(self) -> None:
        self.run.updated_at = now_ms()
        self.svc.store.save_run(self.run)

    def emit(self, type_: str, *, agent: Agent = "system", node_id: str | None = None,
             payload: dict[str, Any] | None = None, **data: Any) -> None:
        """``payload`` carries data keys that collide with keyword names (the reflection event has its own
        ``agent`` field)."""
        self.emitter.emit(type_, agent=agent, node_id=node_id, data={**(payload or {}), **data})

    async def flush(self) -> None:
        await self.emitter.flush()

    def snapshot(self) -> Run:
        return self.run.model_copy(deep=True)

    def set_status(self, status: RunStatus) -> None:
        """Every transition is persisted and published as ``run.status`` (the SSE stream and crash recovery
        both depend on it)."""
        if self.run.status is status:
            return
        previous = self.run.status
        self.run.status = status
        self.persist()
        self.emit("run.status", status=status.value, previous=previous.value)

    def transition(self, status: RunStatus) -> None:
        """Pipeline-side status change: refuses to move a run the user just paused or cancelled."""
        self.checkpoint()
        self.set_status(status)

    def set_node_status(self, node: PlanNode, status: NodeStatus, *, agent: Agent = "executor") -> None:
        if node.status is status:
            return
        node.status = status
        if status in (NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.SKIPPED, NodeStatus.BLOCKED,
                      NodeStatus.SIMULATED, NodeStatus.CANCELLED, NodeStatus.COMPENSATED):
            node.finished_at = now_ms()
        self.emit("node.status", agent=agent, node_id=node.id, status=status.value, attempts=node.attempts)

    def log(self, message: str, *, level: str = "info", agent: Agent = "system", node_id: str | None = None,
            **extra: Any) -> None:
        self.emit("log", agent=agent, node_id=node_id, level=level, message=message, **extra)

    # -- control -------------------------------------------------------------
    def checkpoint(self) -> None:
        """Called before every dispatch and every tool call: the kill switch and cancel take effect here."""
        if self.cancel_requested:
            raise Cancelled()
        if self.pause_requested:
            self.pause_observed = True
            raise Paused()

    def check_budget(self) -> None:
        m, b = self.run.metrics, self.run.budget
        for limit, value, maximum in (("max_llm_calls", m.llm_calls, b.max_llm_calls),
                                      ("max_tool_calls", m.tool_calls, b.max_tool_calls),
                                      ("max_cost_usd", m.cost_usd, b.max_cost_usd)):
            if value >= maximum:
                raise BudgetExceeded(limit, value, maximum)

    def can_replan(self) -> bool:
        if self.run.metrics.replans < self.run.budget.max_replans:
            return True
        if "max_replans" not in self.budget_reported:
            self.budget_reported.add("max_replans")
            self.emit("budget.exceeded", limit="max_replans", value=self.run.metrics.replans,
                      max=self.run.budget.max_replans)
        return False

    def goal_lock(self, goal_id: str) -> asyncio.Lock:
        return self.goal_locks.setdefault(goal_id, asyncio.Lock())

    # -- approvals bound to hashes ---------------------------------------------
    def approve_hash(self, node_id: str, h: str) -> None:
        if h:
            self.approved.setdefault(node_id, set()).add(h)

    def is_approved(self, node_id: str, h: str) -> bool:
        return h in self.approved.get(node_id, ())

    def hydrate(self) -> None:
        """Rebuild in-memory state that is derivable from the event log (after a restart)."""
        last_active: RunStatus | None = None
        for ev in self.svc.store.events(self.run.id):
            if ev.type == "approval.resolved":
                for item in (ev.data.get("approval") or {}).get("items", []):
                    if item.get("decision") in ("approved", "edited"):
                        self.approve_hash(str(item.get("node_id")), str(item.get("args_hash") or ""))
            elif ev.type == "run.status":
                try:
                    status = RunStatus(ev.data.get("status"))
                except ValueError:
                    continue
                if status is not RunStatus.PAUSED:
                    last_active = status
        if self.run.status is RunStatus.PAUSED and self.resume_status is None:
            self.resume_status = last_active

    # -- tracing sink ----------------------------------------------------------
    def on_trace(self, kind: str, data: dict[str, Any]) -> None:
        """Installed as ``app.core.tracing.trace_sink`` inside the run's task: accounts every Muse/Jev call in
        ``run.metrics`` (budgets are enforced from these numbers) and publishes it on the timeline."""
        data = dict(data)
        data.pop("agent", None)
        node_id = data.pop("node_id", None) or current_node.get()
        m = self.run.metrics
        purpose = str(data.get("purpose") or "")
        cost = _num(data.get("cost_usd"))
        if kind == "llm.call":
            m.llm_calls += 1
            m.llm_input_tokens += int(_num(data.get("input_tokens")))
            m.llm_output_tokens += int(_num(data.get("output_tokens")))
            m.cost_usd = round(m.cost_usd + cost, 6)
            self.emit("llm.call", agent=llm_agent(purpose), node_id=node_id, **data)
        elif kind == "judgment.call":
            m.judgment_calls += 1
            m.judgment_tokens += int(_tokens(data.get("tokens")))
            m.cost_usd = round(m.cost_usd + cost, 6)
            self.emit("judgment.call", agent=judgment_agent(purpose), node_id=node_id, **data)


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _tokens(value: Any) -> float:
    if isinstance(value, dict):
        return sum(_num(v) for v in value.values())
    return _num(value)


def action_nodes(plan: Plan) -> list[PlanNode]:
    return [n for n in plan.nodes.values() if n.kind is NodeKind.ACTION]

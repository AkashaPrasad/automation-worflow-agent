"""Shared data contracts for Adjutant.

Every component (engine, judgment, tools, store, api, frontend) speaks these types.
Change them deliberately: the frontend mirrors them in `frontend/src/lib/types.ts`.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def now_ms() -> int:
    return int(time.time() * 1000)


def args_hash(tool: str, args: dict[str, Any]) -> str:
    """Stable hash of a tool call. Approvals are bound to it: if args change after
    approval, the approval no longer applies (policy lives outside the model)."""
    blob = json.dumps({"tool": tool, "args": args}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


class EffectClass(str, Enum):
    READ = "read"  # no side effects on the world
    WRITE_REVERSIBLE = "write_reversible"  # changes the world, has a compensation (create doc/event)
    WRITE_IRREVERSIBLE = "write_irreversible"  # cannot be undone (permanent delete, payment)
    COMMUNICATE = "communicate"  # delivers content to other humans (email, slack). Irreversible + audience


class Trust(str, Enum):
    TRUSTED = "trusted"  # the user's own words / content the user authored
    UNTRUSTED = "untrusted"  # external content: inbound email bodies, others' docs, transcripts, web


class ToolSpec(BaseModel):
    name: str  # "gmail.send" (app.verb)
    app: str  # "gmail" | "calendar" | "docs" | "sheets" | "notion" | "slack" | "meetings" | "web" | "llm" | "memory" | "mcp:<server>"
    title: str
    description: str  # written for the planner LLM
    effect: EffectClass
    input_schema: dict[str, Any]  # JSON Schema (object)
    output_trust: Trust = Trust.TRUSTED  # does output carry untrusted external content?
    compensable: bool = False  # has compensate()
    idempotent: bool = False  # safe to call twice with same args
    source: Literal["builtin", "mcp"] = "builtin"
    effect_inferred: bool = False  # True when effect was inferred by Jev (e.g. unannotated MCP tool)


class ErrorKind(str, Enum):
    TRANSIENT = "transient"  # timeout, 5xx, rate limit
    AUTH = "auth"  # expired / missing credentials
    INVALID_ARGS = "invalid_args"  # bad or missing arguments
    NOT_FOUND = "not_found"  # referenced object does not exist
    PRECONDITION = "precondition"  # world state prevents it (slot taken, doc locked)
    PERMISSION = "permission"  # not allowed
    UNKNOWN = "unknown"


class ToolError(BaseModel):
    kind: ErrorKind = ErrorKind.UNKNOWN
    message: str
    retryable: bool = False


class EffectRecord(BaseModel):
    """One change to the world. Written to the ledger BEFORE the call as `intent`
    (transactional outbox) and updated to `applied` after it."""

    id: str = Field(default_factory=lambda: new_id("fx"))
    run_id: str = ""
    node_id: str = ""
    tool: str
    app: str
    effect: EffectClass
    idempotency_key: str = ""
    args_hash: str = ""
    summary: str  # human readable: "Email to dana@northwind.com — 'Q3 renewal recap'"
    target: dict[str, Any] = {}  # {"kind": "email", "id": "msg_x"} -- what was created/changed
    preview: dict[str, Any] = {}  # the content that will be / was delivered (for diff UI)
    compensation: dict[str, Any] | None = None  # {"tool": "calendar.delete_event", "args": {...}}
    status: Literal["intent", "simulated", "applied", "failed", "unknown", "compensated"] = "intent"
    simulated: bool = False
    created_at: int = Field(default_factory=now_ms)
    applied_at: int | None = None
    compensated_at: int | None = None


class ToolResult(BaseModel):
    ok: bool
    output: dict[str, Any] | None = None
    error: ToolError | None = None
    simulated: bool = False
    effect: EffectRecord | None = None  # for non-read tools
    tainted: bool = False  # output contains untrusted content (set by executor from spec + inputs)
    latency_ms: int = 0


class ToolContext(BaseModel):
    run_id: str
    node_id: str
    workspace_id: str
    idempotency_key: str
    mode: Literal["live", "shadow"] = "live"
    user_email: str = "you@acme.dev"  # identity the workspace acts as

    model_config = {"arbitrary_types_allowed": True}


# ---------------------------------------------------------------------------
# Judgments (System 1)
# ---------------------------------------------------------------------------


class Verdict(str, Enum):
    AUTO = "auto"  # execute without asking
    ASK = "ask"  # needs human approval
    BLOCK = "block"  # refused by policy


class GateDecision(BaseModel):
    verdict: Verdict
    risk: float = 0.0  # 0..1 composite, computed in code from signals
    signals: dict[str, float] = {}  # raw judgments: alignment, injection, sensitivity, tone_ok, external...
    reasons: list[str] = []  # human-readable, shown in the approval UI
    autonomy: str = "balanced"
    model: str = ""  # "jev-1.13.0" | "laya" | "fallback-conservative"
    args_hash: str = ""


class CriterionCheck(BaseModel):
    criterion: str
    p: float  # Jev noul: probability the evidence shows the criterion is met
    passed: bool


class Verification(BaseModel):
    passed: bool
    checks: list[CriterionCheck] = []
    model: str = ""


class RecoveryStrategy(str, Enum):
    RETRY_SAME = "retry_same"  # transient: back off and retry
    REPAIR_ARGS = "repair_args"  # ask Muse to fix arguments using the error
    SWITCH_TOOL = "switch_tool"  # use a different tool for the same step
    REPLAN = "replan"  # rebuild the parent goal's subtree
    ASK_HUMAN = "ask_human"  # escalate
    SKIP = "skip"  # optional step, continue without it
    ABORT = "abort"


class RecoveryDecision(BaseModel):
    cause: ErrorKind
    strategy: RecoveryStrategy
    confidence: float
    probabilities: dict[str, float] = {}
    note: str = ""


# ---------------------------------------------------------------------------
# Plan tree
# ---------------------------------------------------------------------------


class NodeKind(str, Enum):
    GOAL = "goal"  # has children; done when children done and criteria verified
    ACTION = "action"  # leaf; one tool call


class NodeStatus(str, Enum):
    PENDING = "pending"  # waiting for dependencies
    READY = "ready"
    RUNNING = "running"
    SIMULATED = "simulated"  # shadow run produced a preview for this write
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"  # refused by gate
    COMPENSATED = "compensated"  # undone during rollback
    CANCELLED = "cancelled"


class PlanNode(BaseModel):
    id: str  # short, stable within a run: "g1", "a3", "a3r1" (replanned)
    parent_id: str | None = None
    kind: NodeKind
    title: str
    rationale: str = ""
    success_criteria: list[str] = []  # checkable post-conditions (goal nodes; optional on actions)
    depends_on: list[str] = []  # node ids that must succeed first
    children: list[str] = []
    tool: str | None = None  # action nodes
    args: dict[str, Any] = {}  # may contain templates "{{a2.output.summary}}"
    resolved_args: dict[str, Any] | None = None  # after template resolution
    optional: bool = False
    status: NodeStatus = NodeStatus.PENDING
    attempts: int = 0
    result: ToolResult | None = None
    gate: GateDecision | None = None
    verification: Verification | None = None
    recovery: list[RecoveryDecision] = []
    tainted: bool = False  # any resolved arg derives from untrusted content
    # Per-argument provenance (CaMeL-style): names of args whose values derive from untrusted
    # content AFTER endorsement (a value equal to a trusted value, e.g. a known contact's email,
    # is endorsed). Untrusted data may fill CONTENT args (body, text, content_md); if it reaches a
    # CONTROL arg (to, cc, bcc, attendees, channel, url, *_id targets) the action is control-tainted.
    tainted_args: list[str] = []
    revision: int = 1  # plan revision that created the node
    started_at: int | None = None
    finished_at: int | None = None


class Plan(BaseModel):
    root_id: str
    revision: int = 1
    nodes: dict[str, PlanNode] = {}

    def children_of(self, node_id: str) -> list[PlanNode]:
        return [self.nodes[c] for c in self.nodes[node_id].children if c in self.nodes]

    def actions(self) -> list[PlanNode]:
        return [n for n in self.nodes.values() if n.kind == NodeKind.ACTION]


class Intent(BaseModel):
    goal: str  # one-sentence restatement of what the user wants
    deliverables: list[str] = []  # concrete outcomes expected
    constraints: list[str] = []  # "before Friday", "don't email the client directly"
    people: list[str] = []  # names/emails mentioned
    apps: list[str] = []  # apps likely involved
    time_refs: list[str] = []  # raw time expressions (resolved in code, never by Jev)
    missing_info: list[str] = []  # what Muse thinks is unknown


# ---------------------------------------------------------------------------
# Runs, approvals, events
# ---------------------------------------------------------------------------


class Autonomy(str, Enum):
    CAUTIOUS = "cautious"  # every write asks
    BALANCED = "balanced"  # low-risk reversible writes auto; communication asks unless clearly safe
    AUTONOMOUS = "autonomous"  # only risky/irreversible/tainted actions ask


class RunStatus(str, Enum):
    CREATED = "created"
    CLARIFYING = "clarifying"  # waiting for the user to answer a question
    PLANNING = "planning"
    SHADOWING = "shadowing"  # dry run: reads real, writes simulated
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    PAUSED = "paused"  # kill switch engaged
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ROLLED_BACK = "rolled_back"


TERMINAL_STATUSES = {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.ROLLED_BACK}


class Budget(BaseModel):
    max_llm_calls: int = 40
    max_tool_calls: int = 60
    max_cost_usd: float = 0.50
    max_replans: int = 3


class Metrics(BaseModel):
    llm_calls: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    judgment_calls: int = 0
    judgment_tokens: int = 0
    tool_calls: int = 0
    replans: int = 0
    cost_usd: float = 0.0
    approvals_requested: int = 0  # individual items that asked
    auto_approved: int = 0  # individual writes the gate let through


class ApprovalItem(BaseModel):
    node_id: str
    tool: str
    summary: str
    effect: EffectClass
    args: dict[str, Any]
    args_hash: str
    preview: dict[str, Any] = {}
    gate: GateDecision
    decision: Literal["pending", "approved", "rejected", "edited"] = "pending"


class Approval(BaseModel):
    id: str = Field(default_factory=lambda: new_id("apv"))
    run_id: str
    kind: Literal["plan_diff", "action"] = "plan_diff"  # plan_diff = one batched review after shadow run
    status: Literal["pending", "resolved"] = "pending"
    items: list[ApprovalItem] = []
    created_at: int = Field(default_factory=now_ms)
    resolved_at: int | None = None
    note: str = ""


class Run(BaseModel):
    id: str = Field(default_factory=lambda: new_id("run"))
    workspace_id: str
    request: str
    title: str = ""
    status: RunStatus = RunStatus.CREATED
    autonomy: Autonomy = Autonomy.BALANCED
    intent: Intent | None = None
    plan: Plan | None = None
    clarification: dict[str, Any] | None = None  # {"question": str, "answer": str | None}
    budget: Budget = Budget()
    metrics: Metrics = Metrics()
    summary: str = ""  # final report to the user
    error: str = ""
    created_at: int = Field(default_factory=now_ms)
    updated_at: int = Field(default_factory=now_ms)


class RunSummary(BaseModel):
    id: str
    title: str
    request: str
    status: RunStatus
    autonomy: Autonomy
    created_at: int
    updated_at: int
    metrics: Metrics


EventType = Literal[
    "run.created",
    "run.status",  # data: {status}
    "intent.parsed",  # data: {intent}
    "clarification.requested",  # data: {question}
    "clarification.answered",  # data: {answer}
    "memory.recalled",  # data: {items: [{id, text, p}]}
    "plan.created",  # data: {plan}
    "plan.revised",  # data: {plan, reason, replaced: [node ids], added: [node ids]}
    "node.status",  # node_id; data: {status, attempts}
    "node.started",  # node_id; data: {tool, args, mode}
    "node.result",  # node_id; data: {result}
    "node.gated",  # node_id; data: {gate}
    "node.verified",  # node_id; data: {verification}
    "recovery.decided",  # node_id; data: {decision}
    "reflection",  # node_id?; data: {text, agent}
    "shadow.completed",  # data: {effects: [EffectRecord]}
    "approval.requested",  # data: {approval}
    "approval.resolved",  # data: {approval}
    "effect.recorded",  # data: {effect}
    "effect.compensated",  # data: {effect}
    "llm.call",  # data: {purpose, model, input_tokens, output_tokens, latency_ms, cost_usd}
    "judgment.call",  # data: {purpose, model, questions: [ids], answers, tokens, latency_ms}
    "budget.exceeded",  # data: {limit, value}
    "run.completed",  # data: {summary}
    "run.failed",  # data: {error}
    "log",  # data: {level, message}
]


class RunEvent(BaseModel):
    seq: int = 0  # assigned by the store, monotonically increasing per run
    run_id: str
    ts: int = Field(default_factory=now_ms)
    type: EventType
    agent: Literal["planner", "executor", "evaluator", "guardian", "system"] = "system"
    node_id: str | None = None
    data: dict[str, Any] = {}


class MemoryItem(BaseModel):
    id: str = Field(default_factory=lambda: new_id("mem"))
    workspace_id: str
    kind: Literal["fact", "preference", "person", "playbook"] = "fact"
    text: str  # "Dana Reyes (dana@northwind.com) is Northwind's procurement lead"
    data: dict[str, Any] = {}  # playbook: {"request": str, "plan_outline": [...]}
    source_run_id: str | None = None
    created_at: int = Field(default_factory=now_ms)
    uses: int = 0

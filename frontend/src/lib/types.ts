// Mirror of backend/app/core/models.py. Keep in lockstep with the backend contract.
// Pydantic defaults become required-but-possibly-empty fields here; `| null` mirrors `X | None`.

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type JsonObject = { [key: string]: unknown };

// ---------------------------------------------------------------------------
// Tools
// ---------------------------------------------------------------------------

export type EffectClass = "read" | "write_reversible" | "write_irreversible" | "communicate";
export type Trust = "trusted" | "untrusted";

export interface ToolSpec {
  name: string; // "gmail.send" (app.verb)
  app: string;
  title: string;
  description: string;
  effect: EffectClass;
  input_schema: JsonObject;
  output_trust: Trust;
  compensable: boolean;
  idempotent: boolean;
  source: "builtin" | "mcp";
  effect_inferred: boolean;
}

export type ErrorKind =
  | "transient"
  | "auth"
  | "invalid_args"
  | "not_found"
  | "precondition"
  | "permission"
  | "unknown";

export interface ToolError {
  kind: ErrorKind;
  message: string;
  retryable: boolean;
}

export type EffectStatus = "intent" | "simulated" | "applied" | "failed" | "unknown" | "compensated";

export interface EffectRecord {
  id: string;
  run_id: string;
  node_id: string;
  tool: string;
  app: string;
  effect: EffectClass;
  idempotency_key: string;
  args_hash: string;
  summary: string;
  target: JsonObject;
  preview: JsonObject;
  compensation: JsonObject | null;
  status: EffectStatus;
  simulated: boolean;
  created_at: number;
  applied_at: number | null;
  compensated_at: number | null;
}

export interface ToolResult {
  ok: boolean;
  output: JsonObject | null;
  error: ToolError | null;
  simulated: boolean;
  effect: EffectRecord | null;
  tainted: boolean;
  latency_ms: number;
}

// ---------------------------------------------------------------------------
// Judgments (System 1)
// ---------------------------------------------------------------------------

export type Verdict = "auto" | "ask" | "block";

export interface GateDecision {
  verdict: Verdict;
  risk: number; // 0..1 composite, computed in code from signals
  signals: Record<string, number>;
  reasons: string[];
  autonomy: string;
  model: string;
  args_hash: string;
}

export interface CriterionCheck {
  criterion: string;
  p: number;
  passed: boolean;
}

export interface Verification {
  passed: boolean;
  checks: CriterionCheck[];
  model: string;
}

export type RecoveryStrategy =
  | "retry_same"
  | "repair_args"
  | "switch_tool"
  | "replan"
  | "ask_human"
  | "skip"
  | "abort";

export interface RecoveryDecision {
  cause: ErrorKind;
  strategy: RecoveryStrategy;
  confidence: number;
  probabilities: Record<string, number>;
  note: string;
}

// ---------------------------------------------------------------------------
// Plan tree
// ---------------------------------------------------------------------------

export type NodeKind = "goal" | "action";

export type NodeStatus =
  | "pending"
  | "ready"
  | "running"
  | "simulated"
  | "awaiting_approval"
  | "succeeded"
  | "failed"
  | "skipped"
  | "blocked"
  | "compensated"
  | "cancelled";

export interface PlanNode {
  id: string;
  parent_id: string | null;
  kind: NodeKind;
  title: string;
  rationale: string;
  success_criteria: string[];
  depends_on: string[];
  children: string[];
  tool: string | null;
  args: JsonObject;
  resolved_args: JsonObject | null;
  optional: boolean;
  status: NodeStatus;
  attempts: number;
  result: ToolResult | null;
  gate: GateDecision | null;
  verification: Verification | null;
  recovery: RecoveryDecision[];
  tainted: boolean;
  revision: number;
  started_at: number | null;
  finished_at: number | null;
}

export interface Plan {
  root_id: string;
  revision: number;
  nodes: Record<string, PlanNode>;
}

export interface Intent {
  goal: string;
  deliverables: string[];
  constraints: string[];
  people: string[];
  apps: string[];
  time_refs: string[];
  missing_info: string[];
}

// ---------------------------------------------------------------------------
// Runs, approvals, events
// ---------------------------------------------------------------------------

export type Autonomy = "cautious" | "balanced" | "autonomous";

export type RunStatus =
  | "created"
  | "clarifying"
  | "planning"
  | "shadowing"
  | "awaiting_approval"
  | "executing"
  | "verifying"
  | "paused"
  | "completed"
  | "failed"
  | "cancelled"
  | "rolled_back";

export const TERMINAL_STATUSES: ReadonlySet<RunStatus> = new Set<RunStatus>([
  "completed",
  "failed",
  "cancelled",
  "rolled_back",
]);

export interface Budget {
  max_llm_calls: number;
  max_tool_calls: number;
  max_cost_usd: number;
  max_replans: number;
}

export interface Metrics {
  llm_calls: number;
  llm_input_tokens: number;
  llm_output_tokens: number;
  judgment_calls: number;
  judgment_tokens: number;
  tool_calls: number;
  replans: number;
  cost_usd: number;
  approvals_requested: number;
  auto_approved: number;
}

export type ApprovalDecision = "pending" | "approved" | "rejected" | "edited";

export interface ApprovalItem {
  node_id: string;
  tool: string;
  summary: string;
  effect: EffectClass;
  args: JsonObject;
  args_hash: string;
  preview: JsonObject;
  gate: GateDecision;
  decision: ApprovalDecision;
}

export interface Approval {
  id: string;
  run_id: string;
  kind: "plan_diff" | "action";
  status: "pending" | "resolved";
  items: ApprovalItem[];
  created_at: number;
  resolved_at: number | null;
  note: string;
}

export interface Clarification {
  question: string;
  answer: string | null;
  [key: string]: unknown;
}

export interface Run {
  id: string;
  workspace_id: string;
  request: string;
  title: string;
  status: RunStatus;
  autonomy: Autonomy;
  intent: Intent | null;
  plan: Plan | null;
  clarification: Clarification | null;
  budget: Budget;
  metrics: Metrics;
  summary: string;
  error: string;
  created_at: number;
  updated_at: number;
}

export interface RunSummary {
  id: string;
  title: string;
  request: string;
  status: RunStatus;
  autonomy: Autonomy;
  created_at: number;
  updated_at: number;
  metrics: Metrics;
}

export type EventType =
  | "run.created"
  | "run.status"
  | "intent.parsed"
  | "clarification.requested"
  | "clarification.answered"
  | "memory.recalled"
  | "plan.created"
  | "plan.revised"
  | "node.status"
  | "node.started"
  | "node.result"
  | "node.gated"
  | "node.verified"
  | "recovery.decided"
  | "reflection"
  | "shadow.completed"
  | "approval.requested"
  | "approval.resolved"
  | "effect.recorded"
  | "effect.compensated"
  | "llm.call"
  | "judgment.call"
  | "budget.exceeded"
  | "run.completed"
  | "run.failed"
  | "log";

export type Agent = "planner" | "executor" | "evaluator" | "guardian" | "system";

export interface RunEvent {
  seq: number;
  run_id: string;
  ts: number;
  type: EventType;
  agent: Agent;
  node_id: string | null;
  data: JsonObject;
}

export type MemoryKind = "fact" | "preference" | "person" | "playbook";

export interface MemoryItem {
  id: string;
  workspace_id: string;
  kind: MemoryKind;
  text: string;
  data: JsonObject;
  source_run_id: string | null;
  created_at: number;
  uses: number;
}

// ---------------------------------------------------------------------------
// API envelopes (SPEC section 7)
// ---------------------------------------------------------------------------

export interface Health {
  ok: boolean;
  version: string;
  models: { planner: string; judge: string; fallback: string };
  llm_configured: boolean;
  judge_configured: boolean;
}

export interface AutonomyLevel {
  id: Autonomy;
  title: string;
  description: string;
}

export interface Template {
  id: string;
  title: string;
  prompt: string;
  apps: string[];
  highlights?: string[];
}

/** Integration entries are defined by the tool registry; treat every field as optional. */
export interface Integration {
  app?: string;
  name?: string;
  id?: string;
  title?: string;
  mode?: string; // "sandbox" | "live"
  status?: string;
  live?: boolean;
  connected?: boolean;
  [key: string]: unknown;
}

export interface AppConfig {
  autonomy_levels: AutonomyLevel[] | string[];
  policy: { weights?: Record<string, number>; thresholds?: Record<string, unknown>; [key: string]: unknown };
  integrations: Integration[] | Record<string, unknown>;
  budget_defaults: Budget;
  templates: Template[];
  models?: { planner: string; judge: string; fallback: string };
}

export interface RunDetail {
  run: Run;
  approval: Approval | null;
  effects: EffectRecord[];
}

export interface ApprovalSubmission {
  decisions: Record<string, "approved" | "rejected">;
  edits: Record<string, Record<string, unknown>>;
  note?: string;
}

/** Sandbox snapshot. Shape owned by the sandbox engineer; render defensively. */
export interface WorkspaceSnapshot {
  profile?: JsonObject;
  mail?: JsonObject[];
  sent?: JsonObject[];
  drafts?: JsonObject[];
  calendar?: JsonObject[];
  docs?: JsonObject[];
  sheets?: JsonObject[];
  notion?: JsonObject[];
  slack?: JsonObject | JsonObject[];
  meetings?: JsonObject[];
  outbox?: JsonObject[];
  now?: string | number;
  [key: string]: unknown;
}

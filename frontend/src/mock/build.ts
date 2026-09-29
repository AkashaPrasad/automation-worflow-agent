// Builders for mock fixtures (VITE_MOCK=1 only; tree-shaken out of production builds).
import type {
  EffectClass,
  EffectRecord,
  ErrorKind,
  GateDecision,
  JsonObject,
  Metrics,
  Plan,
  PlanNode,
  RecoveryDecision,
  RunEvent,
  ToolResult,
} from "../lib/types";

export const NOW = Date.now();
export const MIN = 60_000;
export const HOUR = 60 * MIN;
export const DAY = 24 * HOUR;

export const MUSE = "muse-spark-1.3-contributor";
export const JEV = "jev-1.13.0";
export const PRICE_IN = 0.1 / 1_000_000;
export const PRICE_OUT = 0.2 / 1_000_000;
export const JEV_PRICE = 0.042 / 1_000_000;

export function hex(seed: string, len = 16): string {
  let h1 = 0x811c9dc5;
  let h2 = 0x1234567;
  for (let i = 0; i < seed.length; i++) {
    h1 = Math.imul(h1 ^ seed.charCodeAt(i), 16777619);
    h2 = Math.imul(h2 ^ seed.charCodeAt(i), 2246822519);
  }
  let out = "";
  let a = h1 >>> 0;
  let b = h2 >>> 0;
  while (out.length < len) {
    a = Math.imul(a ^ (a >>> 15), 2654435761) >>> 0;
    b = Math.imul(b ^ (b >>> 13), 1597334677) >>> 0;
    out += ((a ^ b) >>> 0).toString(16).padStart(8, "0");
  }
  return out.slice(0, len);
}

export function metrics(p: Partial<Metrics> = {}): Metrics {
  return {
    llm_calls: 0,
    llm_input_tokens: 0,
    llm_output_tokens: 0,
    judgment_calls: 0,
    judgment_tokens: 0,
    tool_calls: 0,
    replans: 0,
    cost_usd: 0,
    approvals_requested: 0,
    auto_approved: 0,
    ...p,
  };
}

type NodeInit = Partial<PlanNode> & Pick<PlanNode, "id" | "title">;

export function goal(p: NodeInit & { children: string[] }): PlanNode {
  return base({ kind: "goal", ...p });
}

export function action(p: NodeInit & { tool: string }): PlanNode {
  return base({ kind: "action", ...p });
}

function base(p: NodeInit): PlanNode {
  return {
    parent_id: null,
    kind: "action",
    rationale: "",
    success_criteria: [],
    depends_on: [],
    children: [],
    tool: null,
    args: {},
    resolved_args: null,
    optional: false,
    status: "pending",
    attempts: 0,
    result: null,
    gate: null,
    verification: null,
    recovery: [],
    tainted: false,
    revision: 1,
    started_at: null,
    finished_at: null,
    ...p,
  };
}

export function plan(rootId: string, nodes: PlanNode[], revision = 1): Plan {
  const map: Record<string, PlanNode> = {};
  for (const n of nodes) map[n.id] = n;
  // parent links from children lists
  for (const n of nodes) for (const c of n.children) if (map[c]) map[c].parent_id = n.id;
  return { root_id: rootId, revision, nodes: map };
}

export function ok(output: JsonObject, p: Partial<ToolResult> = {}): ToolResult {
  return { ok: true, output, error: null, simulated: false, effect: null, tainted: false, latency_ms: 180, ...p };
}

export function fail(kind: ErrorKind, message: string, p: Partial<ToolResult> = {}): ToolResult {
  return {
    ok: false,
    output: null,
    error: { kind, message, retryable: kind === "transient" },
    simulated: false,
    effect: null,
    tainted: false,
    latency_ms: 240,
    ...p,
  };
}

export function gate(verdict: GateDecision["verdict"], risk: number, signals: Record<string, number>, reasons: string[], autonomy = "balanced"): GateDecision {
  return { verdict, risk, signals, reasons, autonomy, model: JEV, args_hash: "" };
}

export function recovery(p: Partial<RecoveryDecision> & Pick<RecoveryDecision, "cause" | "strategy" | "confidence">): RecoveryDecision {
  return { probabilities: {}, note: "", ...p };
}

export function effect(
  runId: string,
  node: PlanNode,
  appName: string,
  eff: EffectClass,
  summary: string,
  preview: JsonObject,
  p: Partial<EffectRecord> = {},
): EffectRecord {
  const argsHash = hex(`${node.tool}${JSON.stringify(node.resolved_args ?? node.args)}`);
  return {
    id: `fx_${hex(runId + node.id, 12)}`,
    run_id: runId,
    node_id: node.id,
    tool: node.tool ?? "",
    app: appName,
    effect: eff,
    idempotency_key: `${runId.replace("run_", "")}:${node.id}:${argsHash}`,
    args_hash: argsHash,
    summary,
    target: {},
    preview,
    compensation: null,
    status: "simulated",
    simulated: true,
    created_at: NOW,
    applied_at: null,
    compensated_at: null,
    ...p,
  };
}

/** Sequenced event builder for a run's history. */
export function eventLog(runId: string, start: number) {
  const events: RunEvent[] = [];
  let t = start;
  const push = (type: RunEvent["type"], agent: RunEvent["agent"], data: JsonObject = {}, nodeId: string | null = null, dt = 400) => {
    t += dt;
    events.push({ seq: events.length + 1, run_id: runId, ts: t, type, agent, node_id: nodeId, data });
  };
  const llm = (purpose: string, input: number, output: number, latency: number, agent: RunEvent["agent"] = "planner", nodeId: string | null = null) =>
    push(
      "llm.call",
      agent,
      {
        purpose,
        model: MUSE,
        input_tokens: input,
        output_tokens: output,
        latency_ms: latency,
        cost_usd: +(input * PRICE_IN + output * PRICE_OUT).toFixed(6),
      },
      nodeId,
      latency,
    );
  const judge = (
    purpose: string,
    questions: string[],
    answers: JsonObject,
    tokens: number,
    latency: number,
    agent: RunEvent["agent"] = "guardian",
    nodeId: string | null = null,
  ) =>
    push(
      "judgment.call",
      agent,
      { purpose, model: JEV, questions, answers, tokens, latency_ms: latency, cost_usd: +(tokens * JEV_PRICE).toFixed(6) },
      nodeId,
      latency,
    );
  return { events, push, llm, judge, at: () => t };
}

export function clone<T>(v: T): T {
  return JSON.parse(JSON.stringify(v)) as T;
}

export function weekday(offsetDays: number, hour: number, minute = 0): number {
  const d = new Date(NOW);
  d.setDate(d.getDate() + offsetDays);
  d.setHours(hour, minute, 0, 0);
  return d.getTime();
}

/** Monday of the current week at 00:00. */
export function mondayOf(ts = NOW): Date {
  const d = new Date(ts);
  const day = (d.getDay() + 6) % 7;
  d.setDate(d.getDate() - day);
  d.setHours(0, 0, 0, 0);
  return d;
}

export function at(dayFromMonday: number, hour: number, minute = 0, week = 0): string {
  const d = mondayOf();
  d.setDate(d.getDate() + dayFromMonday + week * 7);
  d.setHours(hour, minute, 0, 0);
  return d.toISOString();
}

export function nextWeekday(target: number): Date {
  // target: 0=Sun..6=Sat
  const d = new Date(NOW);
  const diff = (target - d.getDay() + 7) % 7 || 7;
  d.setDate(d.getDate() + diff);
  return d;
}

export function longDate(d: Date): string {
  return d.toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric" });
}

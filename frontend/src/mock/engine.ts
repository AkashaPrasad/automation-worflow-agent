// In-browser mock of the Adjutant API (VITE_MOCK=1). It seeds a few runs by replaying scenario
// scripts on a virtual clock, then drives new runs live with the same scripts so every screen,
// state and transition can be exercised without the backend.
import { ApiError } from "../lib/api";
import type {
  Approval,
  ApprovalItem,
  Autonomy,
  EffectRecord,
  GateDecision,
  Health,
  JsonObject,
  MemoryItem,
  PlanNode,
  Run,
  RunDetail,
  RunEvent,
  RunStatus,
  RunSummary,
  WorkspaceSnapshot,
} from "../lib/types";
import { TERMINAL_STATUSES } from "../lib/types";
import { DAY, HOUR, JEV, JEV_PRICE, MIN, MUSE, NOW, PRICE_IN, PRICE_OUT, clone, effect as mkEffect, fail, hex, metrics } from "./build";
import { DESIGN, HIRING, QBR, TRIAGE, pickScenario, type Scenario } from "./scenarios";
import { buildWorld } from "./world";
import { MOCK_CONFIG, MOCK_TOOLS } from "./catalog";

interface Rec {
  run: Run;
  approvals: Approval[];
  effects: EffectRecord[];
  events: RunEvent[];
  listeners: Set<(e: RunEvent) => void>;
  scenario: Scenario;
  final: Record<string, PlanNode>;
  virtual: number | null;
  paused: boolean;
  resumeTo: RunStatus | null;
  cancelled: boolean;
  driving: boolean;
  gen: Generator<number> | null;
}

const WORKSPACE = "mock-workspace";
const runs = new Map<string, Rec>();
let world: WorkspaceSnapshot = buildWorld();
let memories: MemoryItem[] = [];

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const clock = (rec: Rec) => rec.virtual ?? Date.now();

// ---------------------------------------------------------------------------
// event plumbing
// ---------------------------------------------------------------------------

function emit(rec: Rec, type: RunEvent["type"], agent: RunEvent["agent"], data: JsonObject = {}, nodeId: string | null = null) {
  const ev: RunEvent = { seq: rec.events.length + 1, run_id: rec.run.id, ts: clock(rec), type, agent, node_id: nodeId, data: clone(data) };
  rec.events.push(ev);
  rec.run.updated_at = ev.ts;
  if (rec.virtual === null) for (const l of rec.listeners) setTimeout(() => l(ev), 0);
}

function setStatus(rec: Rec, status: RunStatus) {
  rec.run.status = status;
  emit(rec, "run.status", "system", { status });
}

function llm(rec: Rec, purpose: string, input: number, output: number, latency: number, agent: RunEvent["agent"] = "planner", nodeId: string | null = null) {
  const cost = input * PRICE_IN + output * PRICE_OUT;
  const m = rec.run.metrics;
  m.llm_calls += 1;
  m.llm_input_tokens += input;
  m.llm_output_tokens += output;
  m.cost_usd = +(m.cost_usd + cost).toFixed(6);
  emit(rec, "llm.call", agent, { purpose, model: MUSE, input_tokens: input, output_tokens: output, latency_ms: latency, cost_usd: +cost.toFixed(6) }, nodeId);
}

function judge(rec: Rec, purpose: string, questions: string[], answers: JsonObject, tokens: number, latency: number, agent: RunEvent["agent"] = "guardian", nodeId: string | null = null) {
  const cost = tokens * JEV_PRICE;
  const m = rec.run.metrics;
  m.judgment_calls += 1;
  m.judgment_tokens += tokens;
  m.cost_usd = +(m.cost_usd + cost).toFixed(6);
  emit(rec, "judgment.call", agent, { purpose, model: JEV, questions, answers, tokens, latency_ms: latency, cost_usd: +cost.toFixed(6) }, nodeId);
}

function nodeStatus(rec: Rec, node: PlanNode, status: PlanNode["status"]) {
  node.status = status;
  emit(rec, "node.status", "executor", { status, attempts: node.attempts }, node.id);
}

// ---------------------------------------------------------------------------
// scripts (generators yield the delay before their next step)
// ---------------------------------------------------------------------------

function pristine(n: PlanNode): PlanNode {
  return {
    ...clone(n),
    status: "pending",
    attempts: 0,
    result: null,
    gate: null,
    verification: null,
    recovery: [],
    tainted: false,
    resolved_args: null,
    started_at: null,
    finished_at: null,
  };
}

function* intake(rec: Rec): Generator<number> {
  setStatus(rec, "planning");
  yield 500;
  llm(rec, "intent", 1840, 312, 1420);
  rec.run.intent = clone(rec.scenario.intent) as unknown as Run["intent"];
  emit(rec, "intent.parsed", "planner", { intent: rec.scenario.intent });
  yield 300;
  const needs = rec.scenario.key === "design" && !rec.run.clarification?.answer;
  judge(rec, "needs_clarification", ["missing_info"], { missing_info: needs ? 0.86 : 0.12 }, 410, 96, "guardian");
  if (needs) {
    const question =
      "Which engineering leads should I include: Tomás Rivera and Elena Petrova, or also Sofia Marin from Controls? And should it be this week or next?";
    rec.run.clarification = { question, answer: null };
    emit(rec, "clarification.requested", "planner", { question });
    setStatus(rec, "clarifying");
    return;
  }
  yield* planAndShadow(rec);
}

function* planAndShadow(rec: Rec): Generator<number> {
  if (rec.run.status !== "planning") setStatus(rec, "planning");
  yield 300;
  judge(rec, "rank_memories", ["m1", "m2", "m3"], { m1: 0.94, m2: 0.71, m3: 0.18 }, 620, 88, "evaluator");
  emit(rec, "memory.recalled", "evaluator", {
    items: [
      { id: "mem_dana", text: "Dana Reyes (dana@northwind.com) is Northwind's VP Procurement.", p: 0.94 },
      { id: "mem_globex", text: "Bank detail changes from vendors are never accepted by email.", p: 0.71 },
    ],
  });
  yield 600;
  llm(rec, "plan", 5210, 1680, 6120);
  const nodes: Record<string, PlanNode> = {};
  for (const [id, n] of Object.entries(rec.final)) if (n.revision === 1) nodes[id] = pristine(n);
  for (const n of Object.values(nodes)) n.children = n.children.filter((c) => c in nodes);
  rec.run.plan = { root_id: rec.scenario.plan.root_id, revision: 1, nodes };
  emit(rec, "plan.created", "planner", { plan: rec.run.plan });
  yield 500;
  setStatus(rec, "shadowing");

  const actions = Object.values(rec.final).filter((n) => n.kind === "action");
  for (const f of actions) {
    const plan = rec.run.plan!;
    const node = plan.nodes[f.id];
    if (!node) continue;
    const write = rec.scenario.writes[f.id];
    yield 260;
    node.status = "running";
    node.started_at = clock(rec);
    node.attempts = 1;
    rec.run.metrics.tool_calls += 1;
    emit(rec, "node.started", "executor", { tool: node.tool, args: node.args, mode: write ? "shadow" : "live" }, node.id);
    emit(rec, "node.status", "executor", { status: "running", attempts: 1 }, node.id);
    yield write ? 420 : 520 + ((f.result?.latency_ms ?? 200) % 400);
    if (node.tool?.startsWith("llm.")) llm(rec, node.tool.slice(4), 2100, 480, f.result?.latency_ms ?? 1600, "executor", node.id);

    const rec0 = f.recovery[0];
    if (!write && rec0?.strategy === "retry_same") {
      const err = fail("transient", "503 Service Unavailable from gmail", { latency_ms: 3010 });
      emit(rec, "node.result", "executor", { result: err }, node.id);
      judge(rec, "classify_failure", ["transient", "auth", "invalid_args", "not_found"], rec0.probabilities as JsonObject, 380, 81, "guardian", node.id);
      node.recovery = [clone(rec0)];
      emit(rec, "recovery.decided", "guardian", { decision: rec0 }, node.id);
      yield 900;
      node.attempts = 2;
      rec.run.metrics.tool_calls += 1;
      emit(rec, "node.started", "executor", { tool: node.tool, args: node.args, mode: "live", attempt: 2 }, node.id);
      yield 400;
    }
    if (!write && rec0?.strategy === "replan") {
      node.result = clone(f.result);
      node.finished_at = clock(rec);
      emit(rec, "node.result", "executor", { result: f.result }, node.id);
      nodeStatus(rec, node, "failed");
      judge(rec, "classify_failure", ["transient", "not_found", "invalid_args", "permission"], rec0.probabilities as JsonObject, 380, 92, "guardian", node.id);
      node.recovery = [clone(rec0)];
      emit(rec, "recovery.decided", "guardian", { decision: rec0 }, node.id);
      yield 400;
      emit(rec, "reflection", "planner", { text: rec0.note, agent: "planner" }, node.parent_id);
      llm(rec, "replan", 3890, 940, 3400);
      const added = Object.values(rec.final).filter((n) => n.revision > 1);
      for (const a of added) {
        plan.nodes[a.id] = pristine(a);
        const parent = a.parent_id ? plan.nodes[a.parent_id] : null;
        if (parent && !parent.children.includes(a.id)) {
          const idx = parent.children.indexOf(node.id);
          parent.children.splice(idx + 1, 0, a.id);
        }
      }
      plan.revision = 2;
      rec.run.metrics.replans += 1;
      emit(rec, "plan.revised", "planner", {
        plan,
        reason: rec0.note,
        replaced: [node.id],
        added: added.map((a) => a.id),
      });
      continue;
    }

    node.tainted = f.tainted;
    node.resolved_args = clone(f.resolved_args ?? f.args);
    node.finished_at = clock(rec);
    if (write) {
      const fx = mkEffect(rec.run.id, node, write.app, write.effect, write.summary, write.preview, {
        created_at: clock(rec),
        compensation: write.compensation ?? null,
      });
      const result = { ...clone(f.result!), simulated: true, effect: fx };
      node.result = result;
      rec.effects.push(fx);
      emit(rec, "node.result", "executor", { result }, node.id);
      emit(rec, "effect.recorded", "executor", { effect: fx }, node.id);
      nodeStatus(rec, node, "simulated");
    } else {
      node.result = clone(f.result);
      emit(rec, "node.result", "executor", { result: f.result }, node.id);
      nodeStatus(rec, node, f.status === "failed" ? "failed" : "succeeded");
      if (f.tainted && !node.tool?.startsWith("llm.")) {
        const scan = (f.result?.output as { injection_scan?: { p: number } } | null)?.injection_scan?.p;
        judge(rec, "scan_untrusted", [`${node.id}.output`], { [`${node.id}.output`]: scan ?? 0.03 }, 1450, 118, "guardian", node.id);
      }
    }
  }

  emit(rec, "shadow.completed", "system", { effects: rec.effects });
  yield 500;
  const writes = actions.filter((f) => rec.scenario.writes[f.id] && rec.run.plan!.nodes[f.id]);
  judge(
    rec,
    "gate_action",
    writes.flatMap((w) => [`${w.id}.alignment`, `${w.id}.injection`, `${w.id}.sensitive`, `${w.id}.tone_ok`]),
    Object.fromEntries(writes.map((w) => [w.id, w.gate?.signals ?? {}])),
    3900,
    184,
    "guardian",
  );
  const asks: PlanNode[] = [];
  for (const f of writes) {
    const node = rec.run.plan!.nodes[f.id];
    const g = gateFor(rec, f);
    node.gate = g;
    emit(rec, "node.gated", "guardian", { gate: g }, node.id);
    if (g.verdict === "block") nodeStatus(rec, node, "blocked");
    if (g.verdict === "ask") asks.push(node);
    if (g.verdict === "auto") rec.run.metrics.auto_approved += 1;
  }
  yield 300;
  const blocks = writes.map((f) => rec.run.plan!.nodes[f.id]).filter((n) => n.gate?.verdict === "block");
  if (asks.length) {
    rec.run.metrics.approvals_requested += asks.length;
    const items: ApprovalItem[] = [...asks, ...blocks].map((n) => {
      const w = rec.scenario.writes[n.id];
      return {
        node_id: n.id,
        tool: n.tool ?? "",
        summary: w.summary,
        effect: w.effect,
        args: clone(n.resolved_args ?? n.args),
        args_hash: n.gate?.args_hash ?? "",
        preview: clone(w.preview),
        gate: clone(n.gate!),
        decision: "pending",
      };
    });
    const apv: Approval = {
      id: `apv_${hex(rec.run.id + "apv", 12)}`,
      run_id: rec.run.id,
      kind: "plan_diff",
      status: "pending",
      items,
      created_at: clock(rec),
      resolved_at: null,
      note: "",
    };
    rec.approvals.push(apv);
    for (const n of asks) nodeStatus(rec, n, "awaiting_approval");
    emit(rec, "approval.requested", "system", { approval: apv });
    setStatus(rec, "awaiting_approval");
    return;
  }
  yield* commit(rec, {}, {});
}

function gateFor(rec: Rec, f: PlanNode): GateDecision {
  const g = clone(f.gate!);
  g.autonomy = rec.run.autonomy;
  g.args_hash = hex(`${f.tool}${JSON.stringify(f.resolved_args ?? f.args)}`);
  if (g.verdict === "block") return g;
  if (rec.run.autonomy === "cautious" && g.verdict === "auto") {
    g.verdict = "ask";
    g.reasons = ["Cautious mode: every write asks.", ...g.reasons];
  } else if (rec.run.autonomy === "autonomous" && g.verdict === "ask" && g.risk < 0.6 && !((g.signals.tainted ?? 0) >= 0.5 && (g.signals.injection ?? 0) >= 0.3)) {
    g.verdict = "auto";
    g.reasons = [`Autonomous mode: risk ${Math.round(g.risk * 100)}% is under 60% with no injection signal.`, ...g.reasons];
  }
  return g;
}

function* commit(rec: Rec, decisions: Record<string, string>, edits: Record<string, Record<string, unknown>>): Generator<number> {
  setStatus(rec, "executing");
  const plan = rec.run.plan!;
  const writes = Object.values(plan.nodes).filter((n) => n.kind === "action" && rec.scenario.writes[n.id]);
  for (const node of writes) {
    const verdict = node.gate?.verdict;
    if (verdict === "block") continue;
    if (verdict === "ask" && decisions[node.id] !== "approved") {
      yield 200;
      nodeStatus(rec, node, "skipped");
      continue;
    }
    yield 450;
    if (edits[node.id]) {
      node.resolved_args = { ...(node.resolved_args ?? node.args), ...edits[node.id] };
    }
    node.status = "running";
    node.started_at = clock(rec);
    rec.run.metrics.tool_calls += 1;
    emit(rec, "node.started", "executor", { tool: node.tool, args: node.resolved_args, mode: "live" }, node.id);
    emit(rec, "node.status", "executor", { status: "running", attempts: node.attempts }, node.id);
    yield 700;
    const fx = rec.effects.find((e) => e.node_id === node.id);
    const f = rec.final[node.id];
    if (fx) {
      fx.status = "applied";
      fx.simulated = false;
      fx.applied_at = clock(rec);
      if (edits[node.id]) fx.preview = { ...fx.preview, ...edits[node.id] };
      applyToWorld(rec, node, fx);
    }
    node.result = { ...clone(f?.result ?? { ok: true, output: {}, error: null, simulated: false, effect: null, tainted: false, latency_ms: 300 }), simulated: false, effect: fx ? clone(fx) : null };
    node.finished_at = clock(rec);
    emit(rec, "node.result", "executor", { result: node.result }, node.id);
    if (fx) emit(rec, "effect.recorded", "executor", { effect: fx }, node.id);
    nodeStatus(rec, node, "succeeded");
  }

  setStatus(rec, "verifying");
  const goals = Object.values(plan.nodes).filter((n) => n.kind === "goal").reverse();
  for (const g of goals) {
    yield 380;
    const v = clone(rec.scenario.verifications[g.id] ?? { passed: true, checks: g.success_criteria.map((c) => ({ criterion: c, p: 0.9, passed: true })), model: JEV });
    const skippedChild = g.children.some((c) => plan.nodes[c]?.status === "skipped");
    if (skippedChild && g.id !== plan.root_id) {
      v.passed = false;
      v.checks = v.checks.map((c, i) => (i === 0 ? { ...c, p: 0.14, passed: false } : c));
    }
    judge(rec, "verify", v.checks.map((_c, i) => `${g.id}.c${i + 1}`), Object.fromEntries(v.checks.map((c, i) => [`${g.id}.c${i + 1}`, c.p])), 900 + v.checks.length * 120, 104, "evaluator", g.id);
    g.verification = v;
    emit(rec, "node.verified", "evaluator", { verification: v }, g.id);
    nodeStatus(rec, g, v.passed ? "succeeded" : "skipped");
  }
  yield 500;
  llm(rec, "summary", 4100, 620, 2900);
  yield 200;
  llm(rec, "extract_memories", 2600, 260, 1500);
  for (const m of rec.scenario.memories) {
    memories.unshift({
      id: `mem_${hex(rec.run.id + m.text, 10)}`,
      workspace_id: WORKSPACE,
      kind: m.kind,
      text: m.text,
      data: m.data ?? {},
      source_run_id: rec.run.id,
      created_at: clock(rec),
      uses: 0,
    });
  }
  const rejected = Object.entries(decisions).filter(([, d]) => d === "rejected").length;
  rec.run.summary =
    rec.scenario.summary + (rejected && rec.scenario.key !== "triage" ? `\n\n_${rejected} change${rejected > 1 ? "s were" : " was"} rejected at review and skipped._` : "");
  emit(rec, "run.completed", "planner", { summary: rec.run.summary });
  setStatus(rec, "completed");
}

function applyToWorld(rec: Rec, node: PlanNode, fx: EffectRecord) {
  if (rec.virtual !== null) return; // seeded history does not mutate the sandbox
  const pv = fx.preview as Record<string, unknown>;
  const kind = node.tool === "gmail.send" ? "email" : node.tool === "calendar.create_event" ? "invite" : node.tool?.split(".")[0] ?? "change";
  world.outbox = [
    {
      id: `out_${hex(fx.id, 8)}`,
      kind,
      to: (pv.to as string[]) ?? (pv.attendees as string[]) ?? (pv.channel ? [String(pv.channel)] : []),
      subject: String(pv.subject ?? pv.title ?? pv.channel ?? fx.summary),
      body: String(pv.body ?? pv.text ?? pv.content_md ?? ""),
      at: new Date().toISOString(),
      adjutant_key: fx.idempotency_key,
      run_id: rec.run.id,
      node_id: node.id,
      tool: node.tool,
    },
    ...(world.outbox ?? []),
  ];
  if (node.tool === "notion.create_page") {
    world.notion = [{ id: `ntn_${hex(fx.id, 6)}`, title: pv.title, parent: pv.parent ?? null, content_md: pv.content_md, created_at: new Date().toISOString(), by_agent: true }, ...(world.notion ?? [])];
  }
}

// ---------------------------------------------------------------------------
// runners
// ---------------------------------------------------------------------------

function runSync(rec: Rec, gen: Generator<number>, start: number, stopWhen?: () => boolean) {
  rec.virtual = start;
  rec.gen = null;
  for (;;) {
    if (stopWhen?.()) {
      rec.gen = gen; // suspended mid-script; resumed live later
      break;
    }
    const r = gen.next();
    if (r.done) break;
    rec.virtual += r.value;
  }
  rec.virtual = null;
}

async function drive(rec: Rec, gen: Generator<number>) {
  rec.driving = true;
  rec.gen = gen;
  try {
    for (;;) {
      if (rec.cancelled) return;
      while (rec.paused && !rec.cancelled) await sleep(200);
      if (rec.cancelled) return;
      const r = gen.next();
      if (r.done) return;
      await sleep(r.value);
    }
  } finally {
    rec.driving = false;
    rec.gen = null;
  }
}

function newRec(id: string, request: string, autonomy: Autonomy, scenario: Scenario, createdAt: number): Rec {
  const final: Record<string, PlanNode> = {};
  for (const [k, n] of Object.entries(scenario.plan.nodes)) final[k] = clone(n);
  const run: Run = {
    id,
    workspace_id: WORKSPACE,
    request,
    title: scenario.title,
    status: "created",
    autonomy,
    intent: null,
    plan: null,
    clarification: null,
    budget: { max_llm_calls: 40, max_tool_calls: 60, max_cost_usd: 0.5, max_replans: 3 },
    metrics: metrics(),
    summary: "",
    error: "",
    created_at: createdAt,
    updated_at: createdAt,
  };
  const rec: Rec = { run, approvals: [], effects: [], events: [], listeners: new Set(), scenario, final, virtual: createdAt, paused: false, resumeTo: null, cancelled: false, driving: false, gen: null };
  emit(rec, "run.created", "system", { request, autonomy });
  rec.virtual = null;
  runs.set(id, rec);
  return rec;
}

function decideAll(rec: Rec, how: "approved" | "rejected" = "approved") {
  const apv = rec.approvals.find((a) => a.status === "pending");
  if (!apv) return { decisions: {}, edits: {} };
  const decisions: Record<string, string> = {};
  for (const it of apv.items) decisions[it.node_id] = it.gate.verdict === "block" ? "rejected" : how;
  resolveApproval(rec, apv, decisions);
  return { decisions, edits: {} };
}

function resolveApproval(rec: Rec, apv: Approval, decisions: Record<string, string>) {
  apv.status = "resolved";
  apv.resolved_at = clock(rec);
  for (const it of apv.items) it.decision = (decisions[it.node_id] as ApprovalItem["decision"]) ?? "rejected";
  emit(rec, "approval.resolved", "system", { approval: apv });
}

// ---------------------------------------------------------------------------
// seed
// ---------------------------------------------------------------------------

function* seedScript(rec: Rec): Generator<number> {
  yield* intake(rec);
  if (rec.run.status === "awaiting_approval") {
    const { decisions } = decideAll(rec);
    yield* commit(rec, decisions, {});
  }
}

function seed() {
  runs.clear();
  memories = [
    { id: "mem_seed_dana", workspace_id: WORKSPACE, kind: "person", text: "Dana Reyes (dana@northwind.com) is Northwind's VP Procurement and escalates late POs directly.", data: {}, source_run_id: null, created_at: NOW - 6 * DAY, uses: 4 },
    { id: "mem_seed_bank", workspace_id: WORKSPACE, kind: "fact", text: "Vendor bank detail changes are never accepted by email; they need a call to the account manager.", data: {}, source_run_id: null, created_at: NOW - 9 * DAY, uses: 2 },
    { id: "mem_seed_pref", workspace_id: WORKSPACE, kind: "preference", text: "Priya prefers customer replies to lead with dates and owners, not apologies.", data: {}, source_run_id: null, created_at: NOW - 12 * DAY, uses: 6 },
  ];

  // Completed QBR recap, ~2h ago
  const qbr = newRec("run_2b81d04c9a17", QBR_REQUEST, "balanced", QBR, NOW - 2 * HOUR - 14 * MIN);
  runSync(qbr, seedScript(qbr), qbr.run.created_at);

  // Paused hiring run, ~50 min ago
  const hiring = newRec("run_9d0e5f1a2c3b", HIRING_REQUEST, "autonomous", HIRING, NOW - 52 * MIN);
  runSync(hiring, seedScript(hiring), hiring.run.created_at, () => hiring.run.plan?.nodes.h4?.status === "succeeded");
  hiring.paused = true;
  hiring.resumeTo = "executing";
  hiring.run.status = "paused";
  hiring.virtual = hiring.run.updated_at + 1200;
  emit(hiring, "log", "system", { level: "warning", message: "Kill switch engaged by Priya. In-flight calls finished; nothing new will start." });
  emit(hiring, "run.status", "system", { status: "paused" });
  hiring.virtual = null;

  // Clarifying run, ~20 min ago
  const clar = newRec("run_c4e19a7b3d52", "Book a review of the AR-300 alpha with the engineering leads.", "balanced", DESIGN, NOW - 21 * MIN);
  runSync(clar, intake(clar), clar.run.created_at);

  // Showcase: inbox triage awaiting approval, ~6 min ago
  const tri = newRec(SHOWCASE_ID, TRIAGE_REQUEST, "balanced", TRIAGE, NOW - 6 * MIN - 20_000);
  runSync(tri, intake(tri), tri.run.created_at);
}

export const SHOWCASE_ID = "run_7f3a9c21e0b4";
const TRIAGE_REQUEST =
  "Triage my inbox. Draft replies to anything urgent, and pay special attention to vendor invoices, especially the Globex one. Summarize what you found and what needs my attention.";
const QBR_REQUEST =
  "Summarize the Northwind QBR meeting from the transcript. Create a Notion page with the decisions and action items, email the recap to everyone who attended, and then schedule a 30-minute follow-up next week at a time when all attendees are free.";
const HIRING_REQUEST =
  "Using the Hiring Pipeline sheet and the #hiring Slack channel, schedule interviews for the two candidates in the onsite stage with the right panelists. Update the sheet with the interview times and post a summary in #hiring.";

seed();

// ---------------------------------------------------------------------------
// HTTP-ish surface
// ---------------------------------------------------------------------------

function summary(r: Run): RunSummary {
  return { id: r.id, title: r.title, request: r.request, status: r.status, autonomy: r.autonomy, created_at: r.created_at, updated_at: r.updated_at, metrics: clone(r.metrics) };
}

function detail(rec: Rec): RunDetail {
  const approval = rec.approvals[rec.approvals.length - 1] ?? null;
  return clone({ run: rec.run, approval, effects: rec.effects });
}

function need(id: string): Rec {
  const rec = runs.get(id);
  if (!rec) throw new ApiError(404, "run not found");
  return rec;
}

const ACTIVE: RunStatus[] = ["created", "planning", "shadowing", "executing", "verifying", "awaiting_approval", "clarifying"];

export async function handle<T>(method: string, path: string, body?: unknown): Promise<T> {
  await sleep(80 + Math.random() * 140);
  const url = new URL(path, "http://mock");
  const p = url.pathname;
  const b = (body ?? {}) as Record<string, unknown>;
  let m: RegExpMatchArray | null;

  if (method === "GET" && p === "/api/health") {
    const h: Health = { ok: true, version: "0.1.0 (mock)", models: { planner: MUSE, judge: JEV, fallback: "laya" }, llm_configured: true, judge_configured: true };
    return h as T;
  }
  if (method === "GET" && p === "/api/config") return clone(MOCK_CONFIG) as T;
  if (method === "GET" && p === "/api/tools") return clone(MOCK_TOOLS) as T;
  if (method === "GET" && p === "/api/runs") {
    return [...runs.values()].map((r) => summary(r.run)).sort((a, z) => z.created_at - a.created_at) as T;
  }
  if (method === "POST" && p === "/api/runs") {
    const request = String(b.request ?? "").trim();
    if (!request) throw new ApiError(422, "request: String should have at least 1 character");
    const autonomy = (["cautious", "balanced", "autonomous"].includes(String(b.autonomy)) ? b.autonomy : "balanced") as Autonomy;
    const scenario = pickScenario(request);
    const rec = newRec(`run_${hex(request + Date.now(), 12)}`, request, autonomy, scenario, Date.now());
    void drive(rec, intake(rec));
    return clone(rec.run) as T;
  }
  if ((m = p.match(/^\/api\/runs\/([^/]+)$/)) && method === "GET") return detail(need(m[1])) as T;
  if ((m = p.match(/^\/api\/runs\/([^/]+)\/events$/)) && method === "GET") {
    const after = Number(url.searchParams.get("after") ?? 0);
    return clone(need(m[1]).events.filter((e) => e.seq > after)) as T;
  }
  if ((m = p.match(/^\/api\/runs\/([^/]+)\/approvals\/([^/]+)$/)) && method === "POST") {
    const rec = need(m[1]);
    const apv = rec.approvals.find((a) => a.id === m![2]);
    if (!apv) throw new ApiError(404, "approval not found");
    if (apv.status !== "pending") throw new ApiError(409, "approval already resolved");
    const decisions = (b.decisions ?? {}) as Record<string, string>;
    const edits = (b.edits ?? {}) as Record<string, Record<string, unknown>>;
    const ids = new Set(apv.items.map((i) => i.node_id));
    const unknown = [...Object.keys(decisions), ...Object.keys(edits)].filter((k) => !ids.has(k));
    if (unknown.length) throw new ApiError(400, `unknown node ids in approval: [${unknown.map((u) => `'${u}'`).join(", ")}]`);
    const blockedApproved = apv.items.filter((i) => i.gate.verdict === "block" && decisions[i.node_id] === "approved");
    if (blockedApproved.length) throw new ApiError(400, `blocked items cannot be approved: ${blockedApproved.map((i) => i.node_id).join(", ")}`);
    for (const [id, e] of Object.entries(edits)) {
      const it = apv.items.find((i) => i.node_id === id);
      if (it) {
        it.args = { ...it.args, ...e };
        it.preview = { ...it.preview, ...e };
      }
    }
    resolveApproval(rec, apv, decisions);
    for (const [id, e] of Object.entries(edits)) if (Object.keys(e).length) apv.items.find((i) => i.node_id === id)!.decision = "edited";
    void drive(rec, commit(rec, decisions, edits));
    return clone(rec.run) as T;
  }
  if ((m = p.match(/^\/api\/runs\/([^/]+)\/clarify$/)) && method === "POST") {
    const rec = need(m[1]);
    if (rec.run.status !== "clarifying") throw new ApiError(409, `run is not waiting for clarification (status: ${rec.run.status})`);
    const answer = String(b.answer ?? "").trim();
    if (!answer) throw new ApiError(422, "answer: String should have at least 1 character");
    rec.run.clarification = { question: rec.run.clarification?.question ?? "", answer };
    emit(rec, "clarification.answered", "planner", { answer });
    rec.run.status = "planning";
    emit(rec, "run.status", "system", { status: "planning" });
    void drive(rec, planAndShadow(rec));
    return clone(rec.run) as T;
  }
  if ((m = p.match(/^\/api\/runs\/([^/]+)\/(pause|resume|cancel|rollback)$/)) && method === "POST") {
    const rec = need(m[1]);
    const action = m[2];
    const st = rec.run.status;
    if (action === "pause") {
      if (!ACTIVE.includes(st)) throw new ApiError(409, `cannot pause a run that is ${st}`);
      rec.paused = true;
      rec.resumeTo = st;
      emit(rec, "log", "system", { level: "warning", message: "Kill switch engaged. In-flight calls finish; nothing new will start." });
      setStatus(rec, "paused");
    } else if (action === "resume") {
      if (st !== "paused") throw new ApiError(409, `run is not paused (status: ${st})`);
      rec.paused = false;
      setStatus(rec, rec.resumeTo ?? "executing");
      if (!rec.driving && rec.gen) void drive(rec, rec.gen);
    } else if (action === "cancel") {
      if (TERMINAL_STATUSES.has(st)) throw new ApiError(409, `run already ${st}`);
      rec.cancelled = true;
      rec.paused = false;
      for (const n of Object.values(rec.run.plan?.nodes ?? {})) if (["pending", "ready", "running", "awaiting_approval"].includes(n.status)) n.status = "cancelled";
      const apv = rec.approvals.find((a) => a.status === "pending");
      if (apv) {
        apv.status = "resolved";
        apv.resolved_at = Date.now();
        apv.note = "run cancelled";
      }
      setStatus(rec, "cancelled");
    } else {
      const applied = rec.effects.filter((e) => e.status === "applied" && e.compensation);
      if (!applied.length) throw new ApiError(409, "nothing to roll back: no applied reversible effects");
      if (["executing", "verifying"].includes(st)) throw new ApiError(409, "pause the run before rolling back");
      rec.cancelled = true;
      void (async () => {
        for (const fx of [...applied].sort((a, z) => (z.applied_at ?? 0) - (a.applied_at ?? 0))) {
          await sleep(500);
          fx.status = "compensated";
          fx.compensated_at = Date.now();
          const node = rec.run.plan?.nodes[fx.node_id];
          if (node) node.status = "compensated";
          emit(rec, "effect.compensated", "executor", { effect: fx }, fx.node_id);
          if (node) emit(rec, "node.status", "executor", { status: "compensated", attempts: node.attempts }, node.id);
        }
        await sleep(300);
        setStatus(rec, "rolled_back");
      })();
    }
    return clone(rec.run) as T;
  }
  if (method === "GET" && p === "/api/workspace") return clone(world) as T;
  if (method === "POST" && p === "/api/workspace/reset") {
    world = buildWorld();
    return clone(world) as T;
  }
  if (method === "GET" && p === "/api/memory") return clone(memories) as T;
  if ((m = p.match(/^\/api\/memory\/([^/]+)$/)) && method === "DELETE") {
    if (!memories.some((x) => x.id === m![1])) throw new ApiError(404, "memory not found");
    memories = memories.filter((x) => x.id !== m![1]);
    return { ok: true } as T;
  }
  throw new ApiError(404, `mock: no route for ${method} ${p}`);
}

export function subscribe(runId: string, after: number, cb: (e: RunEvent) => void): () => void {
  const rec = runs.get(runId);
  if (!rec) return () => {};
  for (const e of rec.events) if (e.seq > after) setTimeout(() => cb(clone(e)), 0);
  const l = (e: RunEvent) => cb(clone(e));
  rec.listeners.add(l);
  return () => rec.listeners.delete(l);
}

export const _mock = { runs, SHOWCASE_ID };
export type { Rec };

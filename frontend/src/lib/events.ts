import { compact, ms, pct, usd } from "./format";
import { NODE_STATUS, VERDICT } from "./semantics";
import type { Approval, EffectRecord, GateDecision, NodeStatus, Plan, RecoveryDecision, RunEvent, RunStatus, ToolResult, Verification } from "./types";

export interface HumanEvent {
  text: string;
  detail?: string;
  /** model / latency / tokens / cost for llm and judgment calls */
  call?: { model: string; latency: number; tokens: string; cost: number; kind: "llm" | "judgment" };
  tone?: "auto" | "ask" | "block" | "run" | "shadow" | "undo" | "neutral";
  /** routine bookkeeping, rendered muted */
  quiet?: boolean;
}

const STATUS_WORD: Partial<Record<RunStatus, string>> = {
  planning: "Planning",
  shadowing: "Shadow run started: reads are real, writes are simulated",
  awaiting_approval: "Waiting for your review",
  executing: "Committing approved changes",
  verifying: "Checking proof of done",
  paused: "Paused: kill switch engaged",
  completed: "Run completed",
  failed: "Run failed",
  cancelled: "Run cancelled",
  rolled_back: "Rolled back",
  clarifying: "Waiting for your answer",
};

const PURPOSE: Record<string, string> = {
  intent: "parse intent",
  plan: "build plan tree",
  replan: "re-plan subtree",
  draft: "draft",
  reflect: "reflect",
  repair_args: "repair arguments",
  summary: "write summary",
  extract_memories: "extract memories",
  needs_clarification: "needs clarification?",
  rank_memories: "rank memories",
  scan_untrusted: "injection scan",
  gate_action: "gate actions",
  classify_failure: "classify failure",
  verify: "proof of done",
  infer_tool_effect: "infer tool effect",
  summarize: "summarize",
  extract: "extract",
};

function purpose(p: unknown): string {
  const s = String(p ?? "");
  return PURPOSE[s] ?? s.replace(/_/g, " ");
}

export function humanizeEvent(ev: RunEvent, plan: Plan | null | undefined): HumanEvent {
  const d = ev.data ?? {};
  const node = ev.node_id ? plan?.nodes?.[ev.node_id] : undefined;
  const nodeName = node ? node.title : ev.node_id ?? "";
  switch (ev.type) {
    case "run.created":
      return { text: "Run created", detail: d.autonomy ? `${String(d.autonomy)} autonomy` : undefined };
    case "run.status": {
      const s = d.status as RunStatus;
      return { text: STATUS_WORD[s] ?? `Status: ${s}`, tone: s === "completed" ? "auto" : s === "failed" ? "block" : s === "awaiting_approval" || s === "clarifying" ? "ask" : undefined };
    }
    case "intent.parsed": {
      const i = d.intent as { goal?: string } | undefined;
      return { text: "Parsed the request", detail: i?.goal };
    }
    case "clarification.requested":
      return { text: "Asked you a question", detail: String(d.question ?? ""), tone: "ask" };
    case "clarification.answered":
      return { text: "You answered", detail: String(d.answer ?? "") };
    case "memory.recalled": {
      const items = (d.items as { text: string; p?: number }[]) ?? [];
      return { text: `Recalled ${items.length} relevant ${items.length === 1 ? "memory" : "memories"}`, detail: items[0]?.text };
    }
    case "plan.created": {
      const p = d.plan as Plan | undefined;
      const nodes = Object.values(p?.nodes ?? {});
      const actions = nodes.filter((n) => n.kind === "action").length;
      return { text: `Planned ${actions} actions across ${nodes.length - actions} goals` };
    }
    case "plan.revised": {
      const replaced = (d.replaced as string[]) ?? [];
      const added = (d.added as string[]) ?? [];
      return {
        text: `Re-planned: replaced ${replaced.join(", ") || "nothing"}, added ${added.join(", ") || "nothing"}`,
        detail: d.reason ? String(d.reason) : undefined,
        tone: "shadow",
      };
    }
    case "node.status": {
      const s = d.status as NodeStatus;
      const meta = NODE_STATUS[s];
      const loud = s === "failed" || s === "blocked" || s === "awaiting_approval" || s === "compensated";
      return { text: `${nodeName}: ${meta?.label.toLowerCase() ?? s}`, tone: loud ? (meta?.tone as HumanEvent["tone"]) : undefined, quiet: !loud };
    }
    case "node.started":
      return { text: `Started ${String(d.tool ?? node?.tool ?? "")}`, detail: d.mode === "shadow" ? "shadow mode: simulate, don't apply" : d.attempt ? `attempt ${String(d.attempt)}` : undefined };
    case "node.result": {
      const r = d.result as ToolResult | undefined;
      if (!r) return { text: "Result" };
      if (!r.ok) return { text: `${node?.tool ?? "Tool"} failed`, detail: r.error ? `${r.error.kind}: ${r.error.message}` : undefined, tone: "block" };
      if (r.simulated) return { text: `Simulated ${node?.tool ?? ""}: preview captured, nothing changed`, tone: "shadow" };
      return { text: `${node?.tool ?? "Tool"} returned`, detail: `${ms(r.latency_ms)}${r.tainted ? " · output is untrusted" : ""}` };
    }
    case "node.gated": {
      const g = d.gate as GateDecision | undefined;
      if (!g) return { text: "Gated" };
      return { text: `Gate: ${VERDICT[g.verdict]?.label ?? g.verdict} at ${pct(g.risk)} risk · ${nodeName}`, detail: g.reasons?.[0], tone: VERDICT[g.verdict]?.tone as HumanEvent["tone"] };
    }
    case "node.verified": {
      const v = d.verification as Verification | undefined;
      const passed = v?.checks.filter((c) => c.passed).length ?? 0;
      return { text: `Proof of done · ${nodeName}: ${passed}/${v?.checks.length ?? 0} criteria met`, tone: v?.passed ? "auto" : "block" };
    }
    case "recovery.decided": {
      const r = d.decision as RecoveryDecision | undefined;
      return { text: `Recovery: ${r?.cause ?? "?"} → ${String(r?.strategy ?? "?").replace(/_/g, " ")} (${pct(r?.confidence ?? 0)} confident)`, detail: r?.note, tone: "ask" };
    }
    case "reflection":
      return { text: "Reflection", detail: String(d.text ?? "") };
    case "shadow.completed": {
      const fx = (d.effects as EffectRecord[]) ?? [];
      return { text: `Shadow run finished: ${fx.length} ${fx.length === 1 ? "write" : "writes"} previewed, nothing sent`, tone: "shadow" };
    }
    case "approval.requested": {
      const a = d.approval as Approval | undefined;
      const asks = a?.items.filter((i) => i.gate?.verdict !== "block").length ?? 0;
      return { text: `Plan diff ready: ${asks} ${asks === 1 ? "change needs" : "changes need"} your review`, tone: "ask" };
    }
    case "approval.resolved": {
      const a = d.approval as Approval | undefined;
      const ok = a?.items.filter((i) => i.decision === "approved" || i.decision === "edited").length ?? 0;
      const no = a?.items.filter((i) => i.decision === "rejected").length ?? 0;
      return { text: `Review submitted: ${ok} approved, ${no} rejected` };
    }
    case "effect.recorded": {
      const e = d.effect as EffectRecord | undefined;
      return { text: `Ledger ${e?.status ?? ""}: ${e?.summary ?? ""}`, tone: e?.status === "applied" ? "auto" : e?.status === "simulated" ? "shadow" : undefined };
    }
    case "effect.compensated": {
      const e = d.effect as EffectRecord | undefined;
      return { text: `Compensated: ${e?.summary ?? ""}`, tone: "undo" };
    }
    case "llm.call": {
      const inT = Number(d.input_tokens ?? 0);
      const outT = Number(d.output_tokens ?? 0);
      return {
        text: `Muse · ${purpose(d.purpose)}`,
        call: { kind: "llm", model: String(d.model ?? ""), latency: Number(d.latency_ms ?? 0), tokens: `${compact(inT)} in · ${compact(outT)} out`, cost: Number(d.cost_usd ?? 0) },
      };
    }
    case "judgment.call": {
      const qs = (d.questions as unknown[]) ?? [];
      return {
        text: `Jev · ${purpose(d.purpose)}${qs.length > 1 ? ` · ${qs.length} questions` : ""}`,
        call: { kind: "judgment", model: String(d.model ?? ""), latency: Number(d.latency_ms ?? 0), tokens: `${compact(Number(d.tokens ?? 0))} tok`, cost: Number(d.cost_usd ?? 0) },
      };
    }
    case "budget.exceeded":
      return { text: `Budget exceeded: ${String(d.limit ?? "")}`, detail: d.value != null ? `value ${String(d.value)}` : undefined, tone: "block" };
    case "run.completed":
      return { text: "Summary written", tone: "auto" };
    case "run.failed":
      return { text: "Run failed", detail: String(d.error ?? ""), tone: "block" };
    case "log":
      return { text: String(d.message ?? "log"), tone: d.level === "error" ? "block" : d.level === "warning" ? "ask" : undefined };
    default:
      return { text: String(ev.type) };
  }
}

export function callCostLabel(c: HumanEvent["call"]): string {
  if (!c) return "";
  return `${c.model} · ${ms(c.latency)} · ${c.tokens} · ${usd(c.cost, 5)}`;
}

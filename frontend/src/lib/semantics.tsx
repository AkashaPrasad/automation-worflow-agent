// One vocabulary for state, verdict, agent and app, used on every screen.
import {
  AudioLines,
  Ban,
  Brain,
  CalendarDays,
  Circle,
  CircleCheck,
  CircleDashed,
  CircleDot,
  CircleSlash,
  CircleX,
  Eye,
  FileText,
  Ghost,
  Globe,
  Hand,
  Hash,
  Hourglass,
  LoaderCircle,
  Lock,
  Mail,
  MessageCircleQuestionMark,
  NotebookPen,
  Pause,
  Pencil,
  Play,
  Plug,
  ScanEye,
  Send,
  ShieldBan,
  ShieldCheck,
  SkipForward,
  Sparkles,
  Table2,
  Undo2,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import type { Agent, EffectClass, EffectStatus, NodeStatus, RunStatus, Verdict } from "./types";

export type Tone = "auto" | "ask" | "block" | "run" | "shadow" | "undo" | "taint" | "neutral";

/** Literal class strings (Tailwind scans source; never build these dynamically). */
export const TONE: Record<Tone, { fg: string; bg: string; line: string; solid: string; ring: string }> = {
  auto: { fg: "text-auto-fg", bg: "bg-auto-bg", line: "border-auto-line", solid: "bg-auto-solid", ring: "ring-auto-line" },
  ask: { fg: "text-ask-fg", bg: "bg-ask-bg", line: "border-ask-line", solid: "bg-ask-solid", ring: "ring-ask-line" },
  block: { fg: "text-block-fg", bg: "bg-block-bg", line: "border-block-line", solid: "bg-block-solid", ring: "ring-block-line" },
  run: { fg: "text-run-fg", bg: "bg-run-bg", line: "border-run-line", solid: "bg-run-solid", ring: "ring-run-line" },
  shadow: { fg: "text-shadow-fg", bg: "bg-shadow-bg", line: "border-shadow-line", solid: "bg-shadow-solid", ring: "ring-shadow-line" },
  undo: { fg: "text-undo-fg", bg: "bg-undo-bg", line: "border-undo-line", solid: "bg-undo-solid", ring: "ring-undo-line" },
  taint: { fg: "text-taint-fg", bg: "bg-taint-bg", line: "border-taint-line", solid: "bg-taint-solid", ring: "ring-taint-line" },
  neutral: { fg: "text-neutral-fg", bg: "bg-neutral-bg", line: "border-neutral-line", solid: "bg-neutral-solid", ring: "ring-neutral-line" },
};

interface Sem {
  tone: Tone;
  label: string;
  icon: LucideIcon;
  live?: boolean;
  hint?: string;
}

export const NODE_STATUS: Record<NodeStatus, Sem> = {
  pending: { tone: "neutral", label: "Pending", icon: CircleDashed, hint: "Waiting for its dependencies." },
  ready: { tone: "neutral", label: "Ready", icon: CircleDot, hint: "Dependencies met; next to be scheduled." },
  running: { tone: "run", label: "Running", icon: LoaderCircle, live: true, hint: "Tool call in flight." },
  simulated: { tone: "shadow", label: "Simulated", icon: Ghost, hint: "Shadow run: the write was previewed, nothing changed yet." },
  awaiting_approval: { tone: "ask", label: "Awaiting approval", icon: Hourglass, hint: "Held for your review." },
  succeeded: { tone: "auto", label: "Succeeded", icon: CircleCheck },
  failed: { tone: "block", label: "Failed", icon: CircleX },
  skipped: { tone: "neutral", label: "Skipped", icon: SkipForward },
  blocked: { tone: "block", label: "Blocked", icon: Ban, hint: "Refused by policy. It cannot run." },
  compensated: { tone: "undo", label: "Compensated", icon: Undo2, hint: "Undone during rollback." },
  cancelled: { tone: "neutral", label: "Cancelled", icon: CircleSlash },
};

export const RUN_STATUS: Record<RunStatus, Sem> = {
  created: { tone: "neutral", label: "Created", icon: Circle, live: true },
  clarifying: { tone: "ask", label: "Needs an answer", icon: MessageCircleQuestionMark },
  planning: { tone: "run", label: "Planning", icon: Brain, live: true },
  shadowing: { tone: "shadow", label: "Shadow run", icon: Ghost, live: true },
  awaiting_approval: { tone: "ask", label: "Awaiting approval", icon: Hourglass },
  executing: { tone: "run", label: "Executing", icon: Play, live: true },
  verifying: { tone: "run", label: "Verifying", icon: ScanEye, live: true },
  paused: { tone: "neutral", label: "Paused", icon: Pause },
  completed: { tone: "auto", label: "Completed", icon: CircleCheck },
  failed: { tone: "block", label: "Failed", icon: CircleX },
  cancelled: { tone: "neutral", label: "Cancelled", icon: CircleSlash },
  rolled_back: { tone: "undo", label: "Rolled back", icon: Undo2 },
};

export const VERDICT: Record<Verdict, Sem> = {
  auto: { tone: "auto", label: "AUTO", icon: ShieldCheck, hint: "Runs without asking: the gate judged it safe at this autonomy level." },
  ask: { tone: "ask", label: "ASK", icon: Hand, hint: "Needs your approval before it runs." },
  block: { tone: "block", label: "BLOCK", icon: ShieldBan, hint: "Refused by policy. Cannot be approved." },
};

export const EFFECT_STATUS: Record<EffectStatus, Sem> = {
  intent: { tone: "neutral", label: "Intent", icon: CircleDashed, hint: "Recorded before the call (transactional outbox)." },
  simulated: { tone: "shadow", label: "Simulated", icon: Ghost, hint: "Previewed in the shadow run; not applied." },
  applied: { tone: "auto", label: "Applied", icon: CircleCheck },
  failed: { tone: "block", label: "Failed", icon: CircleX },
  unknown: { tone: "ask", label: "Unknown", icon: MessageCircleQuestionMark, hint: "Could not reconcile after a crash; needs a human." },
  compensated: { tone: "undo", label: "Compensated", icon: Undo2 },
};

export const EFFECT_CLASS: Record<EffectClass, Sem & { short: string; order: number }> = {
  communicate: { tone: "ask", label: "Sends to people", short: "Communicate", icon: Send, order: 0, hint: "Delivered to other humans. Cannot be unsent." },
  write_irreversible: { tone: "block", label: "Permanent changes", short: "Irreversible", icon: Lock, order: 1, hint: "Cannot be undone." },
  write_reversible: { tone: "undo", label: "Reversible changes", short: "Reversible", icon: Pencil, order: 2, hint: "Has a compensation; can be rolled back." },
  read: { tone: "neutral", label: "Reads", short: "Read", icon: Eye, order: 3, hint: "No side effects." },
};

export const AGENTS: Agent[] = ["planner", "executor", "evaluator", "guardian", "system"];

export const AGENT: Record<Agent, { label: string; model: string; dot: string; text: string; border: string; describe: string }> = {
  planner: {
    label: "Planner",
    model: "Muse",
    dot: "bg-agent-planner",
    text: "text-agent-planner",
    border: "border-agent-planner",
    describe: "Intent, plan, re-plan, reflection and the final summary (System 2).",
  },
  executor: {
    label: "Executor",
    model: "Code + Muse",
    dot: "bg-agent-executor",
    text: "text-agent-executor",
    border: "border-agent-executor",
    describe: "Scheduling, tool calls, drafting and argument repair.",
  },
  evaluator: {
    label: "Evaluator",
    model: "Jev",
    dot: "bg-agent-evaluator",
    text: "text-agent-evaluator",
    border: "border-agent-evaluator",
    describe: "Proof-of-done checks and memory ranking (System 1).",
  },
  guardian: {
    label: "Guardian",
    model: "Jev",
    dot: "bg-agent-guardian",
    text: "text-agent-guardian",
    border: "border-agent-guardian",
    describe: "Action gate, injection scan and failure triage (System 1).",
  },
  system: {
    label: "System",
    model: "Code",
    dot: "bg-agent-system",
    text: "text-agent-system",
    border: "border-agent-system",
    describe: "Lifecycle, budget and ledger bookkeeping.",
  },
};

const APP_META: Record<string, { label: string; icon: LucideIcon }> = {
  gmail: { label: "Gmail", icon: Mail },
  mail: { label: "Mail", icon: Mail },
  calendar: { label: "Calendar", icon: CalendarDays },
  docs: { label: "Docs", icon: FileText },
  sheets: { label: "Sheets", icon: Table2 },
  notion: { label: "Notion", icon: NotebookPen },
  slack: { label: "Slack", icon: Hash },
  meetings: { label: "Meetings", icon: AudioLines },
  web: { label: "Web", icon: Globe },
  llm: { label: "Muse", icon: Sparkles },
  memory: { label: "Memory", icon: Brain },
};

export function appOf(tool: string | null | undefined): string {
  if (!tool) return "";
  if (tool.startsWith("mcp:")) return tool.split(".")[0];
  return tool.split(".")[0];
}

export function appMeta(appOrTool: string | null | undefined): { label: string; icon: LucideIcon } {
  const app = appOf(appOrTool);
  if (APP_META[app]) return APP_META[app];
  if (app.startsWith("mcp:")) return { label: app.slice(4) || "MCP", icon: Plug };
  return { label: app || "Tool", icon: Wrench };
}

/** Human names for gate signals, in display order. */
export const SIGNALS: { key: string; label: string; good: "high" | "low"; hint: string }[] = [
  { key: "alignment", label: "Alignment", good: "high", hint: "Jev: does this action serve what you asked for? (Score 0..3, normalised)" },
  { key: "injection", label: "Injection", good: "low", hint: "Jev: probability the arguments were steered by instructions hidden in external content." },
  { key: "sensitive", label: "Sensitive", good: "low", hint: "Jev: probability the content includes sensitive data (credentials, bank details, PII)." },
  { key: "tone_ok", label: "Tone ok", good: "high", hint: "Jev: is the message professional and appropriate for its recipients? (communications only)" },
  { key: "recipients_match", label: "Recipients match", good: "high", hint: "Jev: does your request imply these recipients? (communications only)" },
  { key: "external", label: "External", good: "low", hint: "Code: any recipient outside the internal domain." },
  { key: "unknown_recipient", label: "Unknown recipient", good: "low", hint: "Code: any recipient not in known contacts." },
  { key: "tainted", label: "Tainted", good: "low", hint: "Code: arguments derive from untrusted content (provenance tracking)." },
];

export function signalLabel(key: string): string {
  return SIGNALS.find((s) => s.key === key)?.label ?? key.replace(/_/g, " ");
}

export function signalGood(key: string): "high" | "low" {
  return SIGNALS.find((s) => s.key === key)?.good ?? "low";
}

/** Tone for a signal value: green when on the good side, amber in between, red when concerning. */
export function signalTone(key: string, v: number): Tone {
  const bad = signalGood(key) === "high" ? 1 - v : v;
  if (bad >= 0.7) return "block";
  if (bad >= 0.3) return "ask";
  return "auto";
}

export function riskTone(risk: number): Tone {
  if (risk >= 0.6) return "block";
  if (risk >= 0.35) return "ask";
  return "auto";
}

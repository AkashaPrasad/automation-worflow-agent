import { useState, type KeyboardEvent } from "react";
import {
  BadgeCheck,
  ChevronLeft,
  ChevronRight,
  Database,
  Ghost,
  Hand,
  ListTree,
  MessageSquare,
  RefreshCw,
  ShieldCheck,
  type LucideIcon,
} from "lucide-react";
import { cx } from "../../components/ui/primitives";

type Owner = "muse" | "jev" | "code" | "you";

interface Stage {
  id: string;
  title: string;
  short: string;
  icon: LucideIcon;
  owner: Owner[];
  body: string;
  prevents?: string;
  emits: string[];
}

export const STAGES: Stage[] = [
  {
    id: "intent",
    title: "Intent",
    short: "Parse the ask",
    icon: MessageSquare,
    owner: ["muse", "jev"],
    body: "Muse turns the request into a typed Intent: goal, deliverables, constraints, people, apps and raw time expressions. Jev estimates whether information is missing; above 0.75 Adjutant asks one question instead of guessing.",
    prevents: "Confident plans built on a misread request.",
    emits: ["intent.parsed", "clarification.requested"],
  },
  {
    id: "recall",
    title: "Recall",
    short: "Rank memories",
    icon: Database,
    owner: ["jev"],
    body: "Facts, preferences, people and playbooks from earlier runs are ranked by Jev for relevance. Only memories above the threshold reach the planner, so old context can't crowd out the request.",
    emits: ["memory.recalled"],
  },
  {
    id: "plan",
    title: "Plan tree",
    short: "Goals → actions",
    icon: ListTree,
    owner: ["muse", "code"],
    body: "Muse writes a hierarchical plan as data: goals with checkable success criteria, actions as tool calls, explicit dependencies and templates like {{a2.output.summary}}. Code validates tools, schemas, templates and cycles before anything runs.",
    prevents: "Static plans: the tree is data, so one subtree can be re-planned without touching the rest.",
    emits: ["plan.created"],
  },
  {
    id: "shadow",
    title: "Shadow run",
    short: "Reads real, writes simulated",
    icon: Ghost,
    owner: ["code"],
    body: "A DAG scheduler runs ready nodes in parallel. Reads and llm.* steps run for real; every write calls simulate() and becomes a preview in the ledger. What you approve is exactly what gets sent. Taint is tracked on every value that came from outside.",
    prevents: "Prompt injection riding in unnoticed: every untrusted text is scanned once and its taint follows it.",
    emits: ["node.started", "node.result", "shadow.completed"],
  },
  {
    id: "gate",
    title: "Guardian gate",
    short: "Jev scores, code decides",
    icon: ShieldCheck,
    owner: ["jev", "code"],
    body: "For each simulated write, Jev answers parallel typed questions (alignment, injection, sensitivity, tone, recipients) in one request. policy.py turns those calibrated signals into AUTO, ASK or BLOCK for the chosen autonomy level. The rules live in code, outside the model.",
    prevents: "Irreversible actions taken wrongly, even if the model's context drops a rule.",
    emits: ["judgment.call", "node.gated"],
  },
  {
    id: "approve",
    title: "Plan-diff approval",
    short: "One batched review",
    icon: Hand,
    owner: ["you"],
    body: "Only ASK items reach you, in one review grouped by effect. Each approval is bound to the args hash; edit an email and it is re-gated. BLOCK items are shown with their reasons and cannot be approved.",
    prevents: "Approval fatigue: 93% of prompts get rubber-stamped when everything asks.",
    emits: ["approval.requested", "approval.resolved"],
  },
  {
    id: "commit",
    title: "Commit via ledger",
    short: "Intent → act → applied",
    icon: Database,
    owner: ["code"],
    body: "Writes run in dependency order through a transactional outbox: record intent, call the tool with an idempotency key per (run, node, args), then mark applied. After a crash, reconcile reads the world back before retrying.",
    prevents: "Duplicate side effects on retry.",
    emits: ["effect.recorded", "effect.compensated"],
  },
  {
    id: "verify",
    title: "Proof of done",
    short: "Jev checks criteria",
    icon: BadgeCheck,
    owner: ["jev"],
    body: "Every goal's success criteria are checked by Jev against what the tools actually returned, not against what the model says it did. A criterion counts as met at 0.6 or above.",
    prevents: "False \"done\": about a quarter of agent failures are verification failures.",
    emits: ["node.verified"],
  },
  {
    id: "reflect",
    title: "Reflect / re-plan",
    short: "Fix the subtree",
    icon: RefreshCw,
    owner: ["muse", "jev"],
    body: "Failed criteria or tool errors go to Jev's failure classifier and Muse's reflection. The failing goal's subtree is re-planned while completed-effect nodes are kept; new writes go back through shadow, gate and approval. A budget bounds cost, calls and re-plans.",
    prevents: "Cost blowups and loops: the same tool and args attempted more than twice stops the run.",
    emits: ["recovery.decided", "reflection", "plan.revised"],
  },
];

export const OWNER: Record<Owner, { label: string; dot: string; text: string }> = {
  muse: { label: "Muse", dot: "bg-agent-planner", text: "text-agent-planner" },
  jev: { label: "Jev", dot: "bg-agent-guardian", text: "text-agent-guardian" },
  code: { label: "Code", dot: "bg-ink-2", text: "text-ink-2" },
  you: { label: "You", dot: "bg-ask-solid", text: "text-ask-fg" },
};

const R = 41; // radius in % of the square

function pos(i: number, n: number) {
  const a = (-90 + (i * 360) / n) * (Math.PI / 180);
  return { x: 50 + R * Math.cos(a), y: 50 + R * Math.sin(a) };
}

export function LoopDiagram() {
  const [sel, setSel] = useState(4);
  const s = STAGES[sel];
  const n = STAGES.length;

  const onKey = (e: KeyboardEvent) => {
    if (e.key === "ArrowRight" || e.key === "ArrowDown") {
      e.preventDefault();
      setSel((x) => (x + 1) % n);
    } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
      e.preventDefault();
      setSel((x) => (x - 1 + n) % n);
    }
  };

  const plan = pos(2, n);
  const reflect = pos(8, n);

  return (
    <div className="grid items-center gap-8 lg:grid-cols-[minmax(0,1fr)_minmax(0,0.9fr)]">
      {/* Circle (md+) */}
      <div className="relative mx-auto hidden aspect-square w-full max-w-[560px] md:block" role="tablist" aria-label="Run loop stages" onKeyDown={onKey}>
        <svg viewBox="0 0 100 100" className="absolute inset-0 size-full overflow-visible" aria-hidden>
          <defs>
            <marker id="loop-arrow" viewBox="0 0 6 6" refX="3" refY="3" markerWidth="4" markerHeight="4" orient="auto-start-reverse">
              <path d="M0,0 L6,3 L0,6 z" fill="var(--line-strong)" />
            </marker>
          </defs>
          <circle cx="50" cy="50" r={R} fill="none" stroke="var(--line-strong)" strokeWidth="0.35" />
          <circle id="loop-path" cx="50" cy="50" r={R} fill="none" stroke="var(--accent)" strokeWidth="0.5" strokeDasharray="1 3" opacity="0.5" />
          {/* re-plan chord: reflect -> plan */}
          <path
            d={`M ${reflect.x + 4} ${reflect.y + 2.5} Q 52 16 ${plan.x - 5} ${plan.y - 2.8}`}
            fill="none"
            stroke="var(--shadow-solid)"
            strokeWidth="0.45"
            strokeDasharray="1.4 1.2"
            markerEnd="url(#loop-arrow)"
          />
          <text x="53" y="31" textAnchor="middle" fontSize="2.3" fill="var(--shadow-fg)" style={{ fontFamily: "var(--font-mono)" }}>
            re-plan subtree
          </text>
          {/* the loop's heartbeat */}
          <circle r="1.1" fill="var(--accent)" className="motion-reduce:hidden">
            <animateMotion dur="14s" repeatCount="indefinite" path={`M 50 ${50 - R} A ${R} ${R} 0 1 1 49.99 ${50 - R}`} />
          </circle>
        </svg>
        {STAGES.map((st, i) => {
          const p = pos(i, n);
          const active = i === sel;
          return (
            <button
              key={st.id}
              type="button"
              role="tab"
              aria-selected={active}
              aria-controls="loop-detail"
              tabIndex={active ? 0 : -1}
              onClick={() => setSel(i)}
              className={cx(
                "absolute flex -translate-x-1/2 -translate-y-1/2 items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-left shadow-card transition-[background-color,border-color,transform,box-shadow] duration-200",
                active ? "z-10 scale-105 border-ink bg-inverse text-inverse-ink shadow-pop" : "border-line-strong bg-panel text-ink hover:border-ink-4",
              )}
              style={{ left: `${p.x}%`, top: `${p.y}%` }}
            >
              <st.icon className={cx("size-3.5 shrink-0", active ? "text-inverse-ink" : "text-ink-3")} aria-hidden />
              <span className="whitespace-nowrap text-xs font-semibold">{st.title}</span>
              <span className="flex gap-0.5" aria-hidden>
                {st.owner.map((o) => (
                  <span key={o} className={cx("size-1.5 rounded-full", OWNER[o].dot)} />
                ))}
              </span>
            </button>
          );
        })}
        <div className="pointer-events-none absolute inset-[27%] flex flex-col items-center justify-center text-center">
          <p className="font-mono text-2xs text-ink-3">stage {sel + 1} of {n}</p>
          <p className="mt-1 text-lg font-semibold leading-tight text-ink">{s.title}</p>
          <p className="mt-1 text-xs text-ink-3">{s.short}</p>
        </div>
      </div>

      {/* List (mobile) */}
      <ol className="space-y-1.5 md:hidden" role="tablist" aria-label="Run loop stages">
        {STAGES.map((st, i) => (
          <li key={st.id}>
            <button
              type="button"
              role="tab"
              aria-selected={i === sel}
              onClick={() => setSel(i)}
              className={cx("flex w-full items-center gap-2.5 rounded-lg border px-3 py-2 text-left", i === sel ? "border-ink bg-inverse text-inverse-ink" : "border-line bg-panel text-ink")}
            >
              <span className="w-4 font-mono text-2xs opacity-60">{i + 1}</span>
              <st.icon className="size-4 shrink-0 opacity-70" aria-hidden />
              <span className="text-sm font-semibold">{st.title}</span>
              <span className="ml-auto text-2xs opacity-70">{st.short}</span>
            </button>
          </li>
        ))}
      </ol>

      <div id="loop-detail" role="tabpanel" aria-live="polite" className="rounded-xl border border-line bg-panel p-5 shadow-card">
        <div className="flex items-center gap-2">
          <span className="grid size-8 place-items-center rounded-lg border border-line bg-panel-2 text-ink-2">
            <s.icon className="size-4" aria-hidden />
          </span>
          <div>
            <p className="text-base font-semibold text-ink">{s.title}</p>
            <p className="flex items-center gap-2 text-2xs">
              {s.owner.map((o) => (
                <span key={o} className={cx("inline-flex items-center gap-1 font-medium", OWNER[o].text)}>
                  <span className={cx("size-1.5 rounded-full", OWNER[o].dot)} aria-hidden />
                  {OWNER[o].label}
                </span>
              ))}
            </p>
          </div>
          <div className="ml-auto flex gap-1">
            <button type="button" onClick={() => setSel((x) => (x - 1 + n) % n)} className="grid size-7 place-items-center rounded-md border border-line text-ink-3 hover:text-ink" aria-label="Previous stage">
              <ChevronLeft className="size-3.5" aria-hidden />
            </button>
            <button type="button" onClick={() => setSel((x) => (x + 1) % n)} className="grid size-7 place-items-center rounded-md border border-line text-ink-3 hover:text-ink" aria-label="Next stage">
              <ChevronRight className="size-3.5" aria-hidden />
            </button>
          </div>
        </div>
        <p className="mt-4 text-sm leading-relaxed text-ink-2">{s.body}</p>
        {s.prevents && (
          <p className="mt-3 rounded-lg border border-line bg-panel-2 px-3 py-2 text-xs leading-relaxed text-ink-2">
            <span className="font-semibold text-ink">Addresses:</span> {s.prevents}
          </p>
        )}
        <div className="mt-4">
          <p className="text-2xs font-medium text-ink-3">Emits</p>
          <div className="mt-1.5 flex flex-wrap gap-1">
            {s.emits.map((e) => (
              <span key={e} className="rounded-[4px] border border-line bg-panel-2 px-1.5 py-px font-mono text-[10.5px] text-ink-2">
                {e}
              </span>
            ))}
          </div>
        </div>
        <p className="mt-4 text-2xs text-ink-3">Use the arrow keys to walk the loop.</p>
      </div>
    </div>
  );
}


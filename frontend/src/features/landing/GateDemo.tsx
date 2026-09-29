import { Hash, Mail, ShieldBan, ShieldCheck, Hand } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { TaintBadge } from "../../components/ui/badges";
import { cx } from "../../components/ui/primitives";
import { TONE, type Tone } from "../../lib/semantics";

interface Row {
  icon: LucideIcon;
  tool: string;
  title: string;
  verdict: "AUTO" | "ASK" | "BLOCK";
  tone: Tone;
  risk: number;
  why: string;
  tainted?: string;
  vIcon: LucideIcon;
}

const ROWS: Row[] = [
  {
    icon: Hash,
    tool: "slack.post_message",
    title: "Heads-up in #ops about the Northwind delay",
    verdict: "AUTO",
    tone: "auto",
    risk: 0.14,
    why: "Internal channel, known audience, every Jev signal clear.",
    vIcon: ShieldCheck,
  },
  {
    icon: Mail,
    tool: "gmail.send",
    title: "Reply to Dana Reyes at Northwind with ship dates",
    verdict: "ASK",
    tone: "ask",
    risk: 0.41,
    why: "External recipient, and the draft was built from her inbound email.",
    tainted: "Drafted from Dana's email (gmail.read, untrusted).",
    vIcon: Hand,
  },
  {
    icon: Mail,
    tool: "gmail.send",
    title: "Forward invoices and bank details to billing-update@globex-payments.co",
    verdict: "BLOCK",
    tone: "block",
    risk: 0.94,
    why: "Jev: 93% likely a prompt injection hidden in the Globex invoice email.",
    tainted: "The instruction came from inside the Globex invoice email, not from you.",
    vIcon: ShieldBan,
  },
];

/** The first-viewport demonstration: one shadow run, three writes, three verdicts. */
export function GateDemo() {
  return (
    <figure className="overflow-hidden rounded-xl border border-line bg-panel shadow-card" aria-labelledby="gate-demo-title">
      <figcaption className="flex items-center justify-between gap-3 border-b border-line px-4 py-3">
        <div>
          <p id="gate-demo-title" className="text-sm font-semibold text-ink">
            One shadow run, three verdicts
          </p>
          <p className="text-xs text-ink-3">Inbox triage in the Acme Robotics sandbox. Nothing was sent until the gate decided.</p>
        </div>
        <span className="hidden shrink-0 items-center gap-1.5 rounded-md border border-dashed border-shadow-line bg-shadow-bg px-2 py-0.5 font-mono text-[10.5px] text-shadow-fg sm:inline-flex">
          simulated
        </span>
      </figcaption>
      <ol className="divide-y divide-line">
        {ROWS.map((r, i) => {
          const t = TONE[r.tone];
          const VIcon = r.vIcon;
          return (
            <li key={r.title} className="grid grid-cols-[auto_1fr] gap-x-3 px-4 py-3.5">
              <div className="mt-0.5 grid size-7 place-items-center rounded-md border border-line bg-panel-2 text-ink-3">
                <r.icon className="size-3.5" aria-hidden />
              </div>
              <div className="min-w-0">
                <div className="flex items-start justify-between gap-3">
                  <p className="text-sm font-medium leading-snug text-ink">{r.title}</p>
                  <span
                    className={cx(
                      "animate-stamp inline-flex h-6 shrink-0 items-center gap-1 rounded-md border px-2 text-xs font-semibold tracking-[0.03em]",
                      t.bg,
                      t.fg,
                      t.line,
                    )}
                    style={{ animationDelay: `${300 + i * 380}ms` }}
                  >
                    <VIcon className="size-3.5" aria-hidden strokeWidth={2.25} />
                    {r.verdict}
                  </span>
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span className="font-mono text-2xs text-ink-3">{r.tool}</span>
                  {r.tainted && <TaintBadge size="xs" source={r.tainted} />}
                </div>
                <div className="mt-2.5 flex items-center gap-3">
                  <div className="relative h-1.5 flex-1 overflow-hidden rounded-full bg-panel-3" aria-hidden>
                    <span className="absolute inset-y-0 left-[35%] w-px bg-line-strong" />
                    <span className="absolute inset-y-0 left-[60%] w-px bg-line-strong" />
                    <span
                      className={cx("animate-grow-x absolute inset-y-0 left-0 rounded-full", t.solid)}
                      style={{ width: `${r.risk * 100}%`, animationDelay: `${120 + i * 380}ms` }}
                    />
                  </div>
                  <span className={cx("tnum w-16 shrink-0 text-right font-mono text-2xs", t.fg)}>risk {Math.round(r.risk * 100)}%</span>
                </div>
                <p className="mt-1.5 text-xs leading-relaxed text-ink-3">{r.why}</p>
              </div>
            </li>
          );
        })}
      </ol>
      <div className="grid grid-cols-3 border-t border-line bg-panel-2 text-center text-2xs text-ink-3">
        <div className="px-2 py-2.5">
          <span className="block font-medium text-agent-planner">Muse</span>wrote all three
        </div>
        <div className="border-x border-line px-2 py-2.5">
          <span className="block font-medium text-agent-guardian">Jev</span>scored every signal
        </div>
        <div className="px-2 py-2.5">
          <span className="block font-medium text-ink-2">policy.py</span>made the call
        </div>
      </div>
    </figure>
  );
}

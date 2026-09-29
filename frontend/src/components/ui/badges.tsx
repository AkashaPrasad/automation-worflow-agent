import { Biohazard, RefreshCw } from "lucide-react";
import type { ReactNode } from "react";
import {
  EFFECT_CLASS,
  EFFECT_STATUS,
  NODE_STATUS,
  RUN_STATUS,
  TONE,
  VERDICT,
  appMeta,
  type Tone,
} from "../../lib/semantics";
import type { EffectClass, EffectStatus, NodeStatus, RunStatus, Verdict } from "../../lib/types";
import { pct } from "../../lib/format";
import { cx } from "./primitives";
import { Tooltip } from "./Tooltip";

export function Dot({ tone, live, className }: { tone: Tone; live?: boolean; className?: string }) {
  return (
    <span
      aria-hidden
      className={cx("inline-block size-1.5 shrink-0 rounded-full", TONE[tone].solid, live && "live-dot", className)}
    />
  );
}

export function Pill({
  tone,
  children,
  className,
  title,
  size = "sm",
}: {
  tone: Tone;
  children: ReactNode;
  className?: string;
  title?: string;
  size?: "xs" | "sm";
}) {
  const t = TONE[tone];
  return (
    <span
      title={title}
      className={cx(
        "inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-[5px] border font-medium",
        size === "xs" ? "h-[18px] px-1.5 text-[10.5px]" : "h-5 px-1.5 text-2xs",
        t.bg,
        t.fg,
        t.line,
        className,
      )}
    >
      {children}
    </span>
  );
}

export function NodeStatusBadge({ status, size = "sm" }: { status: NodeStatus; size?: "xs" | "sm" }) {
  const s = NODE_STATUS[status] ?? NODE_STATUS.pending;
  return (
    <Pill tone={s.tone} size={size} title={s.hint}>
      <Dot tone={s.tone} live={s.live} />
      {s.label}
    </Pill>
  );
}

/** Run status. The visible text IS the raw RunStatus value (tests and operators read the same token). */
export function RunStatusBadge({ status, testId, large }: { status: RunStatus; testId?: string; large?: boolean }) {
  const s = RUN_STATUS[status] ?? RUN_STATUS.created;
  const Icon = s.icon;
  const t = TONE[s.tone];
  return (
    <span
      className={cx(
        "inline-flex shrink-0 items-center gap-1.5 rounded-md border font-mono font-medium transition-colors duration-300",
        large ? "h-7 px-2.5 text-xs" : "h-5 px-1.5 text-[10.5px]",
        t.bg,
        t.fg,
        t.line,
      )}
      title={s.label}
    >
      {s.live ? <Dot tone={s.tone} live /> : <Icon className={large ? "size-3.5" : "size-3"} aria-hidden />}
      <span data-testid={testId}>{status}</span>
    </span>
  );
}

export function VerdictBadge({
  verdict,
  risk,
  size = "sm",
  showIcon = true,
}: {
  verdict: Verdict;
  risk?: number;
  size?: "xs" | "sm";
  showIcon?: boolean;
}) {
  const v = VERDICT[verdict] ?? VERDICT.ask;
  const Icon = v.icon;
  return (
    <Pill tone={v.tone} size={size} title={v.hint} className="font-semibold tracking-[0.02em]">
      {showIcon && <Icon className="size-3" aria-hidden strokeWidth={2.25} />}
      {v.label}
      {typeof risk === "number" && (
        <span className="tnum font-mono font-medium opacity-80" aria-label={`risk ${pct(risk)}`}>
          {pct(risk)}
        </span>
      )}
    </Pill>
  );
}

export function EffectStatusBadge({ status }: { status: EffectStatus }) {
  const s = EFFECT_STATUS[status] ?? EFFECT_STATUS.intent;
  return (
    <Pill tone={s.tone} title={s.hint}>
      <Dot tone={s.tone} />
      {s.label}
    </Pill>
  );
}

export function EffectClassTag({ effect }: { effect: EffectClass }) {
  const e = EFFECT_CLASS[effect] ?? EFFECT_CLASS.read;
  const Icon = e.icon;
  return (
    <span className="inline-flex items-center gap-1 whitespace-nowrap text-xs text-ink-2" title={e.hint}>
      <Icon className="size-3.5 text-ink-3" aria-hidden />
      {e.short}
    </span>
  );
}

/**
 * The provenance marker. Hazard stripes mean: this content (or something it was built from)
 * came from outside your control, such as an inbound email, someone else's doc, a transcript or the web.
 */
export function TaintBadge({
  source,
  size = "sm",
  label = "untrusted input",
  compact,
}: {
  source?: ReactNode;
  size?: "xs" | "sm";
  label?: string;
  compact?: boolean;
}) {
  return (
    <Tooltip
      content={
        <span className="block">
          <span className="mb-1 flex items-center gap-1.5 font-semibold text-taint-fg">
            <Biohazard className="size-3.5" aria-hidden /> Tainted by untrusted content
          </span>
          {source ? (
            <span className="block">{source}</span>
          ) : (
            <span className="block">
              These arguments were derived from external content (email bodies, others&rsquo; docs, transcripts or the
              web). Taint propagates through templates and llm.* steps.
            </span>
          )}
          <span className="mt-1 block text-ink-3">
            A tainted write needs a Jev injection check and can never run automatically in balanced mode.
          </span>
        </span>
      }
    >
      <span
        tabIndex={0}
        aria-label={`${label}: tainted by untrusted content`}
        className={cx(
          "taint-stripes inline-flex shrink-0 cursor-help items-center gap-1 whitespace-nowrap rounded-[5px] border border-taint-line font-semibold text-taint-fg",
          size === "xs" ? "h-[18px] px-1.5 text-[10.5px]" : "h-5 px-1.5 text-2xs",
        )}
      >
        <Biohazard className="size-3" aria-hidden strokeWidth={2.25} />
        {!compact && <span className="rounded-[3px] bg-taint-bg px-0.5">{label}</span>}
      </span>
    </Tooltip>
  );
}

export function ReplannedBadge({ revision }: { revision: number }) {
  return (
    <Pill tone="shadow" size="xs" title={`Created by plan revision ${revision} (subtree re-plan).`}>
      <RefreshCw className="size-2.5" aria-hidden strokeWidth={2.5} />
      replanned
    </Pill>
  );
}

export function AppIcon({ tool, className, withLabel }: { tool: string | null | undefined; className?: string; withLabel?: boolean }) {
  const m = appMeta(tool);
  const Icon = m.icon;
  if (withLabel)
    return (
      <span className={cx("inline-flex items-center gap-1.5 text-xs text-ink-2", className)}>
        <Icon className="size-3.5 text-ink-3" aria-hidden />
        {m.label}
      </span>
    );
  return <Icon className={cx("size-3.5 shrink-0", className)} aria-label={m.label} />;
}

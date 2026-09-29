import { useEffect, useMemo, useRef, useState } from "react";
import { Activity, ArrowDown } from "lucide-react";
import type { Agent, Plan, RunEvent } from "../../lib/types";
import { AGENT, AGENTS, TONE } from "../../lib/semantics";
import { elapsed, ms, usd } from "../../lib/format";
import { humanizeEvent } from "../../lib/events";
import type { StreamState } from "../../lib/sse";
import { EmptyState, Skeleton, cx } from "../../components/ui/primitives";

const NOISY = new Set(["node.status"]);

export function Timeline({
  events,
  plan,
  start,
  onSelectNode,
  stream,
  loading,
}: {
  events: RunEvent[];
  plan: Plan | null | undefined;
  start: number;
  onSelectNode: (id: string) => void;
  stream: StreamState;
  loading?: boolean;
}) {
  const [filter, setFilter] = useState<Agent | "all">("all");
  const [compact, setCompact] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);
  const [follow, setFollow] = useState(true);

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const e of events) c[e.agent] = (c[e.agent] ?? 0) + 1;
    return c;
  }, [events]);

  const totals = useMemo(() => {
    let cost = 0;
    let calls = 0;
    for (const e of events)
      if (e.type === "llm.call" || e.type === "judgment.call") {
        cost += Number(e.data?.cost_usd ?? 0);
        calls += 1;
      }
    return { cost, calls };
  }, [events]);

  const shown = useMemo(
    () => events.filter((e) => (filter === "all" || e.agent === filter) && !(compact && NOISY.has(e.type))),
    [events, filter, compact],
  );

  useEffect(() => {
    const el = scroller.current;
    if (el && follow) el.scrollTop = el.scrollHeight;
  }, [shown.length, follow]);

  const onScroll = () => {
    const el = scroller.current;
    if (!el) return;
    setFollow(el.scrollHeight - el.scrollTop - el.clientHeight < 40);
  };

  return (
    <section className="flex h-full min-h-0 flex-col overflow-hidden rounded-xl border border-line bg-panel shadow-card" aria-labelledby="timeline-title">
      <header className="border-b border-line px-4 pb-2 pt-2.5">
        <div className="flex items-center gap-2">
          <h2 id="timeline-title" className="flex items-center gap-2 text-sm font-semibold text-ink">
            <Activity className="size-4 text-ink-3" aria-hidden />
            Trace
          </h2>
          <StreamPill state={stream} />
          <span className="ml-auto hidden text-2xs text-ink-3 sm:inline">
            {events.length} events · {totals.calls} model calls · <span className="tnum font-mono">{usd(totals.cost)}</span>
          </span>
          <button
            type="button"
            onClick={() => setCompact((x) => !x)}
            aria-pressed={compact}
            className={cx(
              "ml-auto inline-flex h-6 items-center rounded-md border px-2 text-2xs font-medium transition-colors sm:ml-2",
              compact ? "border-line-strong bg-panel-3 text-ink" : "border-line text-ink-3 hover:text-ink",
            )}
            title="Hide routine node status changes"
          >
            Compact
          </button>
        </div>
        <div className="scrollbar-none -mx-1 mt-2 flex items-center gap-1 overflow-x-auto px-1 pb-0.5" role="toolbar" aria-label="Filter by agent">
          <Chip active={filter === "all"} onClick={() => setFilter("all")} label="All" count={events.length} />
          {AGENTS.map((a) => (
            <Chip key={a} active={filter === a} onClick={() => setFilter(a)} label={AGENT[a].label} count={counts[a] ?? 0} dot={AGENT[a].dot} />
          ))}
        </div>
      </header>

      <div className="relative min-h-0 flex-1">
        <div ref={scroller} onScroll={onScroll} className="h-full overflow-y-auto" data-testid="event-timeline" aria-label="Run events" role="log" aria-live="off">
          {loading && !events.length ? (
            <div className="space-y-3 p-4">
              {[0, 1, 2, 3, 4, 5].map((i) => (
                <Skeleton key={i} className="h-8" />
              ))}
            </div>
          ) : !shown.length ? (
            <EmptyState icon={Activity} title={events.length ? "No events for this filter" : "Waiting for the first event"}>
              {events.length ? "Pick another agent above." : "Planner, executor, evaluator and guardian activity streams in here as it happens."}
            </EmptyState>
          ) : (
            <ol className="py-1">
              {shown.map((e) => (
                <EventRow key={e.seq} ev={e} plan={plan} start={start} onSelectNode={onSelectNode} />
              ))}
            </ol>
          )}
        </div>
        {!follow && shown.length > 0 && (
          <button
            type="button"
            onClick={() => {
              setFollow(true);
              const el = scroller.current;
              if (el) el.scrollTop = el.scrollHeight;
            }}
            className="animate-rise absolute bottom-3 left-1/2 inline-flex -translate-x-1/2 items-center gap-1 rounded-full border border-line-strong bg-panel px-3 py-1 text-2xs font-medium text-ink-2 shadow-pop hover:text-ink"
          >
            <ArrowDown className="size-3" aria-hidden /> Follow live
          </button>
        )}
      </div>
    </section>
  );
}

function Chip({ active, onClick, label, count, dot }: { active: boolean; onClick: () => void; label: string; count: number; dot?: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cx(
        "inline-flex h-6 shrink-0 items-center gap-1.5 rounded-md border px-2 text-2xs font-medium transition-colors duration-150",
        active ? "border-line-strong bg-panel-3 text-ink" : "border-transparent text-ink-3 hover:bg-panel-3 hover:text-ink-2",
      )}
    >
      {dot && <span className={cx("size-1.5 rounded-full", dot)} aria-hidden />}
      {label}
      <span className="tnum font-mono text-[10px] text-ink-4">{count}</span>
    </button>
  );
}

function StreamPill({ state }: { state: StreamState }) {
  const map: Record<StreamState, { label: string; cls: string; live?: boolean }> = {
    connecting: { label: "connecting", cls: "text-ink-3" },
    live: { label: "live", cls: "text-run-fg", live: true },
    reconnecting: { label: "reconnecting", cls: "text-ask-fg" },
    closed: { label: "final", cls: "text-ink-3" },
  };
  const m = map[state];
  return (
    <span className={cx("inline-flex items-center gap-1.5 font-mono text-[10.5px]", m.cls)}>
      <span className={cx("size-1.5 rounded-full", m.live ? "live-dot bg-run-solid" : state === "reconnecting" ? "bg-ask-solid" : "bg-neutral-solid")} aria-hidden />
      {m.label}
    </span>
  );
}

function Lanes({ agent }: { agent: Agent }) {
  return (
    <span className="relative flex h-full w-[46px] shrink-0 items-stretch justify-between px-[3px]" aria-hidden>
      {AGENTS.map((a) => (
        <span key={a} className="relative flex w-[6px] justify-center">
          <span className="absolute inset-y-0 w-px bg-line" />
          {a === agent && <span className={cx("relative mt-[7px] size-[6px] rounded-full ring-2 ring-panel", AGENT[a].dot)} />}
        </span>
      ))}
    </span>
  );
}

function EventRow({ ev, plan, start, onSelectNode }: { ev: RunEvent; plan: Plan | null | undefined; start: number; onSelectNode: (id: string) => void }) {
  const h = humanizeEvent(ev, plan);
  const clickable = !!ev.node_id && !!plan?.nodes?.[ev.node_id];
  const toneCls = h.tone ? TONE[h.tone].fg : "text-ink";
  const body = (
    <>
      <Lanes agent={ev.agent} />
      <span className="w-[46px] shrink-0 pt-[3px] font-mono text-[10.5px] text-ink-4">{elapsed(ev.ts, start)}</span>
      <span className="min-w-0 flex-1 py-[2px]">
        <span className={cx("block text-[12.5px] leading-snug", h.tone ? toneCls : h.quiet ? "text-ink-3" : "text-ink")}>
          <span className="sr-only">{AGENT[ev.agent]?.label}: </span>
          {h.text}
        </span>
        {h.detail && <span className="mt-0.5 line-clamp-2 text-xs leading-snug text-ink-3">{h.detail}</span>}
        {h.call && (
          <span className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 font-mono text-[10.5px] text-ink-3">
            <span className={cx("rounded-[4px] px-1", h.call.kind === "llm" ? "bg-accent-soft text-accent" : "bg-panel-3 text-agent-guardian")}>{h.call.model}</span>
            <span>{ms(h.call.latency)}</span>
            <span>{h.call.tokens}</span>
            <span className="text-ink-2">{usd(h.call.cost, 5)}</span>
          </span>
        )}
      </span>
      {ev.node_id && <span className="shrink-0 pt-[3px] font-mono text-[10px] text-ink-4">{ev.node_id}</span>}
    </>
  );
  return (
    <li data-testid="event-item" data-type={ev.type} data-agent={ev.agent} data-seq={ev.seq}>
      {clickable ? (
        <button type="button" onClick={() => onSelectNode(ev.node_id!)} className="flex w-full items-stretch gap-2 px-3 py-1 text-left transition-colors hover:bg-panel-2">
          {body}
        </button>
      ) : (
        <div className="flex items-stretch gap-2 px-3 py-1">{body}</div>
      )}
    </li>
  );
}

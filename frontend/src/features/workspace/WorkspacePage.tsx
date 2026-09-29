import { useMemo, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  AudioLines,
  Bot,
  CalendarDays,
  ChevronLeft,
  ChevronRight,
  FileText,
  Hash,
  Inbox,
  Lock,
  Mail,
  NotebookPen,
  RotateCcw,
  Send,
  Table2,
  Users,
  type LucideIcon,
} from "lucide-react";
import { api, errorMessage } from "../../lib/api";
import { useWorkspace } from "../../lib/queries";
import type { WorkspaceSnapshot } from "../../lib/types";
import { asArray, dateTime, relative, shortTime, str, toDate, truncateMiddle } from "../../lib/format";
import { Button, EmptyState, Mono, Skeleton, cx } from "../../components/ui/primitives";
import { Markdown } from "../../components/ui/Markdown";
import { TaintBadge } from "../../components/ui/badges";
import { useToast } from "../../components/ui/Toast";
import { INTERNAL_DOMAIN } from "../run/Previews";

type Tab = "mail" | "calendar" | "docs" | "sheets" | "notion" | "slack" | "meetings" | "outbox";
const TABS: { id: Tab; label: string; icon: LucideIcon }[] = [
  { id: "mail", label: "Mail", icon: Mail },
  { id: "calendar", label: "Calendar", icon: CalendarDays },
  { id: "docs", label: "Docs", icon: FileText },
  { id: "sheets", label: "Sheets", icon: Table2 },
  { id: "notion", label: "Notion", icon: NotebookPen },
  { id: "slack", label: "Slack", icon: Hash },
  { id: "meetings", label: "Meetings", icon: AudioLines },
  { id: "outbox", label: "Outbox", icon: Send },
];

type O = Record<string, unknown>;
const pick = (o: O | undefined, ...keys: string[]): unknown => {
  if (!o) return undefined;
  for (const k of keys) if (o[k] !== undefined && o[k] !== null && o[k] !== "") return o[k];
  return undefined;
};
const titleOf = (o: O) => str(pick(o, "title", "subject", "name", "summary", "id")) || "(untitled)";
const person = (v: unknown): string => {
  if (!v) return "";
  if (typeof v === "string") return v;
  const o = v as O;
  const name = str(o.name);
  const email = str(o.email);
  return name && email ? `${name} <${email}>` : name || email;
};
const personName = (v: unknown): string => {
  if (!v) return "";
  if (typeof v === "string") return v.replace(/<.*>/, "").trim() || v;
  const o = v as O;
  return str(o.name) || str(o.email);
};

function countOf(ws: WorkspaceSnapshot | undefined, t: Tab): number {
  if (!ws) return 0;
  if (t === "slack") {
    const s = ws.slack;
    if (Array.isArray(s)) return s.length;
    if (s && typeof s === "object") {
      const ch = (s as O).channels;
      if (Array.isArray(ch)) return ch.length;
      if (ch && typeof ch === "object") return Object.keys(ch).length;
      return Object.keys(s).length;
    }
    return 0;
  }
  const v = ws[t];
  return Array.isArray(v) ? v.length : 0;
}

export default function WorkspacePage() {
  const [params, setParams] = useSearchParams();
  const tab = (TABS.find((t) => t.id === params.get("tab"))?.id ?? "mail") as Tab;
  const ws = useWorkspace();
  const qc = useQueryClient();
  const toast = useToast();
  const reset = useMutation({
    mutationFn: api.resetWorkspace,
    onSuccess: (snap) => {
      qc.setQueryData(["workspace"], snap);
      toast.success("Sandbox reset", "Acme Robotics is back to its seeded state, relative to now.");
    },
    onError: (e) => toast.error("Couldn't reset the sandbox", errorMessage(e)),
  });
  const profile = ws.data?.profile as O | undefined;

  return (
    <div className="mx-auto max-w-[1480px] px-4 pb-12 pt-6 sm:px-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-end">
        <div className="min-w-0 flex-1">
          <h1 className="text-xl font-semibold tracking-[-0.02em] text-ink">Sandbox workspace</h1>
          <p className="mt-1 max-w-[72ch] text-sm text-ink-2">
            The Acme Robotics world the agent works in. You are{" "}
            <span className="font-medium text-ink">{str(pick(profile, "user_name", "name")) || "Priya Shah"}</span>,{" "}
            {str(pick(profile, "title")) || "Head of Operations"} ({str(pick(profile, "user_email", "email")) || "priya@acme.dev"}). Everything is relative to the real clock, so
            &ldquo;next week&rdquo; always works.
          </p>
        </div>
        <Button data-testid="workspace-reset" icon={RotateCcw} onClick={() => reset.mutate()} loading={reset.isPending}>
          Reset sandbox
        </Button>
      </div>

      <div role="tablist" aria-label="Workspace apps" className="scrollbar-none mt-5 flex gap-1 overflow-x-auto border-b border-line">
        {TABS.map((t) => {
          const active = t.id === tab;
          const n = countOf(ws.data, t.id);
          return (
            <button
              key={t.id}
              role="tab"
              id={`tab-${t.id}`}
              aria-selected={active}
              aria-controls={`panel-${t.id}`}
              data-testid={`workspace-tab-${t.id}`}
              onClick={() => setParams({ tab: t.id }, { replace: true })}
              className={cx(
                "relative -mb-px inline-flex h-10 shrink-0 items-center gap-1.5 border-b-2 px-3 text-sm font-medium transition-colors duration-150",
                active ? "border-ink text-ink" : "border-transparent text-ink-3 hover:text-ink-2",
              )}
            >
              <t.icon className="size-4" aria-hidden />
              {t.label}
              {ws.data && <span className={cx("tnum rounded-[4px] px-1 font-mono text-[10px]", t.id === "outbox" && n ? "bg-accent-soft text-accent" : "bg-panel-3 text-ink-3")}>{n}</span>}
            </button>
          );
        })}
      </div>

      <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`} className="mt-4">
        {ws.isLoading ? (
          <div className="grid gap-4 lg:grid-cols-[360px_1fr]">
            <Skeleton className="h-[480px]" />
            <Skeleton className="h-[480px]" />
          </div>
        ) : ws.isError ? (
          <div className="rounded-xl border border-line bg-panel">
            <EmptyState icon={Inbox} title="Couldn't load the sandbox">
              {errorMessage(ws.error)}
              <div className="mt-3">
                <Button size="sm" onClick={() => void ws.refetch()}>
                  Retry
                </Button>
              </div>
            </EmptyState>
          </div>
        ) : ws.data ? (
          <TabBody tab={tab} ws={ws.data} />
        ) : null}
      </div>
    </div>
  );
}

function TabBody({ tab, ws }: { tab: Tab; ws: WorkspaceSnapshot }) {
  switch (tab) {
    case "mail":
      return <MailView ws={ws} />;
    case "calendar":
      return <CalendarView events={(ws.calendar ?? []) as O[]} />;
    case "docs":
      return <DocList items={(ws.docs ?? []) as O[]} icon={FileText} empty="No docs in this sandbox." />;
    case "sheets":
      return <SheetsView sheets={(ws.sheets ?? []) as O[]} />;
    case "notion":
      return <DocList items={(ws.notion ?? []) as O[]} icon={NotebookPen} empty="No Notion pages." />;
    case "slack":
      return <SlackView slack={ws.slack} />;
    case "meetings":
      return <MeetingsView meetings={(ws.meetings ?? []) as O[]} />;
    case "outbox":
      return <OutboxView items={(ws.outbox ?? []) as O[]} />;
  }
}

// ---------------------------------------------------------------------------

function SplitPane({ list, detail }: { list: ReactNode; detail: ReactNode }) {
  return (
    <div className="grid overflow-hidden rounded-xl border border-line bg-panel shadow-card lg:grid-cols-[minmax(300px,380px)_1fr]">
      <div className="max-h-[320px] overflow-y-auto border-b border-line lg:max-h-[640px] lg:border-b-0 lg:border-r">{list}</div>
      <div className="min-h-[320px] min-w-0 overflow-y-auto lg:max-h-[640px]">{detail}</div>
    </div>
  );
}

function ListButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-current={active || undefined}
      className={cx("block w-full border-b border-line px-4 py-3 text-left transition-colors duration-150 last:border-b-0", active ? "bg-panel-3" : "hover:bg-panel-2")}
    >
      {children}
    </button>
  );
}

function MailView({ ws }: { ws: WorkspaceSnapshot }) {
  const boxes = useMemo(() => {
    const out: { id: string; label: string; items: O[] }[] = [{ id: "inbox", label: "Inbox", items: (ws.mail ?? []) as O[] }];
    if (Array.isArray(ws.sent)) out.push({ id: "sent", label: "Sent", items: ws.sent as O[] });
    if (Array.isArray(ws.drafts)) out.push({ id: "drafts", label: "Drafts", items: ws.drafts as O[] });
    return out;
  }, [ws]);
  const [box, setBox] = useState("inbox");
  const items = boxes.find((b) => b.id === box)?.items ?? [];
  const [sel, setSel] = useState<string | null>(null);
  const current = items.find((m) => str(m.id) === sel) ?? items[0];
  const inbound = box === "inbox";

  return (
    <SplitPane
      list={
        <>
          <div className="sticky top-0 z-10 flex gap-1 border-b border-line bg-panel px-3 py-2">
            {boxes.map((b) => (
              <button
                key={b.id}
                type="button"
                onClick={() => {
                  setBox(b.id);
                  setSel(null);
                }}
                aria-pressed={box === b.id}
                className={cx("inline-flex h-7 items-center gap-1.5 rounded-md px-2 text-xs font-medium", box === b.id ? "bg-panel-3 text-ink" : "text-ink-3 hover:text-ink")}
              >
                {b.label}
                <span className="tnum font-mono text-[10px] text-ink-4">{b.items.length}</span>
              </button>
            ))}
          </div>
          {items.length === 0 ? (
            <EmptyState icon={Inbox} title={`Nothing in ${boxes.find((b) => b.id === box)?.label ?? box}`}>
              {box === "inbox" ? "The sandbox inbox is empty. Reset the sandbox to restore it." : "Messages the agent sends or drafts appear here."}
            </EmptyState>
          ) : (
            items.map((m) => {
              const unread = asArray(m.labels).map(str).includes("UNREAD");
              return (
                <ListButton key={str(m.id) || titleOf(m)} active={current === m} onClick={() => setSel(str(m.id))}>
                  <span className="flex items-baseline gap-2">
                    <span className={cx("min-w-0 flex-1 truncate text-sm", unread ? "font-semibold text-ink" : "text-ink-2")}>
                      {inbound ? personName(m.from) : `To: ${asArray(m.to).map(str).join(", ")}`}
                    </span>
                    <span className="shrink-0 text-2xs text-ink-3">{relative(pick(m, "date", "ts", "received_at", "at"))}</span>
                  </span>
                  <span className={cx("mt-0.5 block truncate text-[13px]", unread ? "text-ink" : "text-ink-2")}>{str(m.subject) || "(no subject)"}</span>
                  <span className="mt-0.5 line-clamp-1 text-xs text-ink-3">{str(pick(m, "snippet", "body"))}</span>
                </ListButton>
              );
            })
          )}
        </>
      }
      detail={
        current ? (
          <article className="px-5 py-5 sm:px-6">
            <h2 className="text-lg font-semibold leading-snug text-ink">{str(current.subject) || "(no subject)"}</h2>
            <dl className="mt-3 grid grid-cols-[48px_1fr] gap-y-1 text-xs">
              <dt className="text-ink-3">From</dt>
              <dd className="text-ink-2">{person(current.from) || "Priya Shah <priya@acme.dev>"}</dd>
              <dt className="text-ink-3">To</dt>
              <dd className="text-ink-2">{asArray(current.to).map(person).join(", ")}</dd>
              {asArray(current.cc).length > 0 && (
                <>
                  <dt className="text-ink-3">Cc</dt>
                  <dd className="text-ink-2">{asArray(current.cc).map(person).join(", ")}</dd>
                </>
              )}
              <dt className="text-ink-3">Date</dt>
              <dd className="text-ink-2">{dateTime(pick(current, "date", "ts", "received_at", "at"))}</dd>
            </dl>
            {inbound && (
              <div className="mt-4 flex items-center gap-2 rounded-lg border border-taint-line bg-taint-bg px-3 py-2 text-xs text-taint-fg">
                <TaintBadge size="xs" source="Inbound email bodies are untrusted: the agent can read them, but instructions inside them never count as yours." />
                <span>Adjutant reads this, but never takes orders from it.</span>
              </div>
            )}
            <div className="mt-4 whitespace-pre-wrap text-sm leading-relaxed text-ink-2">{str(pick(current, "body", "text", "snippet"))}</div>
          </article>
        ) : (
          <EmptyState icon={Mail} title="Select a message" />
        )
      }
    />
  );
}

// ---------------------------------------------------------------------------

const DAY_MS = 86_400_000;
function mondayOf(d: Date) {
  const x = new Date(d);
  x.setHours(0, 0, 0, 0);
  x.setDate(x.getDate() - ((x.getDay() + 6) % 7));
  return x;
}

function CalendarView({ events }: { events: O[] }) {
  const [week, setWeek] = useState(0);
  const start = useMemo(() => {
    const m = mondayOf(new Date());
    m.setDate(m.getDate() + week * 7);
    return m;
  }, [week]);
  const days = Array.from({ length: 5 }, (_, i) => new Date(start.getTime() + i * DAY_MS));
  const H0 = 8;
  const H1 = 18;
  const PX = 52;
  const parsed = events
    .map((e) => ({ e, s: toDate(pick(e, "start", "start_time")), en: toDate(pick(e, "end", "end_time")) }))
    .filter((x) => x.s) as { e: O; s: Date; en: Date | null }[];
  const inWeek = parsed.filter((x) => x.s.getTime() >= start.getTime() && x.s.getTime() < start.getTime() + 7 * DAY_MS);
  const now = new Date();
  const label = `${start.toLocaleDateString(undefined, { month: "short", day: "numeric" })} to ${days[4].toLocaleDateString(undefined, { month: "short", day: "numeric" })}`;

  return (
    <div className="overflow-hidden rounded-xl border border-line bg-panel shadow-card">
      <div className="flex items-center gap-2 border-b border-line px-4 py-2.5">
        <h2 className="text-sm font-semibold text-ink">{week === 0 ? "This week" : week === 1 ? "Next week" : week === -1 ? "Last week" : label}</h2>
        <span className="text-xs text-ink-3">{label}</span>
        <span className="text-xs text-ink-3">· {inWeek.length} events</span>
        <div className="ml-auto flex items-center gap-1">
          <Button size="sm" variant="ghost" icon={ChevronLeft} onClick={() => setWeek((w) => w - 1)} aria-label="Previous week" />
          <Button size="sm" variant="secondary" onClick={() => setWeek(0)} disabled={week === 0}>
            Today
          </Button>
          <Button size="sm" variant="ghost" icon={ChevronRight} onClick={() => setWeek((w) => w + 1)} aria-label="Next week" />
        </div>
      </div>
      {/* Week grid (md+) */}
      <div className="hidden overflow-x-auto md:block">
        <div className="grid min-w-[760px]" style={{ gridTemplateColumns: `52px repeat(5, minmax(0, 1fr))` }}>
          <div className="border-b border-line" />
          {days.map((d) => {
            const today = d.toDateString() === now.toDateString();
            return (
              <div key={d.toISOString()} className="border-b border-l border-line px-2 py-2 text-center">
                <span className="text-2xs font-medium uppercase tracking-[0.06em] text-ink-3">{d.toLocaleDateString(undefined, { weekday: "short" })}</span>
                <span className={cx("ml-1.5 inline-grid size-6 place-items-center rounded-full text-sm font-semibold", today ? "bg-block-solid text-[oklch(0.99_0_0)]" : "text-ink")}>{d.getDate()}</span>
              </div>
            );
          })}
          <div className="relative" style={{ height: (H1 - H0) * PX }}>
            {Array.from({ length: H1 - H0 }, (_, i) => (
              <span key={i} className="absolute right-2 -translate-y-1/2 font-mono text-[10px] text-ink-4" style={{ top: i * PX }}>
                {i === 0 ? "" : `${H0 + i}:00`}
              </span>
            ))}
          </div>
          {days.map((d) => {
            const dayEvents = inWeek.filter((x) => x.s.toDateString() === d.toDateString());
            const today = d.toDateString() === now.toDateString();
            const nowTop = ((now.getHours() + now.getMinutes() / 60 - H0) / (H1 - H0)) * (H1 - H0) * PX;
            return (
              <div key={d.toISOString()} className="relative border-l border-line" style={{ height: (H1 - H0) * PX }}>
                {Array.from({ length: H1 - H0 }, (_, i) => (
                  <span key={i} className="absolute inset-x-0 border-t border-line/70" style={{ top: i * PX }} aria-hidden />
                ))}
                {today && nowTop > 0 && nowTop < (H1 - H0) * PX && (
                  <span className="absolute inset-x-0 z-20 h-px bg-block-solid" style={{ top: nowTop }} aria-hidden>
                    <span className="absolute -left-1 -top-1 size-2 rounded-full bg-block-solid" />
                  </span>
                )}
                {layoutDay(dayEvents).map(({ x, col, cols }) => {
                  const s = x.s.getHours() + x.s.getMinutes() / 60;
                  const en = x.en ? x.en.getHours() + x.en.getMinutes() / 60 : s + 0.5;
                  const top = Math.max(0, (s - H0) * PX);
                  const h = Math.max(20, (Math.min(en, H1) - Math.max(s, H0)) * PX - 2);
                  const agent = !!str(x.e.adjutant_key) || !!x.e.by_agent;
                  const external = asArray(x.e.attendees).map(str).some((a) => a.includes("@") && !a.endsWith(`@${INTERNAL_DOMAIN}`));
                  if (en <= H0 || s >= H1) return null;
                  return (
                    <div
                      key={str(x.e.id) || titleOf(x.e)}
                      className={cx(
                        "absolute z-10 overflow-hidden rounded-md border px-1.5 py-1 text-[11px] leading-tight",
                        agent ? "border-accent-line bg-accent-soft text-accent" : external ? "border-ask-line bg-ask-bg text-ask-fg" : "border-line-strong bg-panel-2 text-ink-2",
                      )}
                      style={{ top, height: h, left: `calc(${(col / cols) * 100}% + 2px)`, width: `calc(${100 / cols}% - 4px)` }}
                      title={`${titleOf(x.e)} · ${shortTime(x.s)}${x.en ? ` to ${shortTime(x.en)}` : ""} · ${asArray(x.e.attendees).map(str).join(", ")}`}
                    >
                      <span className="block truncate font-medium">{titleOf(x.e)}</span>
                      {h > 30 && <span className="block truncate opacity-80">{shortTime(x.s)}</span>}
                      {agent && h > 44 && (
                        <span className="mt-0.5 flex items-center gap-1 opacity-90">
                          <Bot className="size-3" aria-hidden /> Adjutant
                        </span>
                      )}
                    </div>
                  );
                })}
              </div>
            );
          })}
        </div>
      </div>
      {/* Agenda (mobile) */}
      <ol className="divide-y divide-line md:hidden">
        {days.map((d) => {
          const dayEvents = inWeek.filter((x) => x.s.toDateString() === d.toDateString()).sort((a, b) => a.s.getTime() - b.s.getTime());
          return (
            <li key={d.toISOString()} className="px-4 py-3">
              <p className="text-xs font-semibold text-ink">{d.toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" })}</p>
              {dayEvents.length ? (
                <ul className="mt-2 space-y-1.5">
                  {dayEvents.map((x) => (
                    <li key={str(x.e.id) || titleOf(x.e)} className="flex gap-3 text-sm">
                      <span className="w-14 shrink-0 font-mono text-2xs text-ink-3">{shortTime(x.s)}</span>
                      <span className="min-w-0 text-ink-2">{titleOf(x.e)}</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="mt-1 text-xs text-ink-3">Free</p>
              )}
            </li>
          );
        })}
      </ol>
      <div className="flex flex-wrap items-center gap-4 border-t border-line px-4 py-2 text-2xs text-ink-3">
        <span className="flex items-center gap-1.5"><span className="size-2 rounded-[3px] border border-line-strong bg-panel-2" aria-hidden />internal</span>
        <span className="flex items-center gap-1.5"><span className="size-2 rounded-[3px] border border-ask-line bg-ask-bg" aria-hidden />has external attendees</span>
        <span className="flex items-center gap-1.5"><span className="size-2 rounded-[3px] border border-accent-line bg-accent-soft" aria-hidden />created by Adjutant</span>
      </div>
    </div>
  );
}

function layoutDay(items: { e: O; s: Date; en: Date | null }[]) {
  const sorted = [...items].sort((a, b) => a.s.getTime() - b.s.getTime());
  const out: { x: (typeof items)[number]; col: number; cols: number }[] = [];
  let cluster: typeof out = [];
  let clusterEnd = 0;
  const flush = () => {
    const cols = Math.max(1, ...cluster.map((c) => c.col + 1));
    for (const c of cluster) out.push({ ...c, cols });
    cluster = [];
  };
  for (const x of sorted) {
    const s = x.s.getTime();
    const en = x.en?.getTime() ?? s + 30 * 60_000;
    if (cluster.length && s >= clusterEnd) flush();
    const used = new Set(cluster.filter((c) => (c.x.en?.getTime() ?? c.x.s.getTime() + 1_800_000) > s).map((c) => c.col));
    let col = 0;
    while (used.has(col)) col++;
    cluster.push({ x, col, cols: 1 });
    clusterEnd = Math.max(clusterEnd, en);
  }
  if (cluster.length) flush();
  return out;
}

// ---------------------------------------------------------------------------

function DocList({ items, icon: Icon, empty }: { items: O[]; icon: LucideIcon; empty: string }) {
  const [sel, setSel] = useState<string | null>(null);
  const current = items.find((d) => str(d.id) === sel) ?? items[0];
  if (!items.length) {
    return (
      <div className="rounded-xl border border-line bg-panel">
        <EmptyState icon={Icon} title={empty}>Reset the sandbox to restore the seeded content.</EmptyState>
      </div>
    );
  }
  return (
    <SplitPane
      list={items.map((d) => (
        <ListButton key={str(d.id) || titleOf(d)} active={current === d} onClick={() => setSel(str(d.id))}>
          <span className="flex items-center gap-2">
            <Icon className="size-3.5 shrink-0 text-ink-3" aria-hidden />
            <span className="min-w-0 flex-1 truncate text-sm font-medium text-ink">{titleOf(d)}</span>
            {(d.by_agent || str(d.adjutant_key)) && <Bot className="size-3.5 text-accent" aria-label="created by Adjutant" />}
          </span>
          <span className="mt-0.5 block truncate pl-5.5 text-xs text-ink-3">
            {[str(pick(d, "owner")), relative(pick(d, "updated_at", "created_at")), str(pick(d, "type"))].filter(Boolean).join(" · ")}
          </span>
        </ListButton>
      ))}
      detail={
        current && (
          <article className="px-5 py-5 sm:px-8">
            <div className="mb-4 flex flex-wrap items-center gap-2 text-2xs text-ink-3">
              <Mono>{str(current.id)}</Mono>
              {str(current.parent) && <span>in {str(current.parent)}</span>}
              <TaintBadge size="xs" label="untrusted when read" source="Docs and pages written by other people are treated as untrusted input when the agent reads them." />
            </div>
            <Markdown source={str(pick(current, "content_md", "content", "body", "text")) || "_(empty)_"} className="max-w-[75ch] text-[14px]" />
          </article>
        )
      }
    />
  );
}

// ---------------------------------------------------------------------------

function sheetRows(s: O): { header: string[]; rows: string[][] } {
  const values = (Array.isArray(s.values) ? s.values : Array.isArray(s.rows) ? s.rows : []) as unknown[];
  const cols = Array.isArray(s.columns) ? (s.columns as unknown[]).map(str) : Array.isArray(s.headers) ? (s.headers as unknown[]).map(str) : null;
  const matrix = values.map((r) => (Array.isArray(r) ? r.map(str) : r && typeof r === "object" ? Object.values(r as O).map(str) : [str(r)]));
  if (cols) return { header: cols, rows: matrix };
  if (values.length && !Array.isArray(values[0]) && typeof values[0] === "object") return { header: Object.keys(values[0] as O), rows: matrix };
  return { header: matrix[0] ?? [], rows: matrix.slice(1) };
}

function SheetsView({ sheets }: { sheets: O[] }) {
  const [idx, setIdx] = useState(0);
  const s = sheets[idx];
  if (!s) {
    return (
      <div className="rounded-xl border border-line bg-panel">
        <EmptyState icon={Table2} title="No sheets" />
      </div>
    );
  }
  const { header, rows } = sheetRows(s);
  return (
    <div className="overflow-hidden rounded-xl border border-line bg-panel shadow-card">
      <div className="flex flex-wrap items-center gap-1 border-b border-line px-3 py-2">
        {sheets.map((sh, i) => (
          <button
            key={str(sh.id) || i}
            type="button"
            aria-pressed={i === idx}
            onClick={() => setIdx(i)}
            className={cx("inline-flex h-7 items-center gap-1.5 rounded-md px-2.5 text-xs font-medium", i === idx ? "bg-panel-3 text-ink" : "text-ink-3 hover:text-ink")}
          >
            <Table2 className="size-3.5" aria-hidden />
            {titleOf(sh)}
            {sh.sensitive === true && <Lock className="size-3 text-block-fg" aria-label="sensitive" />}
          </button>
        ))}
        <span className="ml-auto text-2xs text-ink-3">
          {rows.length} rows · {str(s.owner)} · updated {relative(s.updated_at)}
        </span>
      </div>
      {s.sensitive === true && (
        <p className="flex items-center gap-2 border-b border-block-line bg-block-bg px-4 py-2 text-xs text-block-fg">
          <Lock className="size-3.5" aria-hidden /> Sensitive: contains bank details. Sending this outside {INTERNAL_DOMAIN} is blocked by policy (sensitive ≥ 80% and external).
        </p>
      )}
      <div className="max-h-[560px] overflow-auto">
        <table className="w-full min-w-max text-left text-xs">
          <thead className="sticky top-0 z-10 bg-panel-2 text-2xs text-ink-3">
            <tr>
              <th className="w-10 border-b border-r border-line px-2 py-2 text-right font-mono font-normal">#</th>
              {header.map((h, i) => (
                <th key={i} className="whitespace-nowrap border-b border-line px-3 py-2 font-medium">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i} className="hover:bg-panel-2">
                <td className="border-b border-r border-line px-2 py-1.5 text-right font-mono text-[10.5px] text-ink-4">{i + 2}</td>
                {r.map((c, j) => (
                  <td key={j} className={cx("whitespace-nowrap border-b border-line px-3 py-1.5 text-ink-2", /^\d+(\.\d+)?$/.test(c) && "tnum text-right font-mono")}>
                    {c}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------

function slackChannels(slack: unknown): { name: string; messages: O[] }[] {
  if (!slack) return [];
  if (Array.isArray(slack)) return slack.map((c: O) => ({ name: str(c.name ?? c.id), messages: (asArray(c.messages) as O[]) ?? [] }));
  const o = slack as O;
  const src = (o.channels ?? o) as unknown;
  if (Array.isArray(src)) return src.map((c: O) => ({ name: str(c.name ?? c.id), messages: asArray(c.messages) as O[] }));
  return Object.entries(src as O).map(([name, msgs]) => ({ name, messages: (Array.isArray(msgs) ? msgs : asArray((msgs as O)?.messages)) as O[] }));
}

function SlackView({ slack }: { slack: unknown }) {
  const channels = slackChannels(slack);
  const [sel, setSel] = useState<string | null>(null);
  const current = channels.find((c) => c.name === sel) ?? channels[0];
  if (!channels.length) {
    return (
      <div className="rounded-xl border border-line bg-panel">
        <EmptyState icon={Hash} title="No Slack channels" />
      </div>
    );
  }
  const msgs = [...(current?.messages ?? [])].sort((a, b) => (toDate(a.ts)?.getTime() ?? 0) - (toDate(b.ts)?.getTime() ?? 0));
  return (
    <SplitPane
      list={channels.map((c) => (
        <ListButton key={c.name} active={current === c} onClick={() => setSel(c.name)}>
          <span className="flex items-center gap-2 text-sm font-medium text-ink">
            <Hash className="size-3.5 text-ink-3" aria-hidden />
            {c.name.replace(/^#/, "")}
            <span className="ml-auto font-mono text-2xs font-normal text-ink-4">{c.messages.length}</span>
          </span>
          <span className="mt-0.5 block truncate pl-5.5 text-xs text-ink-3">{str(c.messages[c.messages.length - 1]?.text)}</span>
        </ListButton>
      ))}
      detail={
        <div>
          <div className="sticky top-0 z-10 border-b border-line bg-panel px-5 py-3 text-sm font-semibold text-ink">#{current?.name.replace(/^#/, "")}</div>
          <ol className="space-y-4 px-5 py-4">
            {msgs.map((m, i) => {
              const who = str(pick(m, "user", "author", "name", "email")) || "someone";
              const agent = !!m.by_agent || !!str(m.adjutant_key);
              return (
                <li key={str(m.id) || i} className="flex gap-3">
                  <span className={cx("grid size-8 shrink-0 place-items-center rounded-md text-xs font-semibold", agent ? "bg-accent-soft text-accent" : "bg-panel-3 text-ink-2")} aria-hidden>
                    {who
                      .split(/\s+/)
                      .map((w) => w[0])
                      .join("")
                      .slice(0, 2)
                      .toUpperCase()}
                  </span>
                  <div className="min-w-0">
                    <p className="text-sm">
                      <span className="font-semibold text-ink">{who}</span>
                      {agent && <span className="ml-1.5 text-2xs text-accent">via Adjutant</span>}
                      <span className="ml-2 text-2xs text-ink-3">{relative(m.ts)}</span>
                    </p>
                    <p className="mt-0.5 whitespace-pre-wrap text-sm leading-relaxed text-ink-2">{str(m.text)}</p>
                  </div>
                </li>
              );
            })}
          </ol>
        </div>
      }
    />
  );
}

// ---------------------------------------------------------------------------

function MeetingsView({ meetings }: { meetings: O[] }) {
  const [sel, setSel] = useState<string | null>(null);
  const current = meetings.find((m) => str(m.id) === sel) ?? meetings[0];
  if (!meetings.length) {
    return (
      <div className="rounded-xl border border-line bg-panel">
        <EmptyState icon={AudioLines} title="No meeting transcripts" />
      </div>
    );
  }
  const lines = (Array.isArray(current?.sentences) ? current.sentences : Array.isArray(current?.transcript) ? current.transcript : []) as O[];
  const textTranscript = typeof current?.transcript === "string" ? (current.transcript as string) : "";
  const actions = asArray(current?.action_items).map(str);
  return (
    <SplitPane
      list={meetings.map((m) => (
        <ListButton key={str(m.id) || titleOf(m)} active={current === m} onClick={() => setSel(str(m.id))}>
          <span className="block truncate text-sm font-medium text-ink">{titleOf(m)}</span>
          <span className="mt-0.5 block text-xs text-ink-3">
            {dateTime(pick(m, "date", "start", "started_at"))}
            {m.duration_min ? ` · ${str(m.duration_min)} min` : ""}
          </span>
        </ListButton>
      ))}
      detail={
        current && (
          <article className="px-5 py-5 sm:px-6">
            <h2 className="text-lg font-semibold text-ink">{titleOf(current)}</h2>
            <p className="mt-1 flex flex-wrap items-center gap-2 text-xs text-ink-3">
              <Users className="size-3.5" aria-hidden />
              {asArray(current.attendees).map(personName).join(", ")}
            </p>
            <div className="mt-3">
              <TaintBadge size="xs" label="transcript is untrusted" source="Transcripts contain other people's words; they are treated as untrusted input." />
            </div>
            {actions.length > 0 && (
              <div className="mt-4 rounded-lg border border-line bg-panel-2 px-4 py-3">
                <p className="text-xs font-semibold text-ink">Action items</p>
                <ul className="mt-1.5 list-disc space-y-1 pl-4 text-sm text-ink-2">
                  {actions.map((a) => (
                    <li key={a}>{a}</li>
                  ))}
                </ul>
              </div>
            )}
            {lines.length > 0 ? (
              <ol className="mt-4 space-y-3">
                {lines.map((l, i) => (
                  <li key={i} className="grid grid-cols-[52px_1fr] gap-3">
                    <span className="pt-0.5 font-mono text-[10.5px] text-ink-4">{fmtSec(Number(pick(l, "start_s", "t", "time") ?? 0))}</span>
                    <p className="text-sm leading-relaxed text-ink-2">
                      <span className="font-semibold text-ink">{str(pick(l, "speaker", "name"))}</span> {str(pick(l, "text", "line"))}
                    </p>
                  </li>
                ))}
              </ol>
            ) : textTranscript ? (
              <p className="mt-4 whitespace-pre-wrap text-sm leading-relaxed text-ink-2">{textTranscript}</p>
            ) : null}
          </article>
        )
      }
    />
  );
}

function fmtSec(s: number) {
  const m = Math.floor(s / 60);
  const r = Math.floor(s % 60);
  return `${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`;
}

// ---------------------------------------------------------------------------

const OUT_ICON: Record<string, LucideIcon> = { email: Mail, invite: CalendarDays, slack: Hash, cancellation: CalendarDays, notion: NotebookPen, docs: FileText, sheets: Table2 };

function OutboxView({ items }: { items: O[] }) {
  const sorted = [...items].sort((a, b) => (toDate(pick(b, "at", "ts", "date"))?.getTime() ?? 0) - (toDate(pick(a, "at", "ts", "date"))?.getTime() ?? 0));
  return (
    <div className="overflow-hidden rounded-xl border border-line bg-panel shadow-card">
      <div className="flex items-center gap-2 border-b border-line bg-accent-soft/50 px-4 py-2.5 text-xs text-ink-2">
        <Bot className="size-4 text-accent" aria-hidden />
        Everything Adjutant sent or created in this sandbox, newest first. Each entry carries the idempotency key the ledger used.
      </div>
      {sorted.length === 0 ? (
        <EmptyState icon={Send} title="Nothing sent yet">
          Approve a change in a run and it shows up here: emails, invites, Slack posts and new pages.
        </EmptyState>
      ) : (
        <ol className="divide-y divide-line">
          {sorted.map((o, i) => {
            const kind = str(pick(o, "kind", "type", "app")) || "change";
            const Icon = OUT_ICON[kind] ?? Send;
            return (
              <li key={str(o.id) || i} className="grid gap-x-4 gap-y-1 px-4 py-3.5 sm:grid-cols-[auto_1fr_auto]">
                <span className="grid size-8 place-items-center rounded-md border border-accent-line bg-accent-soft text-accent">
                  <Icon className="size-4" aria-hidden />
                </span>
                <div className="min-w-0">
                  <p className="flex flex-wrap items-center gap-2 text-sm">
                    <span className="font-semibold text-ink">{str(pick(o, "subject", "title")) || kind}</span>
                    <span className="rounded-[4px] bg-panel-3 px-1.5 font-mono text-[10px] text-ink-3">{kind}</span>
                  </p>
                  <p className="mt-0.5 text-xs text-ink-3">to {asArray(o.to).map(str).join(", ") || "n/a"}</p>
                  {str(pick(o, "body", "text")) && <p className="mt-1.5 line-clamp-3 whitespace-pre-wrap text-sm leading-relaxed text-ink-2">{str(pick(o, "body", "text"))}</p>}
                </div>
                <div className="text-left sm:text-right">
                  <p className="text-xs text-ink-2">{relative(pick(o, "at", "ts", "date"))}</p>
                  {str(o.adjutant_key) && (
                    <p className="mt-0.5 font-mono text-[10.5px] text-ink-4" title={str(o.adjutant_key)}>
                      {truncateMiddle(str(o.adjutant_key), 24)}
                    </p>
                  )}
                </div>
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}


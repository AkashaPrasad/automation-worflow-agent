import { useRef } from "react";
import { Link } from "react-router";
import {
  ArrowRight,
  BadgeCheck,
  Brain,
  Ghost,
  Hand,
  Inbox,
  RefreshCw,
  ScanEye,
  ShieldCheck,
  Workflow,
  Zap,
  type LucideIcon,
} from "lucide-react";
import { useConfig, useHealth, useRuns } from "../../lib/queries";
import type { AppConfig, Integration, RunSummary, Template } from "../../lib/types";
import { appMeta } from "../../lib/semantics";
import { relative, usd } from "../../lib/format";
import { errorMessage } from "../../lib/api";
import { RunStatusBadge } from "../../components/ui/badges";
import { Button, EmptyState, Skeleton, cx } from "../../components/ui/primitives";
import { Composer, type ComposerHandle } from "./Composer";
import { GateDemo } from "./GateDemo";

export function LandingPage() {
  const composer = useRef<ComposerHandle>(null);
  const config = useConfig();
  const health = useHealth();
  const llmMissing = health.data ? !health.data.llm_configured : false;

  return (
    <div className="mx-auto max-w-[1240px] px-4 sm:px-6">
      <section className="grid gap-8 pb-10 pt-8 sm:pt-12 lg:grid-cols-[minmax(0,7fr)_minmax(0,5fr)] lg:gap-10 lg:pt-14" aria-labelledby="hero-title">
        <div className="min-w-0">
          <h1 id="hero-title" className="text-[2.1rem] font-semibold leading-[1.08] tracking-[-0.035em] text-ink sm:text-5xl sm:leading-[1.04]">
            Muse plans. Jev judges.
            <br />
            <span className="text-ink-3">Code decides.</span>
          </h1>
          <p className="mt-4 max-w-[58ch] text-md leading-relaxed text-ink-2">
            Adjutant is a chief of staff for your workday with <strong className="font-semibold text-ink">calibrated autonomy</strong>. It rehearses
            the whole job in a shadow run, lets a calibrated judge score every change, and asks you only about the ones that deserve a human.
          </p>
          <div className="mt-6">
            <Composer ref={composer} config={config.data} llmMissing={llmMissing} />
          </div>
          <Templates config={config.data} loading={config.isLoading} error={config.error} onPick={(t) => composer.current?.fill(t.prompt)} />
        </div>
        <div className="min-w-0 space-y-4 lg:pt-1">
          <GateDemo />
          <SystemStatus config={config.data} />
        </div>
      </section>

      <RecentRuns />
      <HowItWorks />
    </div>
  );
}

// ---------------------------------------------------------------------------

function Templates({
  config,
  loading,
  error,
  onPick,
}: {
  config: AppConfig | undefined;
  loading: boolean;
  error: unknown;
  onPick: (t: Template) => void;
}) {
  const templates = config?.templates ?? [];
  return (
    <section className="mt-8" aria-labelledby="templates-title">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5">
        <h2 id="templates-title" className="text-sm font-semibold text-ink">
          Try a scenario
        </h2>
        <p className="text-xs text-ink-3">Runs against the Acme Robotics sandbox. Nothing leaves it.</p>
      </div>
      {loading ? (
        <div className="mt-3 grid gap-3 sm:grid-cols-2">
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-[118px]" />
          ))}
        </div>
      ) : error ? (
        <p className="mt-3 rounded-lg border border-block-line bg-block-bg px-3 py-2 text-sm text-block-fg">Couldn't load scenarios: {errorMessage(error)}</p>
      ) : (
        <ul className="mt-3 grid gap-3 sm:grid-cols-2">
          {templates.map((t) => (
            <li key={t.id} className="min-w-0">
              <button
                type="button"
                data-testid="template-card"
                data-template-id={t.id}
                onClick={() => onPick(t)}
                className="group flex h-full w-full flex-col rounded-xl border border-line bg-panel p-3.5 text-left shadow-card transition-[border-color,background-color,transform] duration-150 hover:border-line-strong hover:bg-panel-2 active:translate-y-px"
              >
                <span className="flex items-center gap-1.5">
                  {t.apps.map((a) => {
                    const m = appMeta(a);
                    return (
                      <span key={a} title={m.label} className="grid size-6 place-items-center rounded-md border border-line bg-panel-2 text-ink-3">
                        <m.icon className="size-3.5" aria-hidden />
                      </span>
                    );
                  })}
                  <ArrowRight className="ml-auto size-3.5 text-ink-4 transition-transform duration-150 group-hover:translate-x-0.5 group-hover:text-ink-2" aria-hidden />
                </span>
                <span className="mt-2.5 text-sm font-semibold leading-snug text-ink">{t.title}</span>
                <span className="mt-1 line-clamp-2 text-xs leading-relaxed text-ink-3">{t.prompt}</span>
                {t.highlights && t.highlights.length > 0 && (
                  <span className="mt-2.5 flex flex-wrap gap-1">
                    {t.highlights.map((h) => (
                      <span key={h} className="rounded-[4px] bg-panel-3 px-1.5 py-px text-[10.5px] text-ink-2">
                        {h}
                      </span>
                    ))}
                  </span>
                )}
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------

function integrationList(cfg: AppConfig | undefined): { key: string; label: string; mode: string; detail?: string }[] {
  const raw = cfg?.integrations;
  if (!raw) return [];
  const arr: Integration[] = Array.isArray(raw)
    ? raw
    : Object.entries(raw).map(([k, v]) => (typeof v === "object" && v ? { app: k, ...(v as object) } : { app: k, mode: String(v) }));
  return arr.map((i, idx) => {
    const key = String(i.app ?? i.name ?? i.id ?? idx);
    const mode = String(i.mode ?? (i.live ? "live" : i.status ?? "sandbox"));
    return { key, label: String(i.title ?? appMeta(key).label), mode, detail: typeof i.detail === "string" ? i.detail : undefined };
  });
}

function SystemStatus({ config }: { config: AppConfig | undefined }) {
  const health = useHealth();
  const h = health.data;
  const ints = integrationList(config);
  const models = h?.models ?? config?.models;
  return (
    <section className="rounded-xl border border-line bg-panel shadow-card" aria-labelledby="status-title">
      <header className="flex items-center justify-between border-b border-line px-4 py-3">
        <h2 id="status-title" className="text-sm font-semibold text-ink">
          System status
        </h2>
        {health.isError ? (
          <span className="inline-flex items-center gap-1.5 text-xs text-block-fg">
            <span className="size-1.5 rounded-full bg-block-solid" aria-hidden /> API unreachable
          </span>
        ) : h ? (
          <span className="inline-flex items-center gap-1.5 text-xs text-ink-3">
            <span className={cx("size-1.5 rounded-full", h.ok ? "bg-auto-solid" : "bg-ask-solid")} aria-hidden />
            API v{h.version}
          </span>
        ) : (
          <Skeleton className="h-4 w-20" />
        )}
      </header>
      {health.isError && (
        <p className="border-b border-line bg-block-bg px-4 py-2 text-xs text-block-fg">{errorMessage(health.error)}</p>
      )}
      <dl className="grid grid-cols-2 gap-px bg-line">
        <ModelCell label="Planner · System 2" value={models?.planner} ok={h?.llm_configured} okText="key configured" badText="key missing" icon={Brain} tone="text-agent-planner" />
        <ModelCell label="Judge · System 1" value={models?.judge} ok={h?.judge_configured} okText={`fallback ${models?.fallback ?? "laya"}`} badText={`using ${models?.fallback ?? "Laya"} fallback`} icon={ScanEye} tone="text-agent-guardian" />
      </dl>
      <div className="px-4 py-3">
        <p className="text-2xs font-medium text-ink-3">Integrations</p>
        {ints.length ? (
          <ul className="mt-2 flex flex-wrap gap-1.5">
            {ints.map((i) => {
              const m = appMeta(i.key);
              const live = i.mode === "live";
              return (
                <li key={i.key} title={i.detail}>
                  <span className="inline-flex h-6 items-center gap-1.5 rounded-md border border-line bg-panel-2 pl-1.5 pr-1 text-xs text-ink-2">
                    <m.icon className="size-3.5 text-ink-3" aria-hidden />
                    {i.label}
                    <span
                      className={cx(
                        "rounded-[4px] px-1 font-mono text-[10px]",
                        live ? "bg-auto-bg text-auto-fg" : "bg-panel-3 text-ink-3",
                      )}
                    >
                      {i.mode}
                    </span>
                  </span>
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="mt-2 text-xs text-ink-3">{config ? "No integrations reported." : "Loading integrations"}</p>
        )}
      </div>
    </section>
  );
}

function ModelCell({
  label,
  value,
  ok,
  okText,
  badText,
  icon: Icon,
  tone,
}: {
  label: string;
  value?: string;
  ok?: boolean;
  okText: string;
  badText: string;
  icon: LucideIcon;
  tone: string;
}) {
  return (
    <div className="bg-panel px-4 py-3">
      <dt className="flex items-center gap-1.5 text-2xs font-medium text-ink-3">
        <Icon className={cx("size-3.5", tone)} aria-hidden />
        {label}
      </dt>
      <dd className="mt-1 truncate font-mono text-xs text-ink" title={value}>
        {value ?? <Skeleton className="h-4 w-28" />}
      </dd>
      {ok !== undefined && (
        <dd className={cx("mt-0.5 text-2xs", ok ? "text-ink-3" : "text-ask-fg")}>{ok ? okText : badText}</dd>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------

function RecentRuns() {
  const runs = useRuns();
  const list = runs.data ?? [];
  return (
    <section className="border-t border-line py-10" aria-labelledby="recent-title">
      <div className="flex items-baseline justify-between gap-3">
        <h2 id="recent-title" className="text-lg font-semibold tracking-[-0.015em] text-ink">
          Recent runs
        </h2>
        {list.length > 0 && <p className="text-xs text-ink-3">{list.length} in this workspace</p>}
      </div>
      <div className="mt-4 overflow-hidden rounded-xl border border-line bg-panel shadow-card">
        {runs.isLoading ? (
          <div className="divide-y divide-line">
            {[0, 1, 2].map((i) => (
              <div key={i} className="flex items-center gap-4 px-4 py-3.5">
                <Skeleton className="h-5 w-28" />
                <Skeleton className="h-4 flex-1" />
                <Skeleton className="h-4 w-16" />
              </div>
            ))}
          </div>
        ) : runs.isError ? (
          <EmptyState icon={Inbox} title="Couldn't load runs">
            {errorMessage(runs.error)}
            <div className="mt-3">
              <Button size="sm" onClick={() => void runs.refetch()} icon={RefreshCw}>
                Retry
              </Button>
            </div>
          </EmptyState>
        ) : list.length === 0 ? (
          <EmptyState icon={Workflow} title="No runs yet">
            Pick a scenario above or describe a job in your own words. The first run shows up here with its status, cost and verdicts.
          </EmptyState>
        ) : (
          <ul className="divide-y divide-line">
            {list.slice(0, 12).map((r) => (
              <RunRow key={r.id} run={r} />
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

function RunRow({ run }: { run: RunSummary }) {
  const m = run.metrics;
  return (
    <li>
      <Link
        to={`/runs/${run.id}`}
        data-testid="run-list-item"
        data-run-id={run.id}
        className="grid grid-cols-[1fr_auto] items-center gap-x-4 gap-y-1 px-4 py-3 transition-colors duration-150 hover:bg-panel-2 sm:grid-cols-[168px_minmax(0,1fr)_auto_auto]"
      >
        <span className="order-2 col-span-2 sm:order-none sm:col-span-1">
          <RunStatusBadge status={run.status} />
        </span>
        <span className="order-1 min-w-0 sm:order-none">
          <span className="block truncate text-sm font-medium text-ink">{run.title || run.request}</span>
          <span className="block truncate text-xs text-ink-3">{run.request}</span>
        </span>
        <span className="order-1 hidden text-right sm:order-none sm:block">
          <span className="tnum block font-mono text-xs text-ink-2">{usd(m?.cost_usd ?? 0)}</span>
          <span className="block text-2xs text-ink-3">
            {m?.auto_approved ?? 0} auto · {m?.approvals_requested ?? 0} asked
          </span>
        </span>
        <span className="order-1 text-right text-xs text-ink-3 sm:order-none sm:w-24">
          <span className="block">{relative(run.created_at)}</span>
          <span className="block text-2xs capitalize">{run.autonomy}</span>
        </span>
      </Link>
    </li>
  );
}

// ---------------------------------------------------------------------------

const STAGES: { icon: LucideIcon; title: string; text: string; who: string; dot: string }[] = [
  { icon: Brain, title: "Plan", text: "Muse turns the request into a goal tree with checkable success criteria.", who: "Planner", dot: "bg-agent-planner" },
  { icon: Ghost, title: "Shadow", text: "Reads run for real. Writes are simulated into previews. Nothing changes yet.", who: "Executor", dot: "bg-agent-executor" },
  { icon: ShieldCheck, title: "Gate", text: "Jev scores alignment, injection, sensitivity and tone. policy.py turns scores into AUTO, ASK or BLOCK.", who: "Guardian", dot: "bg-agent-guardian" },
  { icon: Hand, title: "Approve", text: "One plan-diff review with only the risky changes. Approvals bind to the exact arguments.", who: "You", dot: "bg-ask-solid" },
  { icon: Zap, title: "Commit", text: "Writes go through an effect ledger with idempotency keys, so a retry never sends twice.", who: "Executor", dot: "bg-agent-executor" },
  { icon: BadgeCheck, title: "Verify", text: "Jev checks every goal's criteria against what the tools actually returned.", who: "Evaluator", dot: "bg-agent-evaluator" },
];

function HowItWorks() {
  return (
    <section className="border-t border-line py-10" aria-labelledby="how-title">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <h2 id="how-title" className="text-lg font-semibold tracking-[-0.015em] text-ink">
          How a run works
        </h2>
        <Link to="/architecture" className="inline-flex items-center gap-1 text-sm font-medium text-accent hover:underline">
          Full architecture <ArrowRight className="size-3.5" aria-hidden />
        </Link>
      </div>
      <ol className="relative mt-6 grid gap-6 sm:grid-cols-2 lg:grid-cols-6 lg:gap-4">
        <span className="absolute left-0 right-0 top-[15px] hidden h-px bg-line-strong lg:block" aria-hidden />
        {STAGES.map((s, i) => (
          <li key={s.title} className="relative flex gap-3 lg:block">
            <span className="relative z-10 grid size-8 shrink-0 place-items-center rounded-lg border border-line-strong bg-panel text-ink-2 shadow-card">
              <s.icon className="size-4" aria-hidden />
            </span>
            <div className="lg:mt-3">
              <p className="flex items-center gap-2 text-sm font-semibold text-ink">
                {s.title}
                {i === 5 && (
                  <span className="inline-flex items-center gap-1 text-2xs font-normal text-ink-3">
                    <RefreshCw className="size-3" aria-hidden /> re-plan on failure
                  </span>
                )}
              </p>
              <p className="mt-1 text-xs leading-relaxed text-ink-3">{s.text}</p>
              <p className="mt-2 inline-flex items-center gap-1.5 text-2xs font-medium text-ink-2">
                <span className={cx("size-1.5 rounded-full", s.dot)} aria-hidden />
                {s.who}
              </p>
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}

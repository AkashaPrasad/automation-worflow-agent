import { useCallback, useState } from "react";
import { Link, useParams } from "react-router";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  BadgeCheck,
  CircleSlash,
  CircleX,
  Coins,
  Cpu,
  MessageCircleQuestionMark,
  Pause,
  Play,
  RefreshCw,
  ScanEye,
  SearchX,
  Send,
  Split,
  Square,
  Undo2,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import { ApiError, api, errorMessage } from "../../lib/api";
import { useRun } from "../../lib/useRun";
import { TERMINAL_STATUSES, type Run, type RunDetail, type RunStatus } from "../../lib/types";
import { RUN_STATUS } from "../../lib/semantics";
import { compact, relative, usd } from "../../lib/format";
import { RunStatusBadge } from "../../components/ui/badges";
import { Button, EmptyState, Mono, Skeleton, cx } from "../../components/ui/primitives";
import { Markdown } from "../../components/ui/Markdown";
import { useToast } from "../../components/ui/Toast";
import { Tooltip } from "../../components/ui/Tooltip";
import { PlanTree } from "./PlanTree";
import { NodeDrawer } from "./NodeDrawer";
import { ApprovalPanel } from "./ApprovalPanel";
import { Timeline } from "./Timeline";
import { EffectsLedger } from "./EffectsLedger";

const ACTIVE: RunStatus[] = ["created", "planning", "shadowing", "executing", "verifying", "awaiting_approval", "clarifying"];
type Control = "pause" | "resume" | "cancel" | "rollback";

export default function RunPage() {
  const { id = "" } = useParams();
  const state = useRun(id);
  const [selected, setSelected] = useState<string | null>(null);
  const onOpen = useCallback((nid: string) => setSelected(nid), []);
  const d = state.detail;

  if (state.isLoading && !d) return <RunSkeleton />;
  if (!d) {
    const nf = state.error instanceof ApiError && state.error.status === 404;
    return (
      <div className="mx-auto max-w-[1480px] px-4 py-10 sm:px-6">
        <div className="rounded-xl border border-line bg-panel">
          <EmptyState icon={nf ? SearchX : CircleX} title={nf ? "Run not found" : "Couldn't load this run"}>
            {nf ? "It may belong to another workspace, or the link is incomplete." : errorMessage(state.error)}
            <div className="mt-4 flex justify-center gap-2">
              {!nf && (
                <Button size="sm" icon={RefreshCw} onClick={state.refetch}>
                  Retry
                </Button>
              )}
              <Link to="/" className="inline-flex h-7 items-center rounded-md bg-inverse px-2.5 text-xs font-medium text-inverse-ink">
                Back to Command
              </Link>
            </div>
          </EmptyState>
        </div>
      </div>
    );
  }
  return <Console detail={d} state={state} selected={selected} setSelected={setSelected} onOpen={onOpen} />;
}

function Console({
  detail,
  state,
  selected,
  setSelected,
  onOpen,
}: {
  detail: RunDetail;
  state: ReturnType<typeof useRun>;
  selected: string | null;
  setSelected: (id: string | null) => void;
  onOpen: (id: string) => void;
}) {
  const { run, approval, effects } = detail;
  const toast = useToast();
  const qc = useQueryClient();
  const status = run.status;
  const terminal = TERMINAL_STATUSES.has(status);
  const pendingApproval = approval && approval.status === "pending" && status === "awaiting_approval" ? approval : null;
  const canRollback = effects.some((e) => e.status === "applied" && e.compensation) && !["executing", "verifying", "rolled_back"].includes(status);

  const control = useMutation({
    mutationFn: (action: Control) => api.control(run.id, action),
    onSuccess: (r: Run, action) => {
      qc.setQueryData<RunDetail>(["run", run.id], (prev) => (prev ? { ...prev, run: { ...prev.run, ...r } } : prev));
      state.boost();
      void qc.invalidateQueries({ queryKey: ["run", run.id] });
      void qc.invalidateQueries({ queryKey: ["runs"] });
      const msg: Record<Control, [string, string]> = {
        pause: ["Kill switch engaged", "In-flight calls finish. Nothing new starts until you resume."],
        resume: ["Resumed", "The scheduler picks up where it stopped."],
        cancel: ["Run cancelled", "Pending steps were cancelled. Applied effects stay in the ledger."],
        rollback: ["Rolling back", "Reversible effects are compensated newest first. Messages can't be unsent."],
      };
      toast.success(msg[action][0], msg[action][1]);
    },
    onError: (e, action) => toast.error(`Couldn't ${action} the run`, errorMessage(e)),
  });

  const selectedNode = selected && run.plan?.nodes[selected] ? run.plan.nodes[selected] : null;
  const planning = ["created", "planning", "clarifying"].includes(status);

  return (
    <div className="mx-auto max-w-[1480px] px-4 pb-12 pt-5 sm:px-6">
      <span className="sr-only" aria-live="polite" role="status">
        Run status: {RUN_STATUS[status]?.label ?? status}
      </span>

      <RunHeader run={run} controls={<Controls status={status} canRollback={canRollback} pending={control.isPending ? control.variables : undefined} onControl={(a) => control.mutate(a)} />} />
      <Metrics run={run} />

      <div className="mt-4 space-y-4">
        {status === "clarifying" && <ClarifyCard run={run} />}
        {status === "paused" && (
          <Banner tone="neutral" icon={Pause} title="Paused: kill switch engaged">
            In-flight calls finished and nothing new will start. Resume to continue, cancel to stop for good, or roll back reversible changes.
          </Banner>
        )}
        {pendingApproval && <ApprovalPanel key={pendingApproval.id} runId={run.id} approval={pendingApproval} plan={run.plan} effects={effects} />}
        {status === "failed" && (
          <Banner tone="block" icon={CircleX} title="The run failed">
            {run.error || "No error message was recorded."}
          </Banner>
        )}
        {status === "cancelled" && (
          <Banner tone="neutral" icon={CircleSlash} title="Cancelled">
            Pending steps were cancelled. Anything already applied is listed in the ledger below.
          </Banner>
        )}
        {status === "rolled_back" && (
          <Banner tone="undo" icon={Undo2} title="Rolled back">
            {effects.filter((e) => e.status === "compensated").length} reversible changes were compensated. Messages that were already delivered are still listed as applied.
          </Banner>
        )}
        {(status === "completed" || (terminal && run.summary)) && <SummaryCard run={run} effects={effects.length} />}
      </div>

      <div className="mt-4 min-w-0">
        <PlanTree plan={run.plan} selectedId={selected} onOpen={onOpen} planning={planning} waiting={status === "clarifying"} />
      </div>

      <div className="mt-4 grid grid-cols-1 items-start gap-4 xl:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
        <div className="h-[540px] min-w-0 sm:h-[600px]">
          <Timeline events={state.events} plan={run.plan} start={run.created_at} onSelectNode={onOpen} stream={state.stream} loading={state.isLoading} />
        </div>
        <div className="min-w-0">
          <EffectsLedger effects={effects} plan={run.plan} onSelectNode={onOpen} canRollback={canRollback} onRollback={() => control.mutate("rollback")} rollingBack={control.isPending && control.variables === "rollback"} />
        </div>
      </div>

      {selectedNode && run.plan && (
        <NodeDrawer key={selectedNode.id} node={selectedNode} plan={run.plan} effects={effects} events={state.events} onClose={() => setSelected(null)} onSelect={(nid) => setSelected(nid)} />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------

function RunHeader({ run, controls }: { run: Run; controls: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-4 lg:flex-row lg:items-start">
      <div className="min-w-0 flex-1">
        <Link to="/" className="inline-flex items-center gap-1 text-xs font-medium text-ink-3 hover:text-ink">
          <ArrowLeft className="size-3.5" aria-hidden /> Runs
        </Link>
        <div className="mt-2 flex flex-wrap items-center gap-2.5">
          <RunStatusBadge status={run.status} testId="run-status" large />
          <h1 className="min-w-0 text-xl font-semibold leading-tight tracking-[-0.02em] text-ink">{run.title || "Untitled run"}</h1>
        </div>
        <p className="mt-1.5 max-w-[92ch] text-sm leading-relaxed text-ink-2">{run.request}</p>
        <p className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-2xs text-ink-3">
          <span className="capitalize">
            <span className="text-ink-2">{run.autonomy}</span> autonomy
          </span>
          <Mono>{run.id}</Mono>
          <span>started {relative(run.created_at)}</span>
          {run.intent?.people?.length ? <span>people: {run.intent.people.join(", ")}</span> : null}
        </p>
      </div>
      <div className="shrink-0">{controls}</div>
    </div>
  );
}

function Controls({ status, canRollback, pending, onControl }: { status: RunStatus; canRollback: boolean; pending?: Control; onControl: (a: Control) => void }) {
  const terminal = TERMINAL_STATUSES.has(status);
  const items: { id: Control; label: string; icon: LucideIcon; enabled: boolean; hint: string; variant: "secondary" | "danger" | "primary" }[] = [
    { id: "pause", label: "Pause", icon: Pause, enabled: ACTIVE.includes(status), hint: "Kill switch: stop dispatching new steps. In-flight calls finish.", variant: "secondary" },
    { id: "resume", label: "Resume", icon: Play, enabled: status === "paused", hint: "Continue from where the scheduler stopped.", variant: status === "paused" ? "primary" : "secondary" },
    { id: "cancel", label: "Cancel", icon: Square, enabled: !terminal, hint: "Stop the run for good. Applied effects stay in the ledger.", variant: "secondary" },
    { id: "rollback", label: "Rollback", icon: Undo2, enabled: canRollback, hint: "Compensate every applied reversible effect, newest first.", variant: "danger" },
  ];
  return (
    <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Run controls">
      {items.map((c) => (
        <Tooltip key={c.id} content={c.enabled ? c.hint : `${c.hint} Not available while the run is ${status.replace(/_/g, " ")}.`} side="bottom">
          <Button data-testid={`btn-${c.id}`} size="md" variant={c.variant} icon={c.icon} disabled={!c.enabled || !!pending} loading={pending === c.id} onClick={() => onControl(c.id)}>
            {c.label}
          </Button>
        </Tooltip>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------

function Metrics({ run }: { run: Run }) {
  const m = run.metrics;
  const b = run.budget;
  const costRatio = b.max_cost_usd ? Math.min(1, m.cost_usd / b.max_cost_usd) : 0;
  const asked = m.approvals_requested;
  const auto = m.auto_approved;
  const total = asked + auto;
  return (
    <dl className="mt-5 grid grid-cols-2 overflow-hidden rounded-xl border border-line bg-line shadow-card [gap:1px] sm:grid-cols-3 lg:grid-cols-6">
      <Metric icon={Coins} label="Cost vs budget">
        <dd className="tnum font-mono text-[15px] font-medium text-ink" data-testid="metric-cost">
          {usd(m.cost_usd)} / {usd(b.max_cost_usd, 2)}
        </dd>
        <dd className="mt-2 h-1 overflow-hidden rounded-full bg-panel-3" aria-hidden>
          <span className={cx("block h-full rounded-full transition-[width] duration-500", costRatio > 0.8 ? "bg-block-solid" : costRatio > 0.5 ? "bg-ask-solid" : "bg-auto-solid")} style={{ width: `${Math.max(1.5, costRatio * 100)}%` }} />
        </dd>
      </Metric>
      <Metric icon={Cpu} label="LLM calls · Muse">
        <dd className="flex items-baseline gap-1">
          <span className="tnum font-mono text-[15px] font-medium text-ink" data-testid="metric-llm-calls">
            {m.llm_calls}
          </span>
          <span className="tnum font-mono text-2xs text-ink-3">/ {b.max_llm_calls}</span>
        </dd>
        <dd className="mt-1 text-2xs text-ink-3">
          {compact(m.llm_input_tokens)} in · {compact(m.llm_output_tokens)} out
        </dd>
      </Metric>
      <Metric icon={ScanEye} label="Judgments · Jev">
        <dd className="flex items-baseline gap-1">
          <span className="tnum font-mono text-[15px] font-medium text-ink" data-testid="metric-judgment-calls">
            {m.judgment_calls}
          </span>
        </dd>
        <dd className="mt-1 text-2xs text-ink-3">{compact(m.judgment_tokens)} tokens</dd>
      </Metric>
      <Metric icon={Wrench} label="Tool calls">
        <dd className="flex items-baseline gap-1">
          <span className="tnum font-mono text-[15px] font-medium text-ink">{m.tool_calls}</span>
          <span className="tnum font-mono text-2xs text-ink-3">/ {b.max_tool_calls}</span>
        </dd>
        <dd className="mt-1 text-2xs text-ink-3">loop guard at 3 repeats</dd>
      </Metric>
      <Metric icon={Send} label="Auto vs asked">
        <dd className="flex items-baseline gap-2">
          <span className="tnum font-mono text-[15px] font-medium text-auto-fg" data-testid="metric-auto">
            {auto}
          </span>
          <span className="text-2xs text-ink-3">auto</span>
          <span className="tnum font-mono text-[15px] font-medium text-ask-fg" data-testid="metric-asked">
            {asked}
          </span>
          <span className="text-2xs text-ink-3">asked</span>
        </dd>
        <dd className="mt-2 flex h-1 gap-px overflow-hidden rounded-full bg-panel-3" aria-hidden>
          {total > 0 && (
            <>
              <span className="h-full bg-auto-solid" style={{ width: `${(auto / total) * 100}%` }} />
              <span className="h-full bg-ask-solid" style={{ width: `${(asked / total) * 100}%` }} />
            </>
          )}
        </dd>
      </Metric>
      <Metric icon={Split} label="Re-plans">
        <dd className="flex items-baseline gap-1">
          <span className="tnum font-mono text-[15px] font-medium text-ink">{m.replans}</span>
          <span className="tnum font-mono text-2xs text-ink-3">/ {b.max_replans}</span>
        </dd>
        <dd className="mt-1 text-2xs text-ink-3">completed effects are kept</dd>
      </Metric>
    </dl>
  );
}

function Metric({ icon: Icon, label, children }: { icon: LucideIcon; label: string; children: React.ReactNode }) {
  return (
    <div className="bg-panel px-4 py-3">
      <dt className="flex items-center gap-1.5 text-2xs font-medium text-ink-3">
        <Icon className="size-3.5" aria-hidden />
        {label}
      </dt>
      <div className="mt-1.5">{children}</div>
    </div>
  );
}

// ---------------------------------------------------------------------------

function Banner({ tone, icon: Icon, title, children }: { tone: "neutral" | "block" | "undo"; icon: LucideIcon; title: string; children: React.ReactNode }) {
  const cls = tone === "block" ? "border-block-line bg-block-bg" : tone === "undo" ? "border-undo-line bg-undo-bg" : "border-line-strong bg-panel-2";
  const fg = tone === "block" ? "text-block-fg" : tone === "undo" ? "text-undo-fg" : "text-ink-2";
  return (
    <div className={cx("animate-rise flex items-start gap-3 rounded-xl border px-4 py-3", cls)} role="status">
      <Icon className={cx("mt-0.5 size-4 shrink-0", fg)} aria-hidden />
      <div className="min-w-0">
        <p className={cx("text-sm font-semibold", fg)}>{title}</p>
        <p className="mt-0.5 break-words text-sm text-ink-2">{children}</p>
      </div>
    </div>
  );
}

function ClarifyCard({ run }: { run: Run }) {
  const [answer, setAnswer] = useState("");
  const toast = useToast();
  const qc = useQueryClient();
  const send = useMutation({
    mutationFn: () => api.clarify(run.id, answer.trim()),
    onSuccess: (r) => {
      qc.setQueryData<RunDetail>(["run", run.id], (prev) => (prev ? { ...prev, run: { ...prev.run, ...r } } : prev));
      void qc.invalidateQueries({ queryKey: ["run", run.id] });
      toast.success("Answer sent", "Muse is planning with your answer.");
    },
    onError: (e) => toast.error("Couldn't send your answer", errorMessage(e)),
  });
  return (
    <section className="animate-rise overflow-hidden rounded-xl border border-ask-line bg-panel shadow-pop" aria-labelledby="clarify-title">
      <div className="flex items-start gap-3 border-b border-line bg-[color-mix(in_oklch,var(--ask-bg)_60%,var(--panel))] px-5 py-3.5">
        <MessageCircleQuestionMark className="mt-0.5 size-4 shrink-0 text-ask-fg" aria-hidden />
        <div>
          <h2 id="clarify-title" className="text-sm font-semibold text-ink">
            One question before planning
          </h2>
          <p className="mt-0.5 text-xs text-ink-3">Jev judged the request likely to be missing information, so Muse asks instead of guessing.</p>
        </div>
      </div>
      <form
        className="space-y-3 px-5 py-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (answer.trim()) send.mutate();
        }}
      >
        <p className="text-md leading-relaxed text-ink">{run.clarification?.question ?? "What should I know before I plan this?"}</p>
        <label htmlFor="clarify" className="sr-only">
          Your answer
        </label>
        <textarea
          id="clarify"
          data-testid="clarify-input"
          value={answer}
          onChange={(e) => setAnswer(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && answer.trim()) {
              e.preventDefault();
              send.mutate();
            }
          }}
          rows={3}
          placeholder="Tomás and Elena, next week."
          className="block w-full resize-y rounded-lg border border-line-strong bg-panel px-3 py-2 text-sm text-ink outline-none focus:border-accent"
        />
        <div className="flex justify-end">
          <Button type="submit" variant="primary" data-testid="clarify-submit" disabled={!answer.trim()} loading={send.isPending}>
            Send answer
          </Button>
        </div>
      </form>
    </section>
  );
}

function SummaryCard({ run, effects }: { run: Run; effects: number }) {
  const goals = Object.values(run.plan?.nodes ?? {}).filter((n) => n.kind === "goal");
  const verified = goals.filter((g) => g.verification?.passed).length;
  const checks = goals.flatMap((g) => g.verification?.checks ?? []);
  const blocked = Object.values(run.plan?.nodes ?? {}).filter((n) => n.status === "blocked").length;
  return (
    <section className="animate-rise overflow-hidden rounded-xl border border-auto-line bg-panel shadow-card" aria-labelledby="summary-title">
      <header className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-line bg-[color-mix(in_oklch,var(--auto-bg)_55%,var(--panel))] px-5 py-3">
        <h2 id="summary-title" className="flex items-center gap-2 text-sm font-semibold text-ink">
          <BadgeCheck className="size-4 text-auto-fg" aria-hidden />
          Final report
        </h2>
        <p className="text-xs text-ink-3">
          Proof of done: <span className="font-medium text-ink-2">{verified} of {goals.length} goals verified</span> · {checks.filter((c) => c.passed).length}/{checks.length} criteria · {effects} ledger entries
          {blocked ? ` · ${blocked} blocked` : ""}
        </p>
      </header>
      <div className="px-5 py-4">
        {run.summary ? <Markdown source={run.summary} className="max-w-[80ch]" /> : <p className="text-sm text-ink-3">Muse is writing the summary.</p>}
      </div>
    </section>
  );
}

function RunSkeleton() {
  return (
    <div className="mx-auto max-w-[1480px] space-y-4 px-4 py-6 sm:px-6" aria-busy="true" aria-label="Loading run">
      <Skeleton className="h-4 w-16" />
      <Skeleton className="h-7 w-96 max-w-full" />
      <Skeleton className="h-4 w-[36rem] max-w-full" />
      <Skeleton className="h-[74px] w-full" />
      <div className="grid gap-4 xl:grid-cols-[1fr_420px]">
        <Skeleton className="h-[540px]" />
        <Skeleton className="h-[540px]" />
      </div>
    </div>
  );
}

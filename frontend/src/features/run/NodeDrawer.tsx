import { useEffect, useRef, type ReactNode } from "react";
import { ArrowRight, BadgeCheck, CircleDashed, CircleX, GitBranch, RefreshCw, Target, X } from "lucide-react";
import type { EffectRecord, Plan, PlanNode, RunEvent } from "../../lib/types";
import { NODE_STATUS, TONE, appMeta } from "../../lib/semantics";
import { clock, ms, pct } from "../../lib/format";
import { humanizeEvent } from "../../lib/events";
import { EffectStatusBadge, NodeStatusBadge, ReplannedBadge, TaintBadge, VerdictBadge } from "../../components/ui/badges";
import { JsonView } from "../../components/ui/JsonView";
import { RiskMeter, SignalBars } from "../../components/ui/signals";
import { Mono, cx } from "../../components/ui/primitives";

function Section({ title, children, meta }: { title: string; children: ReactNode; meta?: ReactNode }) {
  return (
    <section className="border-t border-line px-5 py-4">
      <div className="mb-2.5 flex items-baseline justify-between gap-3">
        <h3 className="text-xs font-semibold text-ink">{title}</h3>
        {meta && <div className="text-2xs text-ink-3">{meta}</div>}
      </div>
      {children}
    </section>
  );
}

function changedKeys(a: Record<string, unknown> | null | undefined, b: Record<string, unknown> | null | undefined): Set<string> {
  const out = new Set<string>();
  if (!a || !b) return out;
  for (const k of new Set([...Object.keys(a), ...Object.keys(b)])) if (JSON.stringify(a[k]) !== JSON.stringify(b[k])) out.add(k);
  return out;
}

export function NodeDrawer({
  node,
  plan,
  effects,
  events,
  onClose,
  onSelect,
}: {
  node: PlanNode;
  plan: Plan;
  effects: EffectRecord[];
  events: RunEvent[];
  onClose: () => void;
  onSelect: (id: string) => void;
}) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const returnTo = useRef<Element | null>(null);

  useEffect(() => {
    returnTo.current = document.activeElement;
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      if (returnTo.current instanceof HTMLElement) returnTo.current.focus();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const app = appMeta(node.tool);
  const effect = effects.find((e) => e.node_id === node.id);
  const nodeEvents = events.filter((e) => e.node_id === node.id);
  const parent = node.parent_id ? plan.nodes[node.parent_id] : null;
  const children = node.children.map((c) => plan.nodes[c]).filter(Boolean);
  const deps = node.depends_on.map((d) => plan.nodes[d]).filter(Boolean);
  const diff = changedKeys(node.args, node.resolved_args);
  const checks = node.verification?.checks ?? [];

  return (
    <div className="fixed inset-0 z-[70]" role="presentation">
      <div className="animate-fade absolute inset-0 bg-[color-mix(in_oklch,var(--ink)_18%,transparent)]" onClick={onClose} aria-hidden />
      <aside
        role="dialog"
        aria-modal="true"
        aria-labelledby="drawer-title"
        className="animate-drawer absolute inset-y-0 right-0 flex w-full max-w-[560px] flex-col border-l border-line bg-panel shadow-drawer"
      >
        <header className="flex items-start gap-3 px-5 pb-4 pt-4">
          <div className="grid size-9 shrink-0 place-items-center rounded-lg border border-line bg-panel-2 text-ink-2">
            {node.kind === "goal" ? <Target className="size-4" aria-hidden /> : <app.icon className="size-4" aria-hidden />}
          </div>
          <div className="min-w-0 flex-1">
            <p className="flex flex-wrap items-center gap-x-2 font-mono text-2xs text-ink-3">
              <span>{node.kind}</span>
              <span>{node.id}</span>
              {node.tool && <span>{node.tool}</span>}
              {node.optional && <span>optional</span>}
            </p>
            <h2 id="drawer-title" className="mt-0.5 text-base font-semibold leading-snug text-ink">
              {node.title}
            </h2>
            <div className="mt-2 flex flex-wrap items-center gap-1.5">
              <NodeStatusBadge status={node.status} />
              {node.gate && <VerdictBadge verdict={node.gate.verdict} risk={node.gate.risk} />}
              {node.tainted && <TaintBadge source={taintSource(node, plan)} />}
              {node.revision > 1 && <ReplannedBadge revision={node.revision} />}
              {node.attempts > 1 && <span className="font-mono text-2xs text-ink-3">{node.attempts} attempts</span>}
            </div>
          </div>
          <button
            ref={closeRef}
            type="button"
            onClick={onClose}
            className="grid size-8 shrink-0 place-items-center rounded-md text-ink-3 hover:bg-panel-3 hover:text-ink"
            aria-label="Close details"
          >
            <X className="size-4" aria-hidden />
          </button>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto pb-8">
          {node.rationale && (
            <Section title="Why Muse planned this">
              <p className="text-sm leading-relaxed text-ink-2">{node.rationale}</p>
            </Section>
          )}

          {(node.success_criteria.length > 0 || checks.length > 0) && (
            <Section
              title="Success criteria"
              meta={node.verification ? `proof of done · ${node.verification.model || "Jev"}` : "not verified yet"}
            >
              <ul className="space-y-2">
                {(checks.length ? checks : node.success_criteria.map((c) => ({ criterion: c, p: NaN, passed: false }))).map((c) => {
                  const verified = Number.isFinite(c.p);
                  return (
                    <li key={c.criterion} className="flex items-start gap-2.5">
                      {verified ? (
                        c.passed ? <BadgeCheck className="mt-0.5 size-4 shrink-0 text-auto-fg" aria-label="met" /> : <CircleX className="mt-0.5 size-4 shrink-0 text-block-fg" aria-label="not met" />
                      ) : (
                        <CircleDashed className="mt-0.5 size-4 shrink-0 text-ink-4" aria-label="pending" />
                      )}
                      <span className="min-w-0 flex-1 text-sm text-ink-2">{c.criterion}</span>
                      {verified && (
                        <span className="flex w-24 shrink-0 items-center gap-2">
                          <span className="relative h-1.5 flex-1 overflow-hidden rounded-full bg-panel-3" aria-hidden>
                            <span className={cx("absolute inset-y-0 left-0 rounded-full", c.passed ? "bg-auto-solid" : "bg-block-solid")} style={{ width: `${c.p * 100}%` }} />
                          </span>
                          <span className={cx("tnum font-mono text-2xs", c.passed ? "text-auto-fg" : "text-block-fg")}>{pct(c.p)}</span>
                        </span>
                      )}
                    </li>
                  );
                })}
              </ul>
            </Section>
          )}

          {node.kind === "goal" && children.length > 0 && (
            <Section title="Steps in this goal" meta={`${children.filter((c) => c.status === "succeeded").length} of ${children.length} done`}>
              <ul className="divide-y divide-line rounded-lg border border-line">
                {children.map((c) => (
                  <li key={c.id}>
                    <button type="button" onClick={() => onSelect(c.id)} className="flex w-full items-center gap-2.5 px-3 py-2 text-left hover:bg-panel-2">
                      <span className={cx("size-1.5 shrink-0 rounded-full", TONE[NODE_STATUS[c.status]?.tone ?? "neutral"].solid)} aria-hidden />
                      <span className="min-w-0 flex-1 truncate text-sm text-ink">{c.title}</span>
                      <Mono className="text-2xs text-ink-3">{c.tool ?? c.id}</Mono>
                      {c.gate && <VerdictBadge verdict={c.gate.verdict} size="xs" showIcon={false} />}
                    </button>
                  </li>
                ))}
              </ul>
            </Section>
          )}

          {node.gate && (
            <Section title="Gate decision" meta={`${node.gate.model || "Jev"} · ${node.gate.autonomy} autonomy`}>
              <RiskMeter risk={node.gate.risk} />
              {node.gate.reasons.length > 0 && (
                <ul className="mt-3 space-y-1.5">
                  {node.gate.reasons.map((r) => (
                    <li key={r} className="flex gap-2 text-sm leading-snug text-ink-2">
                      <span className={cx("mt-[7px] size-1.5 shrink-0 rounded-full", TONE[node.gate!.verdict === "block" ? "block" : node.gate!.verdict === "ask" ? "ask" : "auto"].solid)} aria-hidden />
                      {r}
                    </li>
                  ))}
                </ul>
              )}
              <div className="mt-4">
                <SignalBars signals={node.gate.signals} />
              </div>
              {node.gate.args_hash && (
                <p className="mt-3 text-2xs text-ink-3">
                  Bound to args hash <Mono className="text-ink-2">{node.gate.args_hash}</Mono>. If the arguments change, this decision no longer applies.
                </p>
              )}
            </Section>
          )}

          {node.kind === "action" && (
            <Section title="Arguments" meta={diff.size ? `${diff.size} resolved from templates` : undefined}>
              <div className="grid gap-3">
                <JsonView value={node.args} label="args (as planned)" />
                {node.resolved_args && diff.size > 0 && <JsonView value={node.resolved_args} label="resolved args (what runs)" highlight={diff} />}
                {node.resolved_args && diff.size === 0 && <p className="text-xs text-ink-3">Resolved arguments are identical to the plan.</p>}
                {!node.resolved_args && <p className="text-xs text-ink-3">Not resolved yet: templates are filled in when the node runs.</p>}
              </div>
            </Section>
          )}

          {node.result && (
            <Section
              title={node.result.simulated ? "Shadow result" : "Result"}
              meta={
                <span className="flex items-center gap-2">
                  {ms(node.result.latency_ms)}
                  {node.result.simulated && <span className="text-shadow-fg">simulated</span>}
                  {node.result.tainted && <span className="text-taint-fg">untrusted output</span>}
                </span>
              }
            >
              {node.result.ok ? (
                <JsonView value={node.result.output} label="output" empty="No output" />
              ) : (
                <div className="rounded-lg border border-block-line bg-block-bg px-3 py-2.5">
                  <p className="font-mono text-2xs font-semibold text-block-fg">{node.result.error?.kind ?? "error"}</p>
                  <p className="mt-0.5 text-sm text-ink">{node.result.error?.message ?? "The tool call failed."}</p>
                  {node.result.error?.retryable && <p className="mt-1 text-2xs text-ink-3">Marked retryable</p>}
                </div>
              )}
            </Section>
          )}

          {effect && (
            <Section title="Effect ledger entry" meta={<EffectStatusBadge status={effect.status} />}>
              <p className="text-sm text-ink-2">{effect.summary}</p>
              <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-2xs">
                <dt className="text-ink-3">idempotency key</dt>
                <dd className="break-all font-mono text-ink-2">{effect.idempotency_key}</dd>
                <dt className="text-ink-3">args hash</dt>
                <dd className="font-mono text-ink-2">{effect.args_hash}</dd>
                <dt className="text-ink-3">compensation</dt>
                <dd className="font-mono text-ink-2">{effect.compensation ? String((effect.compensation as { tool?: string }).tool ?? "yes") : "none: cannot be undone"}</dd>
              </dl>
            </Section>
          )}

          {node.recovery.length > 0 && (
            <Section title="Recovery history" meta={`${node.recovery.length} decision${node.recovery.length > 1 ? "s" : ""}`}>
              <ol className="space-y-3">
                {node.recovery.map((r, i) => (
                  <li key={i} className="rounded-lg border border-line bg-panel-2 px-3 py-2.5">
                    <p className="flex flex-wrap items-center gap-2 text-sm">
                      <RefreshCw className="size-3.5 text-ink-3" aria-hidden />
                      <Mono className="text-ink-2">{r.cause}</Mono>
                      <ArrowRight className="size-3 text-ink-4" aria-hidden />
                      <span className="font-medium text-ink">{r.strategy.replace(/_/g, " ")}</span>
                      <span className="ml-auto font-mono text-2xs text-ink-3">{pct(r.confidence)} confident</span>
                    </p>
                    {r.note && <p className="mt-1 text-xs leading-relaxed text-ink-3">{r.note}</p>}
                    {Object.keys(r.probabilities).length > 0 && (
                      <div className="mt-2">
                        <SignalBars dense signals={Object.fromEntries(Object.entries(r.probabilities).map(([k, v]) => [`p(${k})`, v]))} />
                      </div>
                    )}
                  </li>
                ))}
              </ol>
            </Section>
          )}

          <Section title="Structure">
            <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-xs">
              {parent && (
                <>
                  <dt className="text-ink-3">Parent goal</dt>
                  <dd>
                    <button type="button" className="text-left text-accent hover:underline" onClick={() => onSelect(parent.id)}>
                      {parent.title}
                    </button>
                  </dd>
                </>
              )}
              {deps.length > 0 && (
                <>
                  <dt className="flex items-center gap-1 text-ink-3">
                    <GitBranch className="size-3" aria-hidden /> Depends on
                  </dt>
                  <dd className="flex flex-wrap gap-1.5">
                    {deps.map((d) => (
                      <button key={d.id} type="button" onClick={() => onSelect(d.id)} className="rounded-[4px] border border-line bg-panel-2 px-1.5 font-mono text-2xs text-ink-2 hover:border-line-strong">
                        {d.id}
                      </button>
                    ))}
                  </dd>
                </>
              )}
              <dt className="text-ink-3">Plan revision</dt>
              <dd className="font-mono text-ink-2">{node.revision}</dd>
              {node.started_at && (
                <>
                  <dt className="text-ink-3">Started</dt>
                  <dd className="font-mono text-ink-2">{clock(node.started_at)}</dd>
                </>
              )}
              {node.finished_at && (
                <>
                  <dt className="text-ink-3">Finished</dt>
                  <dd className="font-mono text-ink-2">{clock(node.finished_at)}</dd>
                </>
              )}
            </dl>
          </Section>

          {nodeEvents.length > 0 && (
            <Section title="Events for this node" meta={nodeEvents.length}>
              <ol className="space-y-1">
                {nodeEvents.map((e) => {
                  const h = humanizeEvent(e, plan);
                  return (
                    <li key={e.seq} className="flex gap-2 text-xs">
                      <span className="w-14 shrink-0 font-mono text-2xs text-ink-4">{clock(e.ts)}</span>
                      <span className="min-w-0 text-ink-2">
                        {h.text}
                        {h.detail && <span className="text-ink-3"> · {h.detail}</span>}
                      </span>
                    </li>
                  );
                })}
              </ol>
            </Section>
          )}
        </div>
      </aside>
    </div>
  );
}

function taintSource(node: PlanNode, plan: Plan): string {
  const sources = node.depends_on
    .map((d) => plan.nodes[d])
    .filter((d) => d?.tainted)
    .map((d) => `${d.id} (${d.tool ?? d.title})`);
  if (sources.length) return `Derived from untrusted output of ${sources.join(", ")}. Taint propagates through templates and llm.* steps.`;
  if (node.tool) return `${node.tool} reads external content (email bodies, others' docs, transcripts or the web), so its output is marked untrusted.`;
  return "Built from untrusted content.";
}

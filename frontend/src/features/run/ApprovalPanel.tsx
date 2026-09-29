import { useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, ChevronDown, ListChecks, Pencil, ShieldBan, ShieldCheck, Undo2, X } from "lucide-react";
import { api, errorMessage } from "../../lib/api";
import type { Approval, ApprovalItem, EffectClass, EffectRecord, Plan, RunDetail } from "../../lib/types";
import { EFFECT_CLASS, appMeta } from "../../lib/semantics";
import { pct, plural } from "../../lib/format";
import { EffectClassTag, TaintBadge, VerdictBadge } from "../../components/ui/badges";
import { RiskMeter, SignalBars } from "../../components/ui/signals";
import { Button, Mono, cx } from "../../components/ui/primitives";
import { useToast } from "../../components/ui/Toast";
import { EDITABLE_KEYS, Preview, effectivePreview } from "./Previews";

type Decision = "approved" | "rejected";

interface Row {
  item: ApprovalItem;
  /** true when the item is only shown for context (BLOCK nodes the backend did not include) */
  derived: boolean;
}

const SAFE_RISK = 0.5;

export function ApprovalPanel({ runId, approval, plan, effects }: { runId: string; approval: Approval; plan: Plan | null; effects: EffectRecord[] }) {
  const toast = useToast();
  const qc = useQueryClient();
  const [decisions, setDecisions] = useState<Record<string, Decision>>(() => {
    const init: Record<string, Decision> = {};
    for (const it of approval.items) if (it.gate?.verdict === "block") init[it.node_id] = "rejected";
    return init;
  });
  const [edits, setEdits] = useState<Record<string, Record<string, string>>>({});
  const [editing, setEditing] = useState<Record<string, boolean>>({});
  const [showAuto, setShowAuto] = useState(false);

  const rows: Row[] = useMemo(() => {
    const listed = new Set(approval.items.map((i) => i.node_id));
    const derived: Row[] = Object.values(plan?.nodes ?? {})
      .filter((n) => n.gate?.verdict === "block" && !listed.has(n.id))
      .map((n) => {
        const fx = effects.find((e) => e.node_id === n.id);
        return {
          derived: true,
          item: {
            node_id: n.id,
            tool: n.tool ?? "",
            summary: fx?.summary ?? n.title,
            effect: (fx?.effect ?? "communicate") as EffectClass,
            args: n.resolved_args ?? n.args,
            args_hash: n.gate?.args_hash ?? "",
            preview: fx?.preview ?? {},
            gate: n.gate!,
            decision: "rejected",
          },
        };
      });
    return [...approval.items.map((item) => ({ item, derived: false })), ...derived];
  }, [approval.items, plan, effects]);

  const asks = rows.filter((r) => r.item.gate?.verdict !== "block");
  const blocks = rows.filter((r) => r.item.gate?.verdict === "block");
  const autos = Object.values(plan?.nodes ?? {}).filter((n) => n.gate?.verdict === "auto");

  const groups = useMemo(() => {
    const by = new Map<EffectClass, Row[]>();
    for (const r of asks) by.set(r.item.effect, [...(by.get(r.item.effect) ?? []), r]);
    return [...by.entries()].sort((a, b) => (EFFECT_CLASS[a[0]]?.order ?? 9) - (EFFECT_CLASS[b[0]]?.order ?? 9));
  }, [asks]);

  const decided = asks.filter((r) => decisions[r.item.node_id]).length;
  const approvedCount = asks.filter((r) => decisions[r.item.node_id] === "approved").length;
  const safeIds = asks.filter((r) => (r.item.gate?.risk ?? 1) < SAFE_RISK).map((r) => r.item.node_id);

  const submit = useMutation({
    mutationFn: () => {
      const d: Record<string, Decision> = {};
      const e: Record<string, Record<string, unknown>> = {};
      for (const { item } of rows) {
        if (!approval.items.some((i) => i.node_id === item.node_id)) continue; // derived BLOCK rows are context only
        const choice = item.gate?.verdict === "block" ? "rejected" : decisions[item.node_id] ?? "rejected";
        d[item.node_id] = choice;
        const ed = edits[item.node_id];
        if (choice === "approved" && ed && Object.keys(ed).length) e[item.node_id] = ed;
      }
      return api.resolveApproval(runId, approval.id, { decisions: d, edits: e });
    },
    onSuccess: (run) => {
      qc.setQueryData<RunDetail>(["run", runId], (prev) => (prev ? { ...prev, run: { ...prev.run, ...run }, approval: prev.approval ? { ...prev.approval, status: "resolved" } : prev.approval } : prev));
      void qc.invalidateQueries({ queryKey: ["run", runId] });
      toast.success(
        approvedCount ? `Review submitted. Committing ${plural(approvedCount, "approved change")}.` : "Review submitted. Nothing was approved.",
        Object.keys(edits).length ? "Edited items get a new args hash and are re-checked by Jev before they run." : undefined,
      );
    },
    onError: (e) => toast.error("Couldn't submit the review", errorMessage(e)),
  });

  const setDecision = (id: string, d: Decision) =>
    setDecisions((x) => {
      const next = { ...x };
      if (next[id] === d) delete next[id];
      else next[id] = d;
      return next;
    });

  const approveAllSafe = () => {
    setDecisions((x) => {
      const next = { ...x };
      for (const id of safeIds) next[id] = "approved";
      return next;
    });
    toast.info(
      safeIds.length ? `Approved ${plural(safeIds.length, "item")} under ${pct(SAFE_RISK)} risk` : `No items under ${pct(SAFE_RISK)} risk`,
      safeIds.length < asks.length ? "Higher-risk items still need your call." : undefined,
    );
  };

  return (
    <section
      data-testid="approval-panel"
      aria-labelledby="approval-title"
      className="animate-rise overflow-hidden rounded-xl border border-ask-line bg-panel shadow-pop"
    >
      <header className="flex flex-col gap-3 border-b border-line bg-[color-mix(in_oklch,var(--ask-bg)_60%,var(--panel))] px-4 py-3.5 sm:px-5 lg:flex-row lg:items-center">
        <div className="min-w-0 flex-1">
          <h2 id="approval-title" className="text-base font-semibold leading-snug text-ink">
            Review the plan diff — {asks.length} {asks.length === 1 ? "change needs" : "changes need"} you, {autos.length} run automatically
          </h2>
          <p className="mt-0.5 text-xs leading-relaxed text-ink-2">
            The shadow run is finished and nothing has been sent. Each approval binds to the exact arguments you see here.
            {blocks.length > 0 && ` ${plural(blocks.length, "item was", "items were")} blocked by policy and cannot run.`}
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          <Button
            size="md"
            variant="secondary"
            icon={ListChecks}
            data-testid="approval-approve-all-safe"
            onClick={approveAllSafe}
            disabled={submit.isPending || asks.length === 0}
            title={`Approve every non-blocked item with risk under ${pct(SAFE_RISK)}`}
          >
            Approve all safe
          </Button>
          <Button size="md" variant="primary" icon={Check} data-testid="approval-submit" onClick={() => submit.mutate()} loading={submit.isPending}>
            Submit review
            <span className="tnum ml-0.5 text-2xs font-normal opacity-70">
              {approvedCount} of {asks.length} approved
            </span>
          </Button>
        </div>
      </header>

      {decided < asks.length && asks.length > 0 && (
        <p className="border-b border-line px-5 py-2 text-2xs text-ink-3" aria-live="polite">
          {decided} of {asks.length} decided. Anything you leave undecided is treated as rejected, so nothing runs without a yes.
        </p>
      )}

      <div className="divide-y divide-line">
        {groups.map(([effect, list]) => {
          const meta = EFFECT_CLASS[effect] ?? EFFECT_CLASS.communicate;
          return (
            <div key={effect} className="px-4 py-4 sm:px-5">
              <GroupHeading icon={meta.icon} title={meta.label} hint={meta.hint} count={list.length} />
              <div className="mt-3 space-y-3">
                {list.map(({ item }) => (
                  <ItemCard
                    key={item.node_id}
                    item={item}
                    decision={decisions[item.node_id]}
                    onDecide={(d) => setDecision(item.node_id, d)}
                    editing={!!editing[item.node_id]}
                    onToggleEdit={() => setEditing((x) => ({ ...x, [item.node_id]: !x[item.node_id] }))}
                    edits={edits[item.node_id]}
                    onEdit={(k, v) =>
                      setEdits((x) => {
                        const cur = { ...(x[item.node_id] ?? {}) };
                        const original = String((item.preview as Record<string, unknown>)[k] ?? (item.args as Record<string, unknown>)[k] ?? "");
                        if (v === original) delete cur[k];
                        else cur[k] = v;
                        const next = { ...x, [item.node_id]: cur };
                        if (!Object.keys(cur).length) delete next[item.node_id];
                        return next;
                      })
                    }
                    onResetEdits={() =>
                      setEdits((x) => {
                        const next = { ...x };
                        delete next[item.node_id];
                        return next;
                      })
                    }
                  />
                ))}
              </div>
            </div>
          );
        })}

        {blocks.length > 0 && (
          <div className="bg-[color-mix(in_oklch,var(--block-bg)_35%,var(--panel))] px-4 py-4 sm:px-5">
            <GroupHeading icon={ShieldBan} title="Blocked by policy" hint="Hard rules in code. These cannot be approved from here." count={blocks.length} tone="block" />
            <div className="mt-3 space-y-3">
              {blocks.map(({ item }) => (
                <ItemCard key={item.node_id} item={item} decision="rejected" blocked onDecide={() => undefined} editing={false} onToggleEdit={() => undefined} onEdit={() => undefined} onResetEdits={() => undefined} />
              ))}
            </div>
          </div>
        )}

        {autos.length > 0 && (
          <div className="px-4 py-3 sm:px-5">
            <button
              type="button"
              onClick={() => setShowAuto((x) => !x)}
              aria-expanded={showAuto}
              className="flex w-full items-center gap-2 text-left text-sm font-medium text-ink-2 hover:text-ink"
            >
              <ShieldCheck className="size-4 text-auto-fg" aria-hidden />
              Runs automatically
              <span className="text-xs font-normal text-ink-3">{autos.length} low-risk {autos.length === 1 ? "change" : "changes"} the gate cleared</span>
              <ChevronDown className={cx("ml-auto size-4 text-ink-3 transition-transform duration-200", showAuto && "rotate-180")} aria-hidden />
            </button>
            {showAuto && (
              <ul className="mt-3 space-y-2">
                {autos.map((n) => {
                  const fx = effects.find((e) => e.node_id === n.id);
                  const app = appMeta(n.tool);
                  return (
                    <li key={n.id} data-testid="approval-auto-item" data-node-id={n.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-line px-3 py-2">
                      <app.icon className="size-3.5 text-ink-3" aria-hidden />
                      <span className="min-w-0 flex-1 text-sm text-ink">{fx?.summary ?? n.title}</span>
                      <Mono className="text-2xs text-ink-3">{n.tool}</Mono>
                      {n.gate && <VerdictBadge verdict="auto" risk={n.gate.risk} size="xs" />}
                      {n.gate?.reasons[0] && <p className="w-full text-xs text-ink-3">{n.gate.reasons[0]}</p>}
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        )}
      </div>
    </section>
  );
}

function GroupHeading({ icon: Icon, title, hint, count, tone }: { icon: typeof Check; title: string; hint?: string; count: number; tone?: "block" }) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
      <h3 className={cx("flex items-center gap-1.5 text-sm font-semibold", tone === "block" ? "text-block-fg" : "text-ink")}>
        <Icon className="size-4 self-center" aria-hidden />
        {title}
        <span className="tnum font-mono text-2xs font-medium text-ink-3">{count}</span>
      </h3>
      {hint && <p className="text-xs text-ink-3">{hint}</p>}
    </div>
  );
}

function ItemCard({
  item,
  decision,
  onDecide,
  blocked,
  editing,
  onToggleEdit,
  edits,
  onEdit,
  onResetEdits,
}: {
  item: ApprovalItem;
  decision: Decision | undefined;
  onDecide: (d: Decision) => void;
  blocked?: boolean;
  editing: boolean;
  onToggleEdit: () => void;
  edits?: Record<string, string>;
  onEdit: (key: string, value: string) => void;
  onResetEdits: () => void;
}) {
  const g = item.gate;
  const pv = effectivePreview(item.preview as Record<string, unknown>, item.args as Record<string, unknown>, edits);
  const editable = EDITABLE_KEYS.filter((k) => typeof (item.args as Record<string, unknown>)[k] === "string" || typeof (item.preview as Record<string, unknown>)[k] === "string");
  const tainted = (g?.signals?.tainted ?? 0) >= 0.5;
  const edited = !!edits && Object.keys(edits).length > 0;
  const app = appMeta(item.tool);

  return (
    <article
      data-testid="approval-item"
      data-node-id={item.node_id}
      data-verdict={g?.verdict ?? "ask"}
      aria-label={`${item.summary}: ${g?.verdict ?? "ask"}`}
      className={cx(
        "overflow-hidden rounded-xl border bg-panel transition-[border-color,box-shadow] duration-200",
        blocked ? "border-block-line" : decision === "approved" ? "border-auto-line shadow-[0_0_0_1px_var(--auto-line)]" : decision === "rejected" ? "border-line opacity-80" : "border-line-strong",
      )}
    >
      <div className="flex flex-col gap-3 border-b border-line px-4 py-3 sm:flex-row sm:items-center">
        <div className="flex min-w-0 flex-1 items-start gap-2.5">
          <span className="mt-0.5 grid size-7 shrink-0 place-items-center rounded-md border border-line bg-panel-2 text-ink-2">
            <app.icon className="size-3.5" aria-hidden />
          </span>
          <div className="min-w-0">
            <p className="text-sm font-medium leading-snug text-ink">{item.summary}</p>
            <div className="mt-1 flex flex-wrap items-center gap-1.5">
              {g && <VerdictBadge verdict={g.verdict} risk={g.risk} />}
              {tainted && <TaintBadge />}
              <EffectClassTag effect={item.effect} />
              <Mono className="text-2xs text-ink-3">
                {item.tool} · {item.node_id}
              </Mono>
              {edited && (
                <span className="inline-flex h-5 items-center gap-1 rounded-[5px] border border-accent-line bg-accent-soft px-1.5 text-2xs font-medium text-accent">
                  <Pencil className="size-3" aria-hidden /> edited
                </span>
              )}
            </div>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {!blocked && editable.length > 0 && (
            <Button size="sm" variant="ghost" icon={Pencil} onClick={onToggleEdit} aria-pressed={editing}>
              {editing ? "Done editing" : "Edit"}
            </Button>
          )}
          <div role="group" aria-label={`Decision for ${item.node_id}`} className="inline-flex rounded-lg border border-line-strong bg-panel-2 p-0.5">
            <button
              type="button"
              data-testid={`approval-approve-${item.node_id}`}
              aria-pressed={decision === "approved"}
              disabled={blocked}
              title={blocked ? "Blocked by policy: this cannot be approved" : "Approve this change"}
              onClick={() => onDecide("approved")}
              className={cx(
                "inline-flex h-7 items-center gap-1 rounded-md px-2.5 text-xs font-semibold transition-colors duration-150 disabled:cursor-not-allowed disabled:opacity-40",
                decision === "approved" ? "bg-auto-solid text-[oklch(0.99_0_0)] shadow-card" : "text-ink-2 hover:bg-panel hover:text-ink",
              )}
            >
              <Check className="size-3.5" aria-hidden strokeWidth={2.5} /> Approve
            </button>
            <button
              type="button"
              data-testid={`approval-reject-${item.node_id}`}
              aria-pressed={decision === "rejected"}
              disabled={blocked}
              onClick={() => onDecide("rejected")}
              className={cx(
                "inline-flex h-7 items-center gap-1 rounded-md px-2.5 text-xs font-semibold transition-colors duration-150 disabled:cursor-not-allowed",
                decision === "rejected" ? (blocked ? "bg-block-bg text-block-fg" : "bg-panel text-block-fg shadow-card") : "text-ink-2 hover:bg-panel hover:text-ink",
              )}
            >
              <X className="size-3.5" aria-hidden strokeWidth={2.5} /> {blocked ? "Blocked" : "Reject"}
            </button>
          </div>
        </div>
      </div>

      <div className="grid gap-4 p-4 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <div className="min-w-0">
          {editing ? (
            <div className="space-y-3">
              {editable.map((k) => {
                const original = String((item.preview as Record<string, unknown>)[k] ?? (item.args as Record<string, unknown>)[k] ?? "");
                const value = edits?.[k] ?? original;
                const long = k === "body" || k === "text" || k === "content_md" || k === "description";
                const id = `edit-${item.node_id}-${k}`;
                return (
                  <div key={k}>
                    <label htmlFor={id} className="mb-1 block text-2xs font-medium text-ink-3">
                      {k.replace("_md", "").replace(/_/g, " ")}
                    </label>
                    {long ? (
                      <textarea
                        id={id}
                        value={value}
                        onChange={(e) => onEdit(k, e.target.value)}
                        rows={Math.min(14, Math.max(4, value.split("\n").length + 1))}
                        className="w-full resize-y rounded-lg border border-line-strong bg-panel px-3 py-2 text-sm leading-relaxed text-ink outline-none focus:border-accent"
                      />
                    ) : (
                      <input
                        id={id}
                        value={value}
                        onChange={(e) => onEdit(k, e.target.value)}
                        className="h-9 w-full rounded-lg border border-line-strong bg-panel px-3 text-sm text-ink outline-none focus:border-accent"
                      />
                    )}
                  </div>
                );
              })}
              <div className="flex items-center justify-between gap-3">
                <p className="text-2xs text-ink-3">Edits create a new args hash; Jev re-checks the change before it runs.</p>
                {edited && (
                  <Button size="xs" variant="ghost" icon={Undo2} onClick={onResetEdits}>
                    Reset
                  </Button>
                )}
              </div>
            </div>
          ) : (
            <Preview tool={item.tool} pv={pv} />
          )}
        </div>
        {g && (
          <div className="min-w-0 space-y-3 rounded-lg border border-line bg-panel-2 p-3">
            <RiskMeter risk={g.risk} />
            {g.reasons.length > 0 && (
              <ul className="space-y-1.5">
                {g.reasons.map((r) => (
                  <li key={r} className="flex gap-2 text-xs leading-snug text-ink-2">
                    <span className={cx("mt-[5px] size-1.5 shrink-0 rounded-full", g.verdict === "block" ? "bg-block-solid" : g.verdict === "ask" ? "bg-ask-solid" : "bg-auto-solid")} aria-hidden />
                    {r}
                  </li>
                ))}
              </ul>
            )}
            <SignalBars signals={g.signals} dense />
            <p className="border-t border-line pt-2 text-2xs text-ink-3">
              {g.model || "Jev"} · args hash <Mono className="text-ink-2">{item.args_hash || g.args_hash || "n/a"}</Mono>
            </p>
          </div>
        )}
      </div>
    </article>
  );
}

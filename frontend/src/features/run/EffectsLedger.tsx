import { useState } from "react";
import { BookOpenCheck, Check, Copy, Undo2 } from "lucide-react";
import type { EffectRecord, Plan } from "../../lib/types";
import { clock, relative, truncateMiddle } from "../../lib/format";
import { AppIcon, EffectClassTag, EffectStatusBadge } from "../../components/ui/badges";
import { Button, EmptyState, cx } from "../../components/ui/primitives";
import { Tooltip } from "../../components/ui/Tooltip";

function reversibility(e: EffectRecord): { ok: boolean; label: string; hint: string } {
  if (e.effect === "communicate") {
    return e.compensation
      ? { ok: false, label: "Audience saw it", hint: `Compensation (${String((e.compensation as { tool?: string }).tool ?? "")}) removes the artifact, but people were already notified. Treated as non-reversible.` }
      : { ok: false, label: "Can't unsend", hint: "Communications cannot be unsent. Rollback lists them but cannot undo them." };
  }
  if (e.effect === "write_irreversible") return { ok: false, label: "Permanent", hint: "No compensation exists for this change." };
  if (e.compensation) return { ok: true, label: String((e.compensation as { tool?: string }).tool ?? "Reversible"), hint: "Rollback runs this compensation, newest first." };
  return { ok: false, label: "No compensation", hint: "No compensation was recorded." };
}

function KeyCell({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  if (!value) return <span className="text-ink-4">none</span>;
  return (
    <span className="inline-flex items-center gap-1">
      <Tooltip content={<span className="break-all font-mono">{value}</span>}>
        <span tabIndex={0} className="font-mono text-[11px] text-ink-2">
          {truncateMiddle(value, 20)}
        </span>
      </Tooltip>
      <button
        type="button"
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(value);
            setCopied(true);
            setTimeout(() => setCopied(false), 1200);
          } catch {
            /* blocked */
          }
        }}
        className="grid size-5 place-items-center rounded text-ink-4 hover:bg-panel-3 hover:text-ink"
        aria-label="Copy idempotency key"
      >
        {copied ? <Check className="size-3" aria-hidden /> : <Copy className="size-3" aria-hidden />}
      </button>
    </span>
  );
}

export function EffectsLedger({
  effects,
  plan,
  onSelectNode,
  canRollback,
  onRollback,
  rollingBack,
}: {
  effects: EffectRecord[];
  plan: Plan | null | undefined;
  onSelectNode: (id: string) => void;
  canRollback: boolean;
  onRollback: () => void;
  rollingBack: boolean;
}) {
  const sorted = [...effects].sort((a, b) => (b.applied_at ?? b.created_at) - (a.applied_at ?? a.created_at));
  const applied = effects.filter((e) => e.status === "applied").length;
  const reversible = effects.filter((e) => e.status === "applied" && reversibility(e).ok).length;
  return (
    <section data-testid="effects-ledger" className="overflow-hidden rounded-xl border border-line bg-panel shadow-card" aria-labelledby="ledger-title">
      <header className="flex min-h-11 flex-wrap items-center gap-x-3 gap-y-1 border-b border-line px-4 py-2">
        <h2 id="ledger-title" className="flex items-center gap-2 text-sm font-semibold text-ink">
          <BookOpenCheck className="size-4 text-ink-3" aria-hidden />
          Effect ledger
        </h2>
        <p className="text-xs text-ink-3">Intent before every write, applied after. Same key, never twice.</p>
        <div className="ml-auto flex items-center gap-2">
          <span className="text-2xs text-ink-3">
            {applied} applied · {reversible} reversible
          </span>
          <Button size="sm" variant="danger" icon={Undo2} disabled={!canRollback} loading={rollingBack} onClick={onRollback} data-testid="ledger-rollback">
            Roll back
          </Button>
        </div>
      </header>
      {sorted.length === 0 ? (
        <EmptyState icon={BookOpenCheck} title="No effects yet">
          Every write the agent previews or applies lands here with its idempotency key, status and whether it can be undone.
        </EmptyState>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[660px] text-left text-sm">
            <thead className="bg-panel-2 text-2xs text-ink-3">
              <tr>
                <th className="px-4 py-2 font-medium">Status</th>
                <th className="px-3 py-2 font-medium">Change</th>
                <th className="px-3 py-2 font-medium">Idempotency key</th>
                <th className="px-3 py-2 font-medium">Applied</th>
                <th className="px-4 py-2 font-medium">Undo</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {sorted.map((e) => {
                const r = reversibility(e);
                const node = plan?.nodes?.[e.node_id];
                return (
                  <tr key={e.id} data-testid="effect-item" data-status={e.status} data-effect-id={e.id} className="transition-colors hover:bg-panel-2">
                    <td className="whitespace-nowrap px-4 py-2.5 align-top">
                      <EffectStatusBadge status={e.status} />
                    </td>
                    <td className="px-3 py-2.5 align-top">
                      <button type="button" onClick={() => node && onSelectNode(e.node_id)} className={cx("block text-left text-[13px] leading-snug text-ink", node && "hover:underline")}>
                        {e.summary}
                      </button>
                      <span className="mt-1 flex flex-wrap items-center gap-x-2.5 gap-y-0.5">
                        <AppIcon tool={e.app || e.tool} withLabel />
                        <EffectClassTag effect={e.effect} />
                        <span className="font-mono text-[10.5px] text-ink-3">
                          {e.node_id} · {e.tool}
                        </span>
                      </span>
                    </td>
                    <td className="whitespace-nowrap px-3 py-2.5 align-top">
                      <KeyCell value={e.idempotency_key} />
                    </td>
                    <td className="whitespace-nowrap px-3 py-2.5 align-top text-xs text-ink-2">
                      {e.applied_at ? (
                        <span title={new Date(e.applied_at).toLocaleString()}>
                          <span className="font-mono">{clock(e.applied_at)}</span>
                          <span className="block text-2xs text-ink-3">{relative(e.applied_at)}</span>
                        </span>
                      ) : e.compensated_at ? (
                        <span className="text-undo-fg">undone {relative(e.compensated_at)}</span>
                      ) : (
                        <span className="text-ink-4">{e.status === "simulated" ? "not applied" : "pending"}</span>
                      )}
                    </td>
                    <td className="whitespace-nowrap px-4 py-2.5 align-top">
                      <Tooltip content={r.hint}>
                        <span tabIndex={0} className={cx("inline-flex items-center gap-1 text-xs", r.ok ? "text-undo-fg" : "text-ink-3")}>
                          {r.ok ? <Undo2 className="size-3.5" aria-hidden /> : null}
                          <span className={r.ok ? "font-mono text-[11px]" : ""}>{r.label}</span>
                        </span>
                      </Tooltip>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

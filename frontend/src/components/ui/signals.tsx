import { SIGNALS, TONE, riskTone, signalGood, signalLabel, signalTone } from "../../lib/semantics";
import { pct } from "../../lib/format";
import { cx } from "./primitives";
import { Tooltip } from "./Tooltip";

const FLAGS = new Set(["external", "unknown_recipient", "tainted"]);

function orderedSignals(signals: Record<string, number>): [string, number][] {
  const known = SIGNALS.map((s) => s.key).filter((k) => k in signals);
  const extra = Object.keys(signals).filter((k) => !known.includes(k)).sort();
  return [...known, ...extra]
    .map((k) => [k, Number(signals[k])] as [string, number])
    .filter(([, v]) => Number.isFinite(v));
}

/** Labelled bars for Jev's raw gate signals (probabilities) and code's flags. */
export function SignalBars({ signals, dense }: { signals: Record<string, number>; dense?: boolean }) {
  const rows = orderedSignals(signals ?? {});
  if (!rows.length) return <p className="text-xs text-ink-3">No signals recorded.</p>;
  return (
    <dl className={cx("grid gap-x-3", dense ? "gap-y-1.5" : "gap-y-2")} style={{ gridTemplateColumns: "minmax(7rem,auto) 1fr auto" }}>
      {rows.map(([key, v]) => {
        const tone = signalTone(key, v);
        const meta = SIGNALS.find((s) => s.key === key);
        const good = signalGood(key);
        return (
          <div key={key} className="contents">
            <dt className="flex items-center text-xs text-ink-2">
              <Tooltip content={meta?.hint ?? key}>
                <span tabIndex={0} className="cursor-help underline decoration-line-strong decoration-dotted underline-offset-4">
                  {signalLabel(key)}
                </span>
              </Tooltip>
            </dt>
            {FLAGS.has(key) ? (
              <>
                <dd className="flex items-center">
                  <span className={cx("h-1.5 w-full rounded-full", v >= 0.5 ? TONE[tone].solid : "bg-panel-3")} aria-hidden />
                </dd>
                <dd className={cx("text-right font-mono text-2xs font-medium", v >= 0.5 ? TONE[tone].fg : "text-ink-3")}>
                  {v >= 0.5 ? "yes" : "no"}
                </dd>
              </>
            ) : (
              <>
                <dd className="flex items-center" aria-hidden>
                  <span className="relative h-1.5 w-full overflow-hidden rounded-full bg-panel-3">
                    <span
                      className={cx("animate-grow-x absolute inset-y-0 left-0 rounded-full", TONE[tone].solid)}
                      style={{ width: `${Math.max(2, Math.min(100, v * 100))}%` }}
                    />
                  </span>
                </dd>
                <dd
                  className={cx("tnum w-10 text-right font-mono text-2xs font-medium", TONE[tone].fg)}
                  aria-label={`${signalLabel(key)} ${pct(v)}, ${good === "high" ? "higher is better" : "lower is better"}`}
                >
                  {pct(v)}
                </dd>
              </>
            )}
          </div>
        );
      })}
    </dl>
  );
}

/** Composite risk on a zoned scale: the zones mirror the policy's auto / ask thresholds. */
export function RiskMeter({ risk, className, zones = [0.35, 0.6] }: { risk: number; className?: string; zones?: [number, number] }) {
  const r = Math.max(0, Math.min(1, risk || 0));
  const tone = riskTone(r);
  return (
    <div className={cx("w-full", className)}>
      <div className="flex items-baseline justify-between text-2xs text-ink-3">
        <span>Composite risk</span>
        <span className={cx("tnum font-mono text-xs font-semibold", TONE[tone].fg)}>{pct(r)}</span>
      </div>
      <div className="relative mt-1.5 h-2" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(r * 100)} aria-label="Composite risk">
        <div className="absolute inset-0 flex overflow-hidden rounded-full">
          <span className="h-full bg-auto-bg" style={{ width: `${zones[0] * 100}%` }} />
          <span className="h-full bg-ask-bg" style={{ width: `${(zones[1] - zones[0]) * 100}%` }} />
          <span className="h-full flex-1 bg-block-bg" />
        </div>
        <div className="absolute inset-0 rounded-full border border-line" />
        <span
          className={cx("absolute top-1/2 size-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-panel shadow-card", TONE[tone].solid)}
          style={{ left: `${r * 100}%` }}
          aria-hidden
        />
      </div>
    </div>
  );
}

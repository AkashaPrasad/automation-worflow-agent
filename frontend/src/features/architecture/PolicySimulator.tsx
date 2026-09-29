import { useMemo, useState } from "react";
import type { Autonomy, EffectClass, Verdict } from "../../lib/types";
import { EFFECT_CLASS, TONE, VERDICT } from "../../lib/semantics";
import { pct } from "../../lib/format";
import { RiskMeter } from "../../components/ui/signals";
import { cx } from "../../components/ui/primitives";

const DEFAULT_W: Record<string, number> = {
  misalignment: 0.7,
  injection: 0.6,
  sensitive: 0.6,
  tone: 0.7,
  recipient_mismatch: 0.2,
  external: 0.15,
  unknown_recipient: 0.15,
  tainted: 0.1,
};
const DEFAULT_T: Record<string, number> = {
  block_injection: 0.7,
  block_sensitive: 0.8,
  balanced_reversible_max_risk: 0.35,
  balanced_comm_min_alignment: 0.66,
  balanced_comm_max_injection: 0.2,
  balanced_comm_max_sensitive: 0.3,
  balanced_comm_min_tone: 0.7,
  autonomous_max_risk: 0.6,
  autonomous_tainted_max_injection: 0.3,
};

interface Sig {
  alignment: number;
  injection: number;
  sensitive: number;
  tone_ok: number;
  recipients_match: number;
  external: boolean;
  unknown_recipient: boolean;
  tainted: boolean;
}

const PRESETS: { id: string; label: string; effect: EffectClass; s: Sig }[] = [
  { id: "ops", label: "Heads-up in #ops", effect: "communicate", s: { alignment: 0.9, injection: 0.02, sensitive: 0.05, tone_ok: 0.93, recipients_match: 0.95, external: false, unknown_recipient: false, tainted: false } },
  { id: "dana", label: "Reply to Dana (Northwind)", effect: "communicate", s: { alignment: 0.92, injection: 0.04, sensitive: 0.12, tone_ok: 0.88, recipients_match: 0.97, external: true, unknown_recipient: false, tainted: true } },
  { id: "globex", label: "Globex forward (injection)", effect: "communicate", s: { alignment: 0.08, injection: 0.93, sensitive: 0.91, tone_ok: 0.71, recipients_match: 0.05, external: true, unknown_recipient: true, tainted: true } },
  { id: "notion", label: "Notion page from a transcript", effect: "write_reversible", s: { alignment: 0.93, injection: 0.03, sensitive: 0.18, tone_ok: 1, recipients_match: 1, external: false, unknown_recipient: false, tainted: true } },
];

function num(o: Record<string, unknown> | undefined, k: string, d: number): number {
  const v = o?.[k];
  return typeof v === "number" && Number.isFinite(v) ? v : d;
}

/** Client-side mirror of policy.py, for explanation only. The server is the authority. */
export function evaluate(effect: EffectClass, autonomy: Autonomy, s: Sig, w: Record<string, number>, t: Record<string, number>) {
  const comm = effect === "communicate";
  const parts: [string, number][] = [
    ["misalignment", w.misalignment * (1 - s.alignment)],
    ["injection", w.injection * s.injection],
    ["sensitive", w.sensitive * s.sensitive],
    ["tone", comm ? w.tone * (1 - s.tone_ok) : 0],
    ["recipient_mismatch", comm ? w.recipient_mismatch * (1 - s.recipients_match) : 0],
    ["external", s.external ? w.external : 0],
    ["unknown_recipient", s.unknown_recipient ? w.unknown_recipient : 0],
    ["tainted", s.tainted ? w.tainted : 0],
  ];
  const risk = Math.min(1, parts.reduce((a, [, v]) => a + v, 0));
  let verdict: Verdict;
  let rule: string;
  if (effect === "read") [verdict, rule] = ["auto", "READ tools are always AUTO."];
  else if (s.injection >= t.block_injection && s.tainted) [verdict, rule] = ["block", `injection ≥ ${t.block_injection} on tainted args → BLOCK.`];
  else if (s.sensitive >= t.block_sensitive && s.external) [verdict, rule] = ["block", `sensitive ≥ ${t.block_sensitive} to an external recipient → BLOCK.`];
  else if (effect === "write_irreversible") [verdict, rule] = ["ask", "write_irreversible → ASK, never AUTO."];
  else if (autonomy === "cautious") [verdict, rule] = ["ask", "cautious: every non-read action → ASK."];
  else if (autonomy === "balanced") {
    if (effect === "write_reversible") {
      const ok = risk < t.balanced_reversible_max_risk && !s.tainted;
      [verdict, rule] = [ok ? "auto" : "ask", `balanced, reversible: AUTO if risk < ${t.balanced_reversible_max_risk} and not tainted.`];
    } else {
      const ok =
        !s.tainted &&
        !s.external &&
        !s.unknown_recipient &&
        s.alignment >= t.balanced_comm_min_alignment &&
        s.injection < t.balanced_comm_max_injection &&
        s.sensitive < t.balanced_comm_max_sensitive &&
        s.tone_ok >= t.balanced_comm_min_tone;
      [verdict, rule] = [ok ? "auto" : "ask", "balanced, communicate: AUTO only if untainted, internal, known, aligned, clean and on-tone."];
    }
  } else {
    const ok = risk < t.autonomous_max_risk && !(s.tainted && s.injection >= t.autonomous_tainted_max_injection);
    [verdict, rule] = [ok ? "auto" : "ask", `autonomous: AUTO if risk < ${t.autonomous_max_risk} and not (tainted and injection ≥ ${t.autonomous_tainted_max_injection}).`];
  }
  return { risk, verdict, rule, parts };
}

export function PolicySimulator({ weights, thresholds }: { weights?: Record<string, unknown>; thresholds?: Record<string, unknown> }) {
  const w = useMemo(() => Object.fromEntries(Object.entries(DEFAULT_W).map(([k, d]) => [k, num(weights, k, d)])), [weights]);
  const t = useMemo(() => Object.fromEntries(Object.entries(DEFAULT_T).map(([k, d]) => [k, num(thresholds, k, d)])), [thresholds]);
  const [preset, setPreset] = useState("dana");
  const [effect, setEffect] = useState<EffectClass>("communicate");
  const [autonomy, setAutonomy] = useState<Autonomy>("balanced");
  const [s, setS] = useState<Sig>(PRESETS[1].s);

  const out = evaluate(effect, autonomy, s, w, t);
  const v = VERDICT[out.verdict];
  const VIcon = v.icon;

  const load = (id: string) => {
    const p = PRESETS.find((x) => x.id === id);
    if (!p) return;
    setPreset(id);
    setEffect(p.effect);
    setS(p.s);
  };

  const slider = (key: keyof Sig, label: string, good: "high" | "low", only?: boolean) => {
    const val = s[key] as number;
    const bad = good === "high" ? 1 - val : val;
    const tone = bad >= 0.7 ? "block" : bad >= 0.3 ? "ask" : "auto";
    const id = `sim-${key}`;
    return (
      <div key={key} className={cx(only === false && "opacity-45")}>
        <div className="flex items-baseline justify-between">
          <label htmlFor={id} className="text-xs text-ink-2">
            {label}
          </label>
          <span className={cx("tnum font-mono text-2xs", TONE[tone].fg)}>{pct(val)}</span>
        </div>
        <input
          id={id}
          type="range"
          min={0}
          max={1}
          step={0.01}
          value={val}
          onChange={(e) => {
            setPreset("");
            setS((x) => ({ ...x, [key]: Number(e.target.value) }));
          }}
          className="mt-1 w-full accent-[var(--accent)]"
        />
      </div>
    );
  };
  const toggle = (key: "external" | "unknown_recipient" | "tainted", label: string) => (
    <label key={key} className="flex cursor-pointer items-center justify-between gap-3 rounded-md border border-line bg-panel px-2.5 py-1.5 text-xs text-ink-2">
      {label}
      <input
        type="checkbox"
        checked={s[key]}
        onChange={(e) => {
          setPreset("");
          setS((x) => ({ ...x, [key]: e.target.checked }));
        }}
        className="size-3.5 accent-[var(--accent)]"
      />
    </label>
  );

  const comm = effect === "communicate";
  return (
    <div className="overflow-hidden rounded-xl border border-line bg-panel shadow-card">
      <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-3">
        <p className="text-sm font-semibold text-ink">Gate simulator</p>
        <p className="text-xs text-ink-3">Move Jev&rsquo;s signals and watch policy.py decide. Mirrors the server&rsquo;s rules; the server is the authority.</p>
      </div>
      <div className="flex flex-wrap gap-1.5 border-b border-line px-4 py-2.5">
        {PRESETS.map((p) => (
          <button
            key={p.id}
            type="button"
            aria-pressed={preset === p.id}
            onClick={() => load(p.id)}
            className={cx("h-7 rounded-md border px-2.5 text-xs font-medium transition-colors", preset === p.id ? "border-ink bg-inverse text-inverse-ink" : "border-line-strong text-ink-2 hover:bg-panel-3")}
          >
            {p.label}
          </button>
        ))}
      </div>
      <div className="grid gap-0 lg:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]">
        <div className="space-y-3 border-b border-line p-4 lg:border-b-0 lg:border-r">
          <div className="grid grid-cols-2 gap-3">
            <label className="text-xs text-ink-2">
              Effect class
              <select
                value={effect}
                onChange={(e) => {
                  setPreset("");
                  setEffect(e.target.value as EffectClass);
                }}
                className="mt-1 h-8 w-full rounded-md border border-line-strong bg-panel px-2 text-sm text-ink"
              >
                {(["read", "write_reversible", "write_irreversible", "communicate"] as EffectClass[]).map((e) => (
                  <option key={e} value={e}>
                    {EFFECT_CLASS[e].short}
                  </option>
                ))}
              </select>
            </label>
            <label className="text-xs text-ink-2">
              Autonomy
              <select value={autonomy} onChange={(e) => setAutonomy(e.target.value as Autonomy)} className="mt-1 h-8 w-full rounded-md border border-line-strong bg-panel px-2 text-sm text-ink">
                <option value="cautious">Cautious</option>
                <option value="balanced">Balanced</option>
                <option value="autonomous">Autonomous</option>
              </select>
            </label>
          </div>
          {slider("alignment", "Alignment (Jev score)", "high")}
          {slider("injection", "Injection (Jev noul)", "low")}
          {slider("sensitive", "Sensitive (Jev noul)", "low")}
          {slider("tone_ok", "Tone ok (communications)", "high", comm)}
          {slider("recipients_match", "Recipients match (communications)", "high", comm)}
          <div className="grid gap-1.5 sm:grid-cols-3">
            {toggle("external", "External")}
            {toggle("unknown_recipient", "Unknown recipient")}
            {toggle("tainted", "Tainted")}
          </div>
        </div>
        <div className="flex flex-col gap-4 p-4">
          <div className={cx("flex items-center gap-3 rounded-xl border px-4 py-3 transition-colors duration-300", TONE[v.tone].bg, TONE[v.tone].line)} aria-live="polite">
            <VIcon className={cx("size-7", TONE[v.tone].fg)} aria-hidden strokeWidth={2} />
            <div>
              <p className={cx("text-xl font-semibold tracking-[0.02em]", TONE[v.tone].fg)}>{v.label}</p>
              <p className="text-xs text-ink-2">{out.rule}</p>
            </div>
          </div>
          <RiskMeter risk={out.risk} zones={[t.balanced_reversible_max_risk, t.autonomous_max_risk]} />
          <div>
            <p className="text-2xs font-medium text-ink-3">Where the risk comes from</p>
            <ul className="mt-1.5 space-y-1">
              {out.parts
                .filter(([, v]) => v > 0.0005)
                .sort((a, b) => b[1] - a[1])
                .map(([k, val]) => (
                  <li key={k} className="grid grid-cols-[8.5rem_1fr_3rem] items-center gap-2 text-2xs">
                    <span className="font-mono text-ink-2">{k}</span>
                    <span className="h-1.5 overflow-hidden rounded-full bg-panel-3" aria-hidden>
                      <span className="block h-full rounded-full bg-ink-4" style={{ width: `${Math.min(100, val * 100)}%` }} />
                    </span>
                    <span className="tnum text-right font-mono text-ink-3">+{val.toFixed(2)}</span>
                  </li>
                ))}
            </ul>
            <p className="mt-2 font-mono text-[10.5px] leading-relaxed text-ink-4">risk = min(1, Σ weight × penalty)</p>
          </div>
        </div>
      </div>
    </div>
  );
}

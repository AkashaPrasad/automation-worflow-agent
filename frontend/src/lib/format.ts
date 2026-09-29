export function usd(n: number | null | undefined, digits?: number): string {
  const v = typeof n === "number" && Number.isFinite(n) ? n : 0;
  const d = digits ?? (v !== 0 && Math.abs(v) < 1 ? 4 : 2);
  return `$${v.toFixed(d)}`;
}

export function pct(n: number | null | undefined, digits = 0): string {
  const v = typeof n === "number" && Number.isFinite(n) ? n : 0;
  return `${(v * 100).toFixed(digits)}%`;
}

export function compact(n: number | null | undefined): string {
  const v = typeof n === "number" && Number.isFinite(n) ? n : 0;
  if (Math.abs(v) >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
  if (Math.abs(v) >= 10_000) return `${Math.round(v / 1000)}k`;
  if (Math.abs(v) >= 1000) return `${(v / 1000).toFixed(1)}k`;
  return String(Math.round(v));
}

export function ms(n: number | null | undefined): string {
  const v = typeof n === "number" && Number.isFinite(n) ? n : 0;
  if (v >= 10_000) return `${(v / 1000).toFixed(1)}s`;
  if (v >= 1000) return `${(v / 1000).toFixed(2)}s`;
  return `${Math.round(v)}ms`;
}

/** Accepts epoch ms, epoch seconds, or ISO strings. */
export function toDate(v: unknown): Date | null {
  if (v == null || v === "") return null;
  if (v instanceof Date) return v;
  if (typeof v === "number") return new Date(v < 1e12 ? v * 1000 : v);
  if (typeof v === "string") {
    const d = new Date(v);
    return Number.isNaN(d.getTime()) ? null : d;
  }
  return null;
}

const rtf = typeof Intl !== "undefined" ? new Intl.RelativeTimeFormat("en", { numeric: "auto" }) : null;

export function relative(v: unknown, now = Date.now()): string {
  const d = toDate(v);
  if (!d) return "";
  const diff = (d.getTime() - now) / 1000;
  const abs = Math.abs(diff);
  if (abs < 45) return diff < 0 ? "just now" : "in a moment";
  if (!rtf) return d.toLocaleString();
  if (abs < 3600) return rtf.format(Math.round(diff / 60), "minute");
  if (abs < 86_400) return rtf.format(Math.round(diff / 3600), "hour");
  if (abs < 86_400 * 7) return rtf.format(Math.round(diff / 86_400), "day");
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function clock(v: unknown): string {
  const d = toDate(v);
  if (!d) return "";
  return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

export function shortTime(v: unknown): string {
  const d = toDate(v);
  if (!d) return "";
  return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

export function dateTime(v: unknown): string {
  const d = toDate(v);
  if (!d) return "";
  return d.toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

/** "+01:23" elapsed since a start timestamp. */
export function elapsed(ts: number, start: number): string {
  const s = Math.max(0, Math.round((ts - start) / 1000));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `+${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`;
}

export function truncateMiddle(s: string, max = 18): string {
  if (!s || s.length <= max) return s;
  const keep = Math.max(4, Math.floor((max - 1) / 2));
  return `${s.slice(0, keep)}…${s.slice(-keep)}`;
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

export function humanize(s: string): string {
  return s.replace(/[_.]/g, " ").replace(/\s+/g, " ").trim();
}

export function capitalize(s: string): string {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

export function str(v: unknown): string {
  if (v == null) return "";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  if (Array.isArray(v)) return v.map(str).filter(Boolean).join(", ");
  if (typeof v === "object") {
    const o = v as Record<string, unknown>;
    for (const k of ["name", "title", "email", "text", "label", "id"]) if (typeof o[k] === "string") return o[k] as string;
    return JSON.stringify(v);
  }
  return String(v);
}

export function asArray(v: unknown): unknown[] {
  if (Array.isArray(v)) return v;
  if (v == null || v === "") return [];
  return [v];
}

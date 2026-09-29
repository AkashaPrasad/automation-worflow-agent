import { useState, type ReactNode } from "react";
import { ChevronRight, Copy, Check } from "lucide-react";
import { cx } from "./primitives";

const TEMPLATE = /(\{\{[^}]+\}\})/g;
const IS_TEMPLATE = /^\{\{[^}]+\}\}$/;

function StringValue({ value }: { value: string }) {
  const parts = value.split(TEMPLATE);
  return (
    <span className="text-auto-fg">
      &quot;
      {parts.map((p, i) =>
        IS_TEMPLATE.test(p) ? (
          <mark key={i} className="rounded-[3px] bg-accent-soft px-0.5 text-accent" title="Template: resolved from an earlier node's output">
            {p}
          </mark>
        ) : (
          <span key={i}>{p}</span>
        ),
      )}
      &quot;
    </span>
  );
}

function Node({ value, depth, name, highlight }: { value: unknown; depth: number; name?: string; highlight?: Set<string> }) {
  const [open, setOpen] = useState(depth < 2);
  const keyEl = name !== undefined && (
    <span className={cx("text-ink-3", depth === 1 && highlight?.has(name) && "rounded-[3px] bg-ask-bg px-0.5 text-ask-fg")}>
      {name}
      <span className="text-ink-4">: </span>
    </span>
  );

  if (value === null || value === undefined) return <div>{keyEl}<span className="text-ink-4">null</span></div>;
  if (typeof value === "string") return <div className="break-words">{keyEl}<StringValue value={value} /></div>;
  if (typeof value === "number") return <div>{keyEl}<span className="text-run-fg">{value}</span></div>;
  if (typeof value === "boolean") return <div>{keyEl}<span className="text-shadow-fg">{String(value)}</span></div>;

  const isArr = Array.isArray(value);
  const entries: [string, unknown][] = isArr
    ? (value as unknown[]).map((v, i) => [String(i), v])
    : Object.entries(value as Record<string, unknown>);
  const [o, c] = isArr ? ["[", "]"] : ["{", "}"];
  if (!entries.length) return <div>{keyEl}<span className="text-ink-4">{o}{c}</span></div>;

  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((x) => !x)}
        className="-ml-4 inline-flex items-center text-left hover:text-ink"
        aria-expanded={open}
      >
        <ChevronRight className={cx("size-3 text-ink-4 transition-transform duration-150", open && "rotate-90")} aria-hidden />
        <span className="ml-1">
          {keyEl}
          <span className="text-ink-4">{o}</span>
          {!open && <span className="text-ink-4"> {entries.length} {isArr ? "items" : "keys"} {c}</span>}
        </span>
      </button>
      {open && (
        <div className="ml-1.5 border-l border-line pl-4">
          {entries.map(([k, v]) => (
            <Node key={k} value={v} depth={depth + 1} name={isArr ? undefined : k} highlight={highlight} />
          ))}
        </div>
      )}
      {open && <span className="text-ink-4">{c}</span>}
    </div>
  );
}

export function JsonView({
  value,
  className,
  highlight,
  label,
  empty = "Nothing here",
}: {
  value: unknown;
  className?: string;
  highlight?: Set<string>;
  label?: ReactNode;
  empty?: string;
}) {
  const [copied, setCopied] = useState(false);
  const isEmpty = value == null || (typeof value === "object" && Object.keys(value as object).length === 0);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(JSON.stringify(value, null, 2));
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch {
      /* clipboard blocked */
    }
  };
  return (
    <div className={cx("group relative rounded-lg border border-line bg-panel-2", className)}>
      {(label || !isEmpty) && (
        <div className="flex items-center justify-between border-b border-line px-3 py-1.5">
          <span className="text-2xs font-medium text-ink-3">{label}</span>
          {!isEmpty && (
            <button
              type="button"
              onClick={copy}
              className="inline-flex items-center gap-1 rounded px-1 text-2xs text-ink-3 hover:text-ink"
              aria-label="Copy JSON"
            >
              {copied ? <Check className="size-3" aria-hidden /> : <Copy className="size-3" aria-hidden />}
              {copied ? "Copied" : "Copy"}
            </button>
          )}
        </div>
      )}
      <div className="max-h-80 overflow-auto px-3 py-2 pl-7 font-mono text-[11.5px] leading-[1.6] text-ink-2">
        {isEmpty ? <span className="-ml-4 text-ink-4">{empty}</span> : <Node value={value} depth={0} highlight={highlight} />}
      </div>
    </div>
  );
}

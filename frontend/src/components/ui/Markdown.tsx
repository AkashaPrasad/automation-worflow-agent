// A small, safe Markdown renderer: produces React elements (never innerHTML), so content from
// tools and models cannot inject markup. Covers what summaries and sandbox docs use.
import type { ReactNode } from "react";
import { Check } from "lucide-react";
import { cx } from "./primitives";

function safeHref(url: string): string | null {
  const u = url.trim();
  if (/^(https?:|mailto:)/i.test(u)) return u;
  if (u.startsWith("/") && !u.startsWith("//")) return u;
  return null;
}

let keySeq = 0;
const k = () => `md${keySeq++}`;

function inline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  // order matters: code, links, bold, italic
  const re = /(`[^`]+`)|(\[([^\]]+)\]\(([^)\s]+)\))|(\*\*([^*]+)\*\*)|(__([^_]+)__)|(\*([^*\s][^*]*)\*)|(_([^_\s][^_]*)_)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    if (m[1]) out.push(<code key={k()} className="rounded-[4px] border border-line bg-panel-2 px-1 py-px font-mono text-[0.88em]">{m[1].slice(1, -1)}</code>);
    else if (m[2]) {
      const href = safeHref(m[4]);
      out.push(
        href ? (
          <a key={k()} href={href} target={href.startsWith("/") ? undefined : "_blank"} rel="noreferrer noopener" className="text-accent underline">
            {m[3]}
          </a>
        ) : (
          <span key={k()}>{m[3]}</span>
        ),
      );
    } else if (m[5]) out.push(<strong key={k()} className="font-semibold text-ink">{inline(m[6])}</strong>);
    else if (m[7]) out.push(<strong key={k()} className="font-semibold text-ink">{inline(m[8])}</strong>);
    else if (m[9]) out.push(<em key={k()}>{inline(m[10])}</em>);
    else if (m[11]) out.push(<em key={k()}>{inline(m[12])}</em>);
    last = re.lastIndex;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

type ListItem = { text: string; checked: boolean | null; children: ListItem[] };

function parseList(lines: string[], ordered: boolean): ReactNode {
  const root: ListItem[] = [];
  const stack: { indent: number; items: ListItem[] }[] = [{ indent: -1, items: root }];
  let lastItem: ListItem | null = null;
  for (const line of lines) {
    const m = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
    if (!m) {
      if (lastItem) lastItem.text += ` ${line.trim()}`;
      continue;
    }
    const indent = m[1].length;
    let text = m[3];
    let checked: boolean | null = null;
    const task = text.match(/^\[( |x|X)\]\s+(.*)$/);
    if (task) {
      checked = task[1].toLowerCase() === "x";
      text = task[2];
    }
    const item: ListItem = { text, checked, children: [] };
    while (stack.length > 1 && indent <= stack[stack.length - 1].indent) stack.pop();
    stack[stack.length - 1].items.push(item);
    lastItem = item;
    stack.push({ indent, items: item.children });
  }
  const render = (items: ListItem[], ord: boolean, depth: number): ReactNode => {
    const Tag = ord ? "ol" : "ul";
    return (
      <Tag key={k()} className={cx(ord ? "list-decimal" : "list-disc", "space-y-1 pl-5 marker:text-ink-4", depth > 0 && "mt-1")}>
        {items.map((it) => (
          <li key={k()} className={cx(it.checked !== null && "-ml-5 list-none")}>
            {it.checked !== null ? (
              <span className="inline-flex items-start gap-2">
                <span
                  aria-hidden
                  className={cx(
                    "mt-[3px] grid size-3.5 shrink-0 place-items-center rounded-[4px] border",
                    it.checked ? "border-auto-line bg-auto-bg text-auto-fg" : "border-line-strong",
                  )}
                >
                  {it.checked && <Check className="size-2.5" strokeWidth={3} />}
                </span>
                <span className={cx(it.checked && "text-ink-3 line-through decoration-ink-4")}>{inline(it.text)}</span>
              </span>
            ) : (
              inline(it.text)
            )}
            {it.children.length > 0 && render(it.children, false, depth + 1)}
          </li>
        ))}
      </Tag>
    );
  };
  return render(root, ordered, 0);
}

function table(lines: string[]): ReactNode {
  const rows = lines
    .filter((l) => !/^\s*\|?\s*:?-{2,}/.test(l))
    .map((l) =>
      l
        .trim()
        .replace(/^\||\|$/g, "")
        .split("|")
        .map((c) => c.trim()),
    );
  const [head, ...body] = rows;
  return (
    <div key={k()} className="overflow-x-auto rounded-lg border border-line">
      <table className="w-full text-left text-sm">
        <thead className="bg-panel-2 text-xs text-ink-3">
          <tr>{head.map((h) => <th key={k()} className="px-3 py-2 font-medium">{inline(h)}</th>)}</tr>
        </thead>
        <tbody className="divide-y divide-line">
          {body.map((r) => (
            <tr key={k()}>{r.map((c) => <td key={k()} className="px-3 py-2 align-top">{inline(c)}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Markdown({ source, className }: { source: string; className?: string }) {
  const lines = (source || "").replace(/\r\n/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i++;
      continue;
    }
    if (/^```/.test(line)) {
      const buf: string[] = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i++;
      blocks.push(
        <pre key={k()} className="overflow-x-auto rounded-lg border border-line bg-panel-2 p-3 font-mono text-xs leading-relaxed text-ink-2">
          {buf.join("\n")}
        </pre>,
      );
      continue;
    }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      const level = h[1].length;
      const cls =
        level === 1
          ? "text-lg font-semibold text-ink"
          : level === 2
            ? "text-base font-semibold text-ink"
            : "text-sm font-semibold text-ink";
      const Tag = (level <= 2 ? `h${level + 1}` : "h4") as "h2" | "h3" | "h4";
      blocks.push(<Tag key={k()} className={cx(cls, "mt-2 first:mt-0")}>{inline(h[2])}</Tag>);
      i++;
      continue;
    }
    if (/^\s*(---|\*\*\*|___)\s*$/.test(line)) {
      blocks.push(<hr key={k()} className="border-line" />);
      i++;
      continue;
    }
    if (/^\s*\|.*\|\s*$/.test(line)) {
      const buf: string[] = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) buf.push(lines[i++]);
      blocks.push(table(buf));
      continue;
    }
    if (/^\s*>/.test(line)) {
      const buf: string[] = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ""));
      blocks.push(
        <blockquote key={k()} className="rounded-md border border-line bg-panel-2 px-3 py-2 text-ink-2">
          {inline(buf.join(" "))}
        </blockquote>,
      );
      continue;
    }
    const listStart = line.match(/^\s*([-*+]|\d+[.)])\s+/);
    if (listStart) {
      const ordered = /\d/.test(listStart[1]);
      const buf: string[] = [];
      while (i < lines.length && lines[i].trim() && (/^\s*([-*+]|\d+[.)])\s+/.test(lines[i]) || /^\s{2,}\S/.test(lines[i]))) buf.push(lines[i++]);
      blocks.push(parseList(buf, ordered));
      continue;
    }
    const buf: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^(#{1,6}\s|```|\s*>|\s*([-*+]|\d+[.)])\s+|\s*\|.*\|\s*$)/.test(lines[i])
    )
      buf.push(lines[i++]);
    blocks.push(<p key={k()}>{inline(buf.join(" "))}</p>);
  }
  return <div className={cx("space-y-3 text-sm leading-relaxed text-ink-2", className)}>{blocks}</div>;
}

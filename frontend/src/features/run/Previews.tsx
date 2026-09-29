// Rich previews of what a write will deliver: the email as an email, the event as an event.
import { CalendarDays, FileText, Hash, MapPin, NotebookPen, Paperclip, Table2 } from "lucide-react";
import type { ReactNode } from "react";
import { asArray, str, toDate } from "../../lib/format";
import { Markdown } from "../../components/ui/Markdown";
import { JsonView } from "../../components/ui/JsonView";
import { cx } from "../../components/ui/primitives";

export const INTERNAL_DOMAIN = "acme.dev";

export type PreviewKind = "email" | "event" | "doc" | "slack" | "sheet" | "generic";

export function previewKind(tool: string, pv: Record<string, unknown>): PreviewKind {
  if (tool.startsWith("gmail.") || (pv.subject !== undefined && pv.body !== undefined)) return "email";
  if (tool.startsWith("calendar.") || (pv.start !== undefined && (pv.attendees !== undefined || pv.title !== undefined))) return "event";
  if (tool.startsWith("slack.") || (pv.channel !== undefined && pv.text !== undefined)) return "slack";
  if (tool.startsWith("sheets.") || pv.rows !== undefined || pv.changes !== undefined) return "sheet";
  if (tool.startsWith("docs.") || tool.startsWith("notion.") || pv.content_md !== undefined || pv.appended_md !== undefined) return "doc";
  return "generic";
}

/** Merge the preview with args and any pending edits, so the preview shows exactly what will run. */
export function effectivePreview(preview: Record<string, unknown>, args: Record<string, unknown>, edits?: Record<string, unknown>) {
  return { ...args, ...preview, ...(edits ?? {}) };
}

function Recipient({ email }: { email: string }) {
  const domain = email.split("@")[1]?.toLowerCase() ?? "";
  const external = !!domain && domain !== INTERNAL_DOMAIN && !email.startsWith("#");
  return (
    <span
      className={cx(
        "inline-flex h-5 items-center gap-1 rounded-[5px] border px-1.5 font-mono text-[11px]",
        external ? "border-ask-line bg-ask-bg text-ask-fg" : "border-line bg-panel-2 text-ink-2",
      )}
      title={external ? `External: outside ${INTERNAL_DOMAIN}` : "Internal"}
    >
      {email}
      {external && <span className="font-sans text-[9.5px] font-semibold uppercase tracking-[0.04em]">ext</span>}
    </span>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[56px_1fr] items-start gap-2 py-1">
      <span className="pt-0.5 text-2xs font-medium text-ink-3">{label}</span>
      <div className="flex min-w-0 flex-wrap gap-1">{children}</div>
    </div>
  );
}

export function EmailPreview({ pv, draft }: { pv: Record<string, unknown>; draft?: boolean }) {
  const to = asArray(pv.to).map(str);
  const cc = asArray(pv.cc).map(str);
  const attachments = asArray(pv.attachments).map(str);
  return (
    <div className="overflow-hidden rounded-lg border border-line bg-panel">
      <div className="border-b border-line bg-panel-2 px-3 py-2">
        <Row label="From">
          <span className="text-xs text-ink-2">Priya Shah &lt;priya@acme.dev&gt;{draft && <span className="ml-2 text-ink-3">saved as draft, not sent</span>}</span>
        </Row>
        <Row label="To">{to.length ? to.map((e) => <Recipient key={e} email={e} />) : <span className="text-xs text-ink-3">nobody</span>}</Row>
        {cc.length > 0 && <Row label="Cc">{cc.map((e) => <Recipient key={e} email={e} />)}</Row>}
        <Row label="Subject">
          <span className="text-sm font-semibold text-ink">{str(pv.subject) || "(no subject)"}</span>
        </Row>
      </div>
      <div className="max-h-60 overflow-auto whitespace-pre-wrap px-4 py-3 text-sm leading-relaxed text-ink-2">{str(pv.body) || <span className="text-ink-3">(empty body)</span>}</div>
      {attachments.length > 0 && (
        <div className="flex flex-wrap gap-1.5 border-t border-line px-3 py-2">
          {attachments.map((a) => (
            <span key={a} className="inline-flex items-center gap-1 rounded-md border border-line bg-panel-2 px-2 py-0.5 text-xs text-ink-2">
              <Paperclip className="size-3 text-ink-3" aria-hidden />
              {a}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

export function EventPreview({ pv }: { pv: Record<string, unknown> }) {
  const start = toDate(pv.start);
  const end = toDate(pv.end);
  const attendees = asArray(pv.attendees).map(str);
  const timeLabel = start
    ? `${start.toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" })} · ${start.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}${end ? ` to ${end.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}` : ""}`
    : [str(pv.start), str(pv.end)].filter(Boolean).join(" to ");
  const mins = start && end ? Math.round((end.getTime() - start.getTime()) / 60000) : null;
  return (
    <div className="flex overflow-hidden rounded-lg border border-line bg-panel">
      <div className="flex w-16 shrink-0 flex-col items-center justify-center border-r border-line bg-panel-2 py-3">
        {start ? (
          <>
            <span className="text-2xs font-semibold uppercase tracking-[0.06em] text-block-fg">{start.toLocaleDateString(undefined, { month: "short" })}</span>
            <span className="text-2xl font-semibold leading-none text-ink">{start.getDate()}</span>
            <span className="mt-0.5 text-2xs text-ink-3">{start.toLocaleDateString(undefined, { weekday: "short" })}</span>
          </>
        ) : (
          <CalendarDays className="size-5 text-ink-3" aria-hidden />
        )}
      </div>
      <div className="min-w-0 flex-1 px-3 py-2.5">
        <p className="text-sm font-semibold text-ink">{str(pv.title) || "(untitled event)"}</p>
        <p className="mt-0.5 text-xs text-ink-2">
          {timeLabel}
          {mins ? <span className="text-ink-3"> · {mins} min</span> : null}
        </p>
        {str(pv.location) && (
          <p className="mt-0.5 flex items-center gap-1 text-xs text-ink-3">
            <MapPin className="size-3" aria-hidden />
            {str(pv.location)}
          </p>
        )}
        {attendees.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1">
            {attendees.map((a) => (
              <Recipient key={a} email={a} />
            ))}
          </div>
        )}
        {str(pv.description) && <p className="mt-2 text-xs leading-relaxed text-ink-3">{str(pv.description)}</p>}
      </div>
    </div>
  );
}

export function DocPreview({ pv, tool }: { pv: Record<string, unknown>; tool: string }) {
  const notion = tool.startsWith("notion.");
  const Icon = notion ? NotebookPen : FileText;
  const md = str(pv.content_md ?? pv.appended_md ?? pv.text ?? "");
  return (
    <div className="overflow-hidden rounded-lg border border-line bg-panel">
      <div className="flex items-center gap-2 border-b border-line bg-panel-2 px-3 py-2">
        <Icon className="size-3.5 text-ink-3" aria-hidden />
        <span className="min-w-0 truncate text-sm font-semibold text-ink">{str(pv.title) || "(untitled)"}</span>
        {pv.parent != null && <span className="ml-auto shrink-0 text-2xs text-ink-3">in {str(pv.parent)}</span>}
        {pv.appended_md != null && <span className="ml-auto shrink-0 text-2xs text-ink-3">appended section</span>}
      </div>
      <div className="relative max-h-64 overflow-hidden px-4 py-3">
        {md ? <Markdown source={md} /> : <p className="text-sm text-ink-3">(empty)</p>}
        <span className="pointer-events-none absolute inset-x-0 bottom-0 h-8 bg-linear-to-t from-panel to-transparent" aria-hidden />
      </div>
    </div>
  );
}

export function SlackPreview({ pv }: { pv: Record<string, unknown> }) {
  const channel = str(pv.channel).replace(/^#?/, "#");
  return (
    <div className="overflow-hidden rounded-lg border border-line bg-panel">
      <div className="flex items-center gap-1.5 border-b border-line bg-panel-2 px-3 py-2 text-sm font-semibold text-ink">
        <Hash className="size-3.5 text-ink-3" aria-hidden />
        {channel.slice(1)}
      </div>
      <div className="flex gap-2.5 px-3 py-3">
        <span className="grid size-8 shrink-0 place-items-center rounded-md bg-accent-soft text-xs font-semibold text-accent" aria-hidden>
          PS
        </span>
        <div className="min-w-0">
          <p className="text-sm">
            <span className="font-semibold text-ink">Priya Shah</span>
            <span className="ml-1.5 text-2xs text-ink-3">via Adjutant</span>
          </p>
          <p className="mt-0.5 whitespace-pre-wrap text-sm leading-relaxed text-ink-2">{str(pv.text)}</p>
        </div>
      </div>
    </div>
  );
}

export function SheetPreview({ pv }: { pv: Record<string, unknown> }) {
  const rows = (Array.isArray(pv.rows) ? pv.rows : []) as unknown[][];
  const columns = (Array.isArray(pv.columns) ? pv.columns : []) as unknown[];
  const changes = Array.isArray(pv.changes) ? (pv.changes as Record<string, unknown>[]) : [];
  return (
    <div className="overflow-hidden rounded-lg border border-line bg-panel">
      <div className="flex items-center gap-2 border-b border-line bg-panel-2 px-3 py-2">
        <Table2 className="size-3.5 text-ink-3" aria-hidden />
        <span className="text-sm font-semibold text-ink">{str(pv.sheet ?? pv.sheet_id) || "Sheet"}</span>
        <span className="ml-auto text-2xs text-ink-3">
          {rows.length ? `+${rows.length} row${rows.length > 1 ? "s" : ""}` : changes.length ? `${changes.length} cell change${changes.length > 1 ? "s" : ""}` : ""}
        </span>
      </div>
      <div className="overflow-x-auto">
        {rows.length > 0 ? (
          <table className="w-full text-left text-xs">
            {columns.length > 0 && (
              <thead className="text-2xs text-ink-3">
                <tr className="border-b border-line">
                  {columns.map((c, i) => (
                    <th key={i} className="whitespace-nowrap px-3 py-1.5 font-medium">
                      {str(c)}
                    </th>
                  ))}
                </tr>
              </thead>
            )}
            <tbody>
              {rows.map((r, i) => (
                <tr key={i} className="bg-auto-bg/60">
                  {(Array.isArray(r) ? r : [r]).map((c, j) => (
                    <td key={j} className="whitespace-nowrap px-3 py-1.5 font-mono text-[11px] text-ink">
                      <span className="mr-1 text-auto-fg">{j === 0 ? "+" : ""}</span>
                      {str(c)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="p-3">
            <JsonView value={changes.length ? changes : pv} />
          </div>
        )}
      </div>
    </div>
  );
}

export function Preview({ tool, pv }: { tool: string; pv: Record<string, unknown> }) {
  const kind = previewKind(tool, pv);
  if (kind === "email") return <EmailPreview pv={pv} draft={tool === "gmail.draft" || pv.kind === "email_draft"} />;
  if (kind === "event") return <EventPreview pv={pv} />;
  if (kind === "slack") return <SlackPreview pv={pv} />;
  if (kind === "sheet") return <SheetPreview pv={pv} />;
  if (kind === "doc") return <DocPreview pv={pv} tool={tool} />;
  return <JsonView value={pv} label="preview" />;
}

/** Which argument keys a person can reasonably edit before approving. */
export const EDITABLE_KEYS = ["subject", "body", "text", "title", "content_md", "description"] as const;

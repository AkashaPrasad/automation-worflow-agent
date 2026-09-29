"""Sandbox implementations of every Adjutant tool, operating on the per-workspace world."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from app.core.models import EffectClass, EffectRecord, ErrorKind, ToolContext, ToolResult, ToolSpec, Trust, new_id, now_ms

from ..base import BaseTool, ToolFail, fail, ok
from .timeutil import GRID_MIN, TIMEZONE, WORK_END, WORK_START, free_slots, iso, merge, overlaps, parse_dt, workspace_tz
from .world import SandboxWorkspaceStore

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-']+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
R, WR, WI, CM = EffectClass.READ, EffectClass.WRITE_REVERSIBLE, EffectClass.WRITE_IRREVERSIBLE, EffectClass.COMMUNICATE
TRUSTED, UNTRUSTED = Trust.TRUSTED, Trust.UNTRUSTED

# Tools that exist so compensations can be dispatched by name; hidden from the planner.
INTERNAL_TOOLS = {"gmail.delete_draft", "docs.trash", "docs.revert", "sheets.delete_rows", "sheets.restore_cells", "notion.archive_page"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _trim(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _snip(s: str, n: int = 160) -> str:
    return _trim(re.sub(r"\s+", " ", s or "").strip(), n)


def _obj(t: str, **props: Any) -> dict[str, Any]:
    required = props.pop("_required", [])
    return {"type": "object", "properties": props, "required": required}


def _str(desc: str, **kw: Any) -> dict[str, Any]:
    return {"type": "string", "description": desc, **kw}


def _strs(desc: str, **kw: Any) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "description": desc, **kw}


def _int(desc: str, **kw: Any) -> dict[str, Any]:
    return {"type": "integer", "description": desc, **kw}


def _spec(name: str, title: str, desc: str, effect: EffectClass, schema: dict[str, Any], trust: Trust = TRUSTED,
          compensable: bool = False) -> ToolSpec:
    return ToolSpec(name=name, app=name.split(".")[0], title=title, description=desc, effect=effect, input_schema=schema,
                    output_trust=trust, compensable=compensable, idempotent=(effect == R))


def _emails(values: list[str], field: str) -> list[str]:
    out = []
    for v in values:
        v = v.strip()
        if not EMAIL_RE.match(v):
            raise ToolFail(ErrorKind.INVALID_ARGS, f"'{field}': '{v}' is not a valid email address")
        out.append(v.lower())
    return list(dict.fromkeys(out))


def _resolve_person(world: dict[str, Any], ref: str) -> str:
    ref = ref.strip()
    if "@" in ref:
        if not EMAIL_RE.match(ref):
            raise ToolFail(ErrorKind.INVALID_ARGS, f"'{ref}' is not a valid email address")
        return ref.lower()
    q = ref.lower()
    people = world["people"]
    hits = [p for p in people if p["name"].lower() == q] or [p for p in people if q in p["name"].lower() or q == p["name"].split()[0].lower()]
    if len(hits) == 1:
        return hits[0]["email"]
    if not hits:
        raise ToolFail(ErrorKind.NOT_FOUND, f"No person named '{ref}' in the workspace; pass an email address instead")
    raise ToolFail(ErrorKind.INVALID_ARGS, f"'{ref}' is ambiguous ({', '.join(p['email'] for p in hits)}); pass an email address")


def _dt(value: str, tz, field: str) -> datetime:
    try:
        return parse_dt(value, tz)
    except Exception:
        raise ToolFail(ErrorKind.INVALID_ARGS, f"'{field}': '{value}' is not an ISO 8601 datetime (example: 2026-10-06T14:00:00+05:30)")


def _find(items: list[dict[str, Any]], ref: str, kind: str, skip_flag: str | None = None) -> dict[str, Any]:
    pool = [i for i in items if not (skip_flag and i.get(skip_flag))]
    for i in pool:
        if i["id"] == ref:
            return i
    q = ref.strip().lower()
    hits = [i for i in pool if i.get("title", "").lower() == q] or [i for i in pool if q and q in i.get("title", "").lower()]
    if len(hits) >= 1 and (len(hits) == 1 or q):
        return hits[0]
    raise ToolFail(ErrorKind.NOT_FOUND, f"{kind} '{ref}' not found")


def _terms(q: str) -> list[str]:
    stop = {"a", "an", "the", "of", "and", "for", "to", "in"}
    return [t for t in re.findall(r"[\w\-']+", q.lower()) if t not in stop]


def _score(text_title: str, text_body: str, terms: list[str]) -> int:
    t, b = text_title.lower(), text_body.lower()
    return sum((2 if x in t else 0) + (1 if x in b else 0) for x in terms)


def _busy(world: dict[str, Any], email: str, tz) -> list[tuple[datetime, datetime, str]]:
    email = email.lower()
    out = []
    for e in world["calendar"]:
        if email in [a.lower() for a in e["attendees"]]:
            out.append((parse_dt(e["start"], tz), parse_dt(e["end"], tz), e["title"]))
    for b in world["busy"].get(email, []):
        out.append((parse_dt(b["start"], tz), parse_dt(b["end"], tz), b.get("label", "Busy")))
    return out


def _outbox(world: dict[str, Any], kind: str, to: list[str], subject: str, body: str, key: str, now: datetime, **extra: Any) -> dict[str, Any]:
    item = {"id": new_id("out"), "kind": kind, "to": to, "subject": subject, "body": body, "at": iso(now),
            "adjutant_key": key, **extra}
    world["outbox"].append(item)
    return item


def _audit(world: dict[str, Any], ctx: ToolContext, tool: str, target: dict[str, Any], now: datetime) -> None:
    world["audit"].append({"key": ctx.idempotency_key, "tool": tool, "target": target, "at": iso(now)})


def _col_index(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _cell(ref: str) -> tuple[int, int]:
    m = re.fullmatch(r"([A-Za-z]{1,3})(\d{1,5})", ref.strip())
    if not m:
        raise ToolFail(ErrorKind.INVALID_ARGS, f"'{ref}' is not an A1 cell reference (example: D2)")
    return int(m.group(2)) - 1, _col_index(m.group(1))


def _msg_summary(m: dict[str, Any]) -> dict[str, Any]:
    return {"id": m["id"], "thread_id": m.get("thread_id"), "from": f"{m['from']['name']} <{m['from']['email']}>",
            "to": m.get("to", []), "subject": m["subject"], "date": m["date"], "snippet": _snip(m["body"]),
            "unread": "UNREAD" in m.get("labels", []), "labels": m.get("labels", [])}


# ---------------------------------------------------------------------------
# base classes
# ---------------------------------------------------------------------------


class SandboxTool(BaseTool):
    def __init__(self, store: SandboxWorkspaceStore):
        super().__init__()
        self.store = store

    # persisted idempotency map + deterministic fault injection live in the world document
    async def idem_get(self, ctx: ToolContext) -> ToolResult | None:
        raw = self.store.read(ctx.workspace_id)["idem"].get(ctx.idempotency_key)
        return ToolResult.model_validate(raw) if raw else None

    async def idem_put(self, ctx: ToolContext, result: ToolResult) -> None:
        with self.store.transaction(ctx.workspace_id) as w:
            w["idem"][ctx.idempotency_key] = result.model_dump(mode="json")

    async def before(self, args: dict[str, Any], ctx: ToolContext, phase: str) -> None:
        name = self.spec.name
        pending = self.store.read(ctx.workspace_id).get("faults", {}).get(name)
        if not pending:
            return
        fired = None
        with self.store.transaction(ctx.workspace_id) as w:
            faults = w["faults"].get(name) or []
            for f in faults:
                if f.get("times", 0) > 0 and f.get("phase", "any") in ("any", phase):
                    f["times"] -= 1
                    fired = dict(f)
                    break
            w["faults"][name] = [f for f in faults if f.get("times", 0) > 0]
        if fired:
            raise ToolFail(ErrorKind(fired["kind"]), fired["message"])

    def now(self) -> datetime:
        return self.store.now()

    @property
    def tz(self):
        return workspace_tz()


class ReadTool(SandboxTool):
    def read(self, world: dict[str, Any], args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ok(self.read(self.store.read(ctx.workspace_id), args, ctx))


class WriteTool(SandboxTool):
    def plan(self, world: dict[str, Any], args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:  # pragma: no cover
        """Validate + check preconditions; return {summary, target, preview, compensation, output}."""
        raise NotImplementedError

    def apply(self, world: dict[str, Any], args: dict[str, Any], ctx: ToolContext, plan: dict[str, Any]) -> None:  # pragma: no cover
        raise NotImplementedError

    def _result(self, args: dict[str, Any], ctx: ToolContext, plan: dict[str, Any], simulated: bool) -> ToolResult:
        eff = self.make_effect(args, ctx, summary=plan["summary"], target=plan["target"], preview=plan["preview"],
                               compensation=plan.get("compensation"), simulated=simulated)
        return ToolResult(ok=True, output=plan["output"], simulated=simulated, effect=eff)

    async def dry_run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        p = self.plan(self.store.read(ctx.workspace_id), args, ctx)
        return self._result(args, ctx, p, True)

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        with self.store.transaction(ctx.workspace_id) as w:
            p = self.plan(w, args, ctx)
            self.apply(w, args, ctx, p)
            _audit(w, ctx, self.spec.name, p["target"], self.now())
        return self._result(args, ctx, p, False)

    async def compensate(self, effect: EffectRecord, ctx: ToolContext) -> ToolResult:
        comp = effect.compensation
        if not self.spec.compensable or not comp:
            return fail(ErrorKind.PRECONDITION, f"{self.spec.name} cannot be undone: {self._why_not()}")
        fn = UNDO.get(comp["tool"])
        if fn is None:
            return fail(ErrorKind.PRECONDITION, f"no compensation handler for {comp['tool']}")
        with self.store.transaction(ctx.workspace_id) as w:
            note = fn(w, comp["args"], ctx, self.now())
        done = effect.model_copy(update={"status": "compensated", "compensated_at": now_ms()})
        return ToolResult(ok=True, output={"undone": comp["tool"], "note": note}, effect=done)

    def _why_not(self) -> str:
        return "the effect is not reversible"

    async def reconcile(self, effect: EffectRecord, ctx: ToolContext) -> bool | None:
        key = effect.idempotency_key
        if not key:
            return None
        return any(a["key"] == key for a in self.store.read(ctx.workspace_id)["audit"])


# ---------------------------------------------------------------------------
# undo handlers (used by compensate() and by the internal compensation tools)
# ---------------------------------------------------------------------------


def _undo_delete_draft(w, a, ctx, now):
    n = len(w["drafts"])
    w["drafts"] = [d for d in w["drafts"] if d["id"] != a["draft_id"]]
    return "draft deleted" if len(w["drafts"]) < n else "draft already gone"


def _undo_docs_trash(w, a, ctx, now):
    for d in w["docs"]:
        if d["id"] == a["doc_id"]:
            d["trashed"] = True
            return "doc moved to trash"
    return "doc already gone"


def _undo_docs_revert(w, a, ctx, now):
    hist = w["doc_history"].get(a["doc_id"], [])
    v = int(a["to_version"])
    for d in w["docs"]:
        if d["id"] == a["doc_id"] and v < len(hist):
            d["content_md"] = hist[v]
            d["updated_at"] = iso(now)
            w["doc_history"][a["doc_id"]] = hist[:v]
            return f"doc reverted to version {v}"
    return "nothing to revert"


def _undo_sheet_rows(w, a, ctx, now):
    sheet = next((s for s in w["sheets"] if s["id"] == a["sheet_id"]), None)
    if not sheet:
        return "sheet already gone"
    rows = a.get("rows") or []
    n = len(rows) or int(a.get("count", 0))
    vals = sheet["values"]
    start = int(a["start_row"]) - 1
    if rows and vals[start:start + n] == rows:
        del vals[start:start + n]
        return f"deleted {n} row(s)"
    if rows:  # rows moved (someone else appended); find the last exact match
        for i in range(len(vals) - n, 0, -1):
            if vals[i:i + n] == rows:
                del vals[i:i + n]
                return f"deleted {n} row(s)"
    return "rows already gone"


def _undo_restore_cells(w, a, ctx, now):
    sheet = next((s for s in w["sheets"] if s["id"] == a["sheet_id"]), None)
    if not sheet:
        return "sheet already gone"
    for u in a["updates"]:
        r, c = _cell(u["cell"])
        _set_cell(sheet["values"], r, c, u.get("value", ""))
    return f"restored {len(a['updates'])} cell(s)"


def _undo_notion_archive(w, a, ctx, now):
    for p in w["notion"]:
        if p["id"] == a["page_id"]:
            p["archived"] = True
            return "page archived"
    return "page already gone"


def _delete_event(w, a, ctx, now):
    ev = next((e for e in w["calendar"] if e["id"] == a["event_id"]), None)
    if not ev:
        return "event already gone"
    w["calendar"] = [e for e in w["calendar"] if e["id"] != ev["id"]]
    others = [x for x in ev["attendees"] if x.lower() != "priya@acme.dev"]
    if others:
        _outbox(w, "cancellation", others, f"Cancelled: {ev['title']}",
                f"The event '{ev['title']}' scheduled {ev['start']} - {ev['end']} has been cancelled by Priya Shah.",
                ctx.idempotency_key, now, ref=ev["id"])
    return "event deleted" + (f"; cancellation sent to {', '.join(others)}" if others else "")


UNDO = {
    "gmail.delete_draft": _undo_delete_draft, "docs.trash": _undo_docs_trash, "docs.revert": _undo_docs_revert,
    "sheets.delete_rows": _undo_sheet_rows, "sheets.restore_cells": _undo_restore_cells,
    "notion.archive_page": _undo_notion_archive, "calendar.delete_event": _delete_event,
}


def _set_cell(vals: list[list[Any]], r: int, c: int, value: Any) -> Any:
    while len(vals) <= r:
        vals.append([])
    row = vals[r]
    while len(row) <= c:
        row.append("")
    old = row[c]
    row[c] = value
    return old


class InternalUndoTool(WriteTool):
    """Compensation targets (docs.trash, ...) so they can be looked up by name. Not shown to the planner."""

    def __init__(self, store: SandboxWorkspaceStore, spec: ToolSpec):
        super().__init__(store)
        self.spec = spec

    def plan(self, world, args, ctx):
        return {"summary": f"{self.spec.title}: {args}", "target": {"kind": "undo", "id": self.spec.name}, "preview": dict(args),
                "compensation": None, "output": {}}

    def apply(self, world, args, ctx, plan):
        plan["output"] = {"note": UNDO[self.spec.name](world, args, ctx, self.now())}


# ---------------------------------------------------------------------------
# gmail
# ---------------------------------------------------------------------------

_TOKEN = re.compile(r'(\w+:"[^"]+"|\w+:\S+|"[^"]+"|\S+)')


class GmailSearch(ReadTool):
    spec = _spec("gmail.search", "Search email",
                 "Search the user's mailbox. Returns matching messages (id, from, subject, date, snippet, unread) newest first. "
                 "Query supports free text plus operators: from:, to:, subject:, is:unread, label:, in:sent|inbox|anywhere. "
                 "An empty query lists the latest inbox messages. Use gmail.read for the full body.", R,
                 _obj("", query=_str("Search query, e.g. 'is:unread' or 'from:dana@northwind.com invoice'", default=""),
                      limit=_int("Max results (1-50)", default=10, minimum=1, maximum=50)), UNTRUSTED)

    def read(self, w, a, ctx):
        q = a.get("query", "")
        scope = "inbox"
        preds = []
        free: list[str] = []
        for tok in _TOKEN.findall(q):
            tl = tok.lower()
            m = re.match(r"^(\w+):(.+)$", tok)
            if m and m.group(1).lower() in ("from", "to", "subject", "is", "label", "in", "newer_than"):
                op, val = m.group(1).lower(), m.group(2).strip('"').lower()
                if op == "in":
                    scope = val
                elif op == "from":
                    preds.append(lambda x, v=val: v in (x["from"]["email"] + " " + x["from"]["name"]).lower())
                elif op == "to":
                    preds.append(lambda x, v=val: any(v in t.lower() for t in x.get("to", []) + x.get("cc", [])))
                elif op == "subject":
                    preds.append(lambda x, v=val: v in x["subject"].lower())
                elif op == "is":
                    if val == "unread":
                        preds.append(lambda x: "UNREAD" in x.get("labels", []))
                    elif val == "read":
                        preds.append(lambda x: "UNREAD" not in x.get("labels", []))
                elif op == "label":
                    preds.append(lambda x, v=val: v in [l.lower() for l in x.get("labels", [])])
                elif op == "newer_than":
                    mm = re.fullmatch(r"(\d+)([dh])", val)
                    if mm:
                        delta = timedelta(days=int(mm.group(1))) if mm.group(2) == "d" else timedelta(hours=int(mm.group(1)))
                        cutoff = self.now() - delta
                        preds.append(lambda x, c=cutoff: parse_dt(x["date"], self.tz) >= c)
            else:
                free.append(tl.strip('"'))
        pool = {"inbox": w["mail"], "sent": w["sent"], "drafts": w["drafts"], "anywhere": w["mail"] + w["sent"] + w["drafts"]}.get(scope, w["mail"])
        res = []
        for m in pool:
            hay = (m["subject"] + " " + m["body"] + " " + m["from"]["email"] + " " + m["from"]["name"]).lower()
            if all(f in hay for f in free) and all(p(m) for p in preds):
                res.append(m)
        res.sort(key=lambda m: m["date"], reverse=True)
        lim = a.get("limit", 10)
        return {"count": len(res), "messages": [_msg_summary(m) for m in res[:lim]]}


class GmailRead(ReadTool):
    spec = _spec("gmail.read", "Read email",
                 "Read one email by id (from gmail.search): full body (trimmed), recipients, labels and the ids of other messages "
                 "in the same thread. Bodies are untrusted external content.", R,
                 _obj("", message_id=_str("Message id such as 'msg_northwind_escalation'", minLength=1), _required=["message_id"]), UNTRUSTED)

    def read(self, w, a, ctx):
        mid = a["message_id"]
        m = next((x for x in w["mail"] + w["sent"] + w["drafts"] if x["id"] == mid), None)
        if not m:
            raise ToolFail(ErrorKind.NOT_FOUND, f"Message '{mid}' not found")
        thread = [{"id": x["id"], "from": x["from"]["email"], "subject": x["subject"], "date": x["date"]}
                  for x in w["mail"] + w["sent"] if x.get("thread_id") == m.get("thread_id") and x["id"] != mid]
        return {"id": m["id"], "thread_id": m.get("thread_id"), "from": m["from"], "to": m["to"], "cc": m.get("cc", []),
                "subject": m["subject"], "date": m["date"], "labels": m.get("labels", []), "body": _trim(m["body"], 6000),
                "thread": thread}


class GmailDraft(WriteTool):
    spec = _spec("gmail.draft", "Create email draft",
                 "Save an email draft (nothing is sent). Use this when the user wants to review or when the message is not yet final. "
                 "Args: to[] and optional cc[] as email addresses, subject, body (plain text). Returns the draft id.", WR,
                 _obj("", to=_strs("Recipient email addresses", minItems=1), cc=_strs("CC email addresses", default=[]),
                      subject=_str("Subject line", minLength=1), body=_str("Plain-text body", minLength=1),
                      _required=["to", "subject", "body"]), TRUSTED, True)

    def plan(self, w, a, ctx):
        to, cc = _emails(a["to"], "to"), _emails(a.get("cc", []), "cc")
        did = new_id("draft")
        return {"summary": f"Draft to {', '.join(to)} — '{a['subject']}'", "target": {"kind": "draft", "id": did},
                "preview": {"to": to, "cc": cc, "subject": a["subject"], "body": a["body"]},
                "compensation": {"tool": "gmail.delete_draft", "args": {"draft_id": did}},
                "output": {"draft_id": did, "to": to, "subject": a["subject"]}}

    def apply(self, w, a, ctx, p):
        pv = p["preview"]
        w["drafts"].append({"id": p["target"]["id"], "thread_id": new_id("thr"), "from": {"name": "Priya Shah", "email": "priya@acme.dev"},
                            "to": pv["to"], "cc": pv["cc"], "subject": pv["subject"], "body": pv["body"], "date": iso(self.now()),
                            "labels": ["DRAFT"], "adjutant_key": ctx.idempotency_key})


class GmailSend(WriteTool):
    spec = _spec("gmail.send", "Send email",
                 "Send an email from the user's account. IRREVERSIBLE: delivered to real recipients and cannot be unsent. "
                 "Args: to[] (email addresses), optional cc[], subject, body (plain text, complete and ready to send), optional reply_to_id "
                 "(message id to thread under). Returns the sent message id.", CM,
                 _obj("", to=_strs("Recipient email addresses", minItems=1), cc=_strs("CC email addresses", default=[]),
                      subject=_str("Subject line", minLength=1), body=_str("Plain-text body", minLength=1),
                      reply_to_id=_str("Optional id of the message being replied to"), _required=["to", "subject", "body"]))

    def _why_not(self):
        return "sent emails cannot be recalled (irreversible; the audience has already received it)"

    def plan(self, w, a, ctx):
        to, cc = _emails(a["to"], "to"), _emails(a.get("cc", []), "cc")
        thread = new_id("thr")
        if a.get("reply_to_id"):
            orig = next((x for x in w["mail"] + w["sent"] if x["id"] == a["reply_to_id"]), None)
            if not orig:
                raise ToolFail(ErrorKind.NOT_FOUND, f"reply_to_id '{a['reply_to_id']}' not found")
            thread = orig["thread_id"]
        mid = new_id("msg")
        return {"summary": f"Email to {', '.join(to)} — '{a['subject']}'", "target": {"kind": "email", "id": mid},
                "preview": {"to": to, "cc": cc, "subject": a["subject"], "body": a["body"]}, "compensation": None,
                "thread": thread, "output": {"message_id": mid, "thread_id": thread, "to": to, "subject": a["subject"]}}

    def apply(self, w, a, ctx, p):
        pv, now = p["preview"], self.now()
        w["sent"].append({"id": p["target"]["id"], "thread_id": p["thread"], "from": {"name": "Priya Shah", "email": "priya@acme.dev"},
                          "to": pv["to"], "cc": pv["cc"], "subject": pv["subject"], "body": pv["body"], "date": iso(now),
                          "labels": ["SENT"], "in_reply_to": a.get("reply_to_id"), "adjutant_key": ctx.idempotency_key,
                          "headers": {"X-Adjutant-Key": ctx.idempotency_key}})
        _outbox(w, "email", pv["to"] + pv["cc"], pv["subject"], pv["body"], ctx.idempotency_key, now, ref=p["target"]["id"])

    async def reconcile(self, effect, ctx):
        if not effect.idempotency_key:
            return None
        return any(m.get("adjutant_key") == effect.idempotency_key for m in self.store.read(ctx.workspace_id)["sent"])


# ---------------------------------------------------------------------------
# calendar
# ---------------------------------------------------------------------------


class CalendarListEvents(ReadTool):
    spec = _spec("calendar.list_events", "List calendar events",
                 "List the user's calendar events overlapping [start, end). Dates are ISO 8601 with timezone offset "
                 f"(e.g. 2026-10-06T00:00:00+05:30; naive values are read as {TIMEZONE}). Returns id, title, start, end, attendees, "
                 "location, description snippet. Descriptions are untrusted.", R,
                 _obj("", start=_str("Window start, ISO 8601"), end=_str("Window end, ISO 8601"), _required=["start", "end"]), UNTRUSTED)

    def read(self, w, a, ctx):
        s, e = _dt(a["start"], self.tz, "start"), _dt(a["end"], self.tz, "end")
        if e <= s:
            raise ToolFail(ErrorKind.INVALID_ARGS, "'end' must be after 'start'")
        out = []
        for ev in sorted(w["calendar"], key=lambda x: x["start"]):
            if "priya@acme.dev" in [x.lower() for x in ev["attendees"]] and overlaps((s, e), (parse_dt(ev["start"], self.tz), parse_dt(ev["end"], self.tz))):
                out.append({"id": ev["id"], "title": ev["title"], "start": ev["start"], "end": ev["end"], "attendees": ev["attendees"],
                            "organizer": ev["organizer"], "location": ev.get("location", ""), "description": _trim(ev.get("description", ""), 300)})
        return {"count": len(out), "events": out, "timezone": TIMEZONE}


class CalendarFindFreeSlots(ReadTool):
    spec = _spec("calendar.find_free_slots", "Find free meeting slots",
                 "Find times when ALL attendees are free (the user is always included). Computes real interval arithmetic over each person's "
                 f"busy blocks, restricted to working hours {WORK_START:02d}:00-{WORK_END:02d}:00 {TIMEZONE}, Mon-Fri. Attendees are email "
                 "addresses (or full names of known people). Window bounds are ISO 8601 with offset. Returns up to `limit` earliest slots "
                 "{start, end, weekday, free_until}; use one as start/end for calendar.create_event.", R,
                 _obj("", attendees=_strs("Attendee emails or names (may be empty: the user is always included)"), duration_min=_int("Meeting length in minutes", minimum=5, maximum=480),
                      window_start=_str("Earliest start, ISO 8601"), window_end=_str("Latest end, ISO 8601"),
                      limit=_int("Max slots to return", default=6, minimum=1, maximum=20),
                      _required=["attendees", "duration_min", "window_start", "window_end"]))

    def read(self, w, a, ctx):
        tz = self.tz
        ws, we = _dt(a["window_start"], tz, "window_start"), _dt(a["window_end"], tz, "window_end")
        if we <= ws:
            raise ToolFail(ErrorKind.INVALID_ARGS, "'window_end' must be after 'window_start'")
        if we - ws > timedelta(days=62):
            raise ToolFail(ErrorKind.INVALID_ARGS, "window too large; use at most 62 days")
        people = list(dict.fromkeys(["priya@acme.dev"] + [_resolve_person(w, x) for x in a["attendees"]]))
        busy = [(s, e) for p in people for s, e, _ in _busy(w, p, tz)]
        slots = free_slots(busy, ws, we, timedelta(minutes=a["duration_min"]), tz, now=self.now(), limit=a.get("limit", 6))
        out = {"attendees": people, "duration_min": a["duration_min"], "timezone": TIMEZONE,
               "working_hours": f"{WORK_START:02d}:00-{WORK_END:02d}:00 Mon-Fri", "slots": slots}
        if not slots:
            out["note"] = "No common free slot in this window; widen the window or shorten the meeting."
        return out


class CalendarCreateEvent(WriteTool):
    spec = _spec("calendar.create_event", "Create calendar event",
                 "Create a calendar event and send invitations to attendees (visible to other people). Fails with a precondition error if any "
                 "attendee is busy: run calendar.find_free_slots first. start/end are ISO 8601 with timezone offset. attendees are emails "
                 "(the user is added automatically). Returns event_id.", CM,
                 _obj("", title=_str("Event title", minLength=1), start=_str("Start, ISO 8601 with offset"), end=_str("End, ISO 8601 with offset"),
                      attendees=_strs("Attendee emails", default=[]), description=_str("Agenda / notes", default=""),
                      _required=["title", "start", "end"]), TRUSTED, True)

    def _why_not(self):
        return "invitations were delivered"

    def plan(self, w, a, ctx):
        tz = self.tz
        s, e = _dt(a["start"], tz, "start"), _dt(a["end"], tz, "end")
        if e <= s:
            raise ToolFail(ErrorKind.INVALID_ARGS, "'end' must be after 'start'")
        if s < self.now() - timedelta(minutes=1):
            raise ToolFail(ErrorKind.PRECONDITION, f"start {a['start']} is in the past (now is {iso(self.now())})")
        atts = list(dict.fromkeys(["priya@acme.dev"] + [_resolve_person(w, x) for x in a.get("attendees", [])]))
        conflicts = []
        for p in atts:
            for bs, be, label in _busy(w, p, tz):
                if overlaps((s, e), (bs, be)):
                    conflicts.append(f"{p} is busy {bs.astimezone(tz).strftime('%a %H:%M')}-{be.astimezone(tz).strftime('%H:%M')} ('{label}')")
        if conflicts:
            raise ToolFail(ErrorKind.PRECONDITION, "Cannot schedule: " + "; ".join(conflicts[:6]) + ". Use calendar.find_free_slots to pick a time everyone is free.")
        eid = new_id("evt")
        pv = {"title": a["title"], "start": iso(s, tz), "end": iso(e, tz), "attendees": atts, "description": a.get("description", "")}
        return {"summary": f"Event '{a['title']}' {s.astimezone(tz).strftime('%a %b %d %H:%M')}-{e.astimezone(tz).strftime('%H:%M')} with {', '.join(x for x in atts if x != 'priya@acme.dev') or 'no one else'}",
                "target": {"kind": "event", "id": eid}, "preview": pv,
                "compensation": {"tool": "calendar.delete_event", "args": {"event_id": eid}},
                "output": {"event_id": eid, "title": a["title"], "start": pv["start"], "end": pv["end"], "attendees": atts}}

    def apply(self, w, a, ctx, p):
        pv = p["preview"]
        w["calendar"].append({"id": p["target"]["id"], "title": pv["title"], "start": pv["start"], "end": pv["end"], "organizer": "priya@acme.dev",
                              "attendees": pv["attendees"], "location": "", "description": pv["description"], "status": "confirmed",
                              "adjutant_key": ctx.idempotency_key})
        others = [x for x in pv["attendees"] if x != "priya@acme.dev"]
        if others:
            _outbox(w, "invite", others, f"Invitation: {pv['title']}", f"{pv['title']}\n{pv['start']} - {pv['end']}\n\n{pv['description']}".strip(),
                    ctx.idempotency_key, self.now(), ref=p["target"]["id"])


class CalendarDeleteEvent(WriteTool):
    spec = _spec("calendar.delete_event", "Delete calendar event",
                 "Permanently delete a calendar event by id and send cancellation notices to its attendees. Irreversible.", WI,
                 _obj("", event_id=_str("Event id, e.g. from calendar.list_events", minLength=1), _required=["event_id"]))

    def plan(self, w, a, ctx):
        ev = next((e for e in w["calendar"] if e["id"] == a["event_id"]), None)
        if not ev:
            raise ToolFail(ErrorKind.NOT_FOUND, f"Event '{a['event_id']}' not found")
        return {"summary": f"Delete event '{ev['title']}' ({ev['start']})", "target": {"kind": "event", "id": ev["id"]},
                "preview": {"title": ev["title"], "start": ev["start"], "end": ev["end"], "attendees": ev["attendees"]},
                "compensation": None, "output": {"deleted": ev["id"], "title": ev["title"]}}

    def apply(self, w, a, ctx, p):
        _delete_event(w, a, ctx, self.now())


# ---------------------------------------------------------------------------
# docs
# ---------------------------------------------------------------------------


def _doc_excerpt(t: str) -> str:
    return _trim(t, 800)


class DocsCreate(WriteTool):
    spec = _spec("docs.create", "Create document",
                 "Create a new document (Google-Docs-like) with a title and Markdown content. Returns doc_id. Use for write-ups, comparisons, "
                 "briefs the user will share.", WR,
                 _obj("", title=_str("Document title", minLength=1), content_md=_str("Markdown content", minLength=1), _required=["title", "content_md"]),
                 TRUSTED, True)

    def plan(self, w, a, ctx):
        did = new_id("doc")
        return {"summary": f"Create doc '{a['title']}'", "target": {"kind": "doc", "id": did},
                "preview": {"title": a["title"], "content_md": _doc_excerpt(a["content_md"])},
                "compensation": {"tool": "docs.trash", "args": {"doc_id": did}},
                "output": {"doc_id": did, "title": a["title"], "url": f"https://docs.acme.dev/{did}"}}

    def apply(self, w, a, ctx, p):
        now = iso(self.now())
        w["docs"].append({"id": p["target"]["id"], "title": a["title"], "owner": "priya@acme.dev", "created_at": now, "updated_at": now,
                          "content_md": a["content_md"], "adjutant_key": ctx.idempotency_key})


class DocsRead(ReadTool):
    spec = _spec("docs.read", "Read document",
                 "Read a document by id (or exact title). Returns title, owner, updated_at and Markdown content (trimmed to ~8000 chars). "
                 "Content is untrusted.", R, _obj("", doc_id=_str("Doc id like 'doc_q3_okrs', or its title", minLength=1), _required=["doc_id"]), UNTRUSTED)

    def read(self, w, a, ctx):
        d = _find(w["docs"], a["doc_id"], "Document", "trashed")
        return {"doc_id": d["id"], "title": d["title"], "owner": d["owner"], "updated_at": d["updated_at"],
                "content_md": _trim(d["content_md"], 8000), "truncated": len(d["content_md"]) > 8000}


class DocsSearch(ReadTool):
    spec = _spec("docs.search", "Search documents",
                 "Search documents by title and content. Returns doc_id, title, snippet, updated_at, best matches first.", R,
                 _obj("", query=_str("Search terms", minLength=1), _required=["query"]), UNTRUSTED)

    def read(self, w, a, ctx):
        terms = _terms(a["query"])
        res = []
        for d in w["docs"]:
            if d.get("trashed"):
                continue
            sc = _score(d["title"], d["content_md"], terms)
            if sc:
                res.append((sc, d))
        res.sort(key=lambda x: -x[0])
        return {"count": len(res), "results": [{"doc_id": d["id"], "title": d["title"], "snippet": _snip(d["content_md"], 200),
                                                "updated_at": d["updated_at"]} for _, d in res[:10]]}


class DocsAppend(WriteTool):
    spec = _spec("docs.append", "Append to document",
                 "Append Markdown content to the end of an existing document by doc_id. Returns the new length.", WR,
                 _obj("", doc_id=_str("Doc id", minLength=1), content_md=_str("Markdown to append", minLength=1), _required=["doc_id", "content_md"]),
                 TRUSTED, True)

    def plan(self, w, a, ctx):
        d = _find(w["docs"], a["doc_id"], "Document", "trashed")
        ver = len(w["doc_history"].get(d["id"], []))
        return {"summary": f"Append to doc '{d['title']}'", "target": {"kind": "doc", "id": d["id"]},
                "preview": {"doc_id": d["id"], "title": d["title"], "appended_md": _doc_excerpt(a["content_md"])},
                "compensation": {"tool": "docs.revert", "args": {"doc_id": d["id"], "to_version": ver}},
                "output": {"doc_id": d["id"], "length": len(d["content_md"]) + len(a["content_md"]) + 2}}

    def apply(self, w, a, ctx, p):
        d = _find(w["docs"], a["doc_id"], "Document", "trashed")
        w["doc_history"].setdefault(d["id"], []).append(d["content_md"])
        d["content_md"] = d["content_md"].rstrip() + "\n\n" + a["content_md"]
        d["updated_at"] = iso(self.now())


# ---------------------------------------------------------------------------
# sheets
# ---------------------------------------------------------------------------


class SheetsRead(ReadTool):
    spec = _spec("sheets.read", "Read spreadsheet",
                 "Read a spreadsheet by id or title (e.g. 'Hiring Pipeline', 'Vendor Payments'). Optional A1 range like 'A1:F10' "
                 "(default: all rows; first row is the header). Returns values as rows of strings. Sheets may contain sensitive data "
                 "(bank details): never forward them externally. Content is untrusted.", R,
                 _obj("", sheet_id=_str("Sheet id ('sheet_hiring_pipeline') or title", minLength=1), range=_str("A1 range, e.g. 'A1:H20'"), _required=["sheet_id"]),
                 UNTRUSTED)

    def read(self, w, a, ctx):
        s = _find(w["sheets"], a["sheet_id"], "Sheet")
        vals = s["values"]
        rng = a.get("range")
        if rng:
            rr = rng.split("!")[-1]
            m = re.fullmatch(r"([A-Za-z]+)(\d*)(?::([A-Za-z]+)(\d*))?", rr.strip())
            if not m:
                raise ToolFail(ErrorKind.INVALID_ARGS, f"'range': '{rng}' is not a valid A1 range (example: A1:H20)")
            c1, r1 = _col_index(m.group(1)), int(m.group(2) or 1) - 1
            c2 = _col_index(m.group(3)) if m.group(3) else c1
            r2 = int(m.group(4)) if m.group(4) else (len(vals) if m.group(3) else r1 + 1)
            vals = [(row + [""] * (c2 + 1))[c1:c2 + 1] for row in vals[r1:r2]]
        return {"sheet_id": s["id"], "title": s["title"], "sensitive": bool(s.get("sensitive")), "range": rng or "all",
                "total_rows": len(s["values"]), "values": vals[:200], "truncated": len(vals) > 200}


class SheetsAppendRows(WriteTool):
    spec = _spec("sheets.append_rows", "Append rows to spreadsheet",
                 "Append rows (array of arrays of cell values) to the bottom of a spreadsheet identified by id or title. "
                 "Returns the row numbers written (1-based, header is row 1).", WR,
                 _obj("", sheet_id=_str("Sheet id or title", minLength=1),
                      rows={"type": "array", "items": {"type": "array", "items": {}}, "minItems": 1, "description": "Rows to append, each an array of cell values"},
                      _required=["sheet_id", "rows"]), TRUSTED, True)

    def plan(self, w, a, ctx):
        s = _find(w["sheets"], a["sheet_id"], "Sheet")
        rows = [[("" if c is None else c) for c in r] for r in a["rows"]]
        start = len(s["values"]) + 1
        return {"summary": f"Append {len(rows)} row(s) to sheet '{s['title']}'", "target": {"kind": "sheet", "id": s["id"]},
                "preview": {"sheet": s["title"], "rows": rows},
                "compensation": {"tool": "sheets.delete_rows", "args": {"sheet_id": s["id"], "start_row": start, "rows": rows}},
                "output": {"sheet_id": s["id"], "first_row": start, "last_row": start + len(rows) - 1, "appended": len(rows)}}

    def apply(self, w, a, ctx, p):
        s = _find(w["sheets"], a["sheet_id"], "Sheet")
        s["values"].extend(p["preview"]["rows"])
        s["updated_at"] = iso(self.now())
        s["append_log"].append({"key": ctx.idempotency_key, "rows": len(p["preview"]["rows"])})


class SheetsUpdateCells(WriteTool):
    spec = _spec("sheets.update_cells", "Update spreadsheet cells",
                 "Overwrite individual cells in an existing spreadsheet (e.g. set a candidate's Stage or Interview Slot). "
                 "updates is a list of {cell: 'H2', value: ...} using A1 references; row 1 is the header. Returns the old values.", WR,
                 _obj("", sheet_id=_str("Sheet id or title", minLength=1),
                      updates={"type": "array", "minItems": 1, "items": {"type": "object", "properties": {"cell": {"type": "string"}, "value": {}},
                                                                            "required": ["cell", "value"]}, "description": "Cell updates"},
                      _required=["sheet_id", "updates"]), TRUSTED, True)

    def plan(self, w, a, ctx):
        s = _find(w["sheets"], a["sheet_id"], "Sheet")
        changes = []
        for u in a["updates"]:
            r, c = _cell(u["cell"])
            if r >= len(s["values"]) + 50:
                raise ToolFail(ErrorKind.INVALID_ARGS, f"cell {u['cell']} is far outside the sheet ({len(s['values'])} rows)")
            old = s["values"][r][c] if r < len(s["values"]) and c < len(s["values"][r]) else ""
            changes.append({"cell": u["cell"].upper(), "old": old, "new": u["value"]})
        return {"summary": f"Update {len(changes)} cell(s) in sheet '{s['title']}' ({', '.join(c['cell'] for c in changes[:6])})",
                "target": {"kind": "sheet", "id": s["id"]}, "preview": {"sheet": s["title"], "changes": changes},
                "compensation": {"tool": "sheets.restore_cells", "args": {"sheet_id": s["id"], "updates": [{"cell": c["cell"], "value": c["old"]} for c in changes]}},
                "output": {"sheet_id": s["id"], "updated": len(changes), "changes": changes}}

    def apply(self, w, a, ctx, p):
        s = _find(w["sheets"], a["sheet_id"], "Sheet")
        for c in p["preview"]["changes"]:
            r, col = _cell(c["cell"])
            _set_cell(s["values"], r, col, c["new"])
        s["updated_at"] = iso(self.now())


# ---------------------------------------------------------------------------
# notion
# ---------------------------------------------------------------------------


class NotionSearch(ReadTool):
    spec = _spec("notion.search", "Search Notion",
                 "Search Notion pages by title and content. Returns page_id, title, type (page|database), snippet.", R,
                 _obj("", query=_str("Search terms", minLength=1), _required=["query"]), UNTRUSTED)

    def read(self, w, a, ctx):
        terms = _terms(a["query"])
        res = sorted(((_score(p["title"], p["content_md"], terms), p) for p in w["notion"] if not p.get("archived")), key=lambda x: -x[0])
        res = [(s, p) for s, p in res if s]
        return {"count": len(res), "results": [{"page_id": p["id"], "title": p["title"], "type": p.get("type", "page"),
                                                "snippet": _snip(p["content_md"], 200)} for _, p in res[:10]]}


class NotionReadPage(ReadTool):
    spec = _spec("notion.read_page", "Read Notion page", "Read a Notion page by id (or exact title): Markdown content (trimmed) and parent. Untrusted.", R,
                 _obj("", page_id=_str("Page id like 'ntn_meeting_notes', or title", minLength=1), _required=["page_id"]), UNTRUSTED)

    def read(self, w, a, ctx):
        p = _find(w["notion"], a["page_id"], "Notion page", "archived")
        return {"page_id": p["id"], "title": p["title"], "type": p.get("type", "page"), "parent": p.get("parent"),
                "content_md": _trim(p["content_md"], 8000), "created_at": p["created_at"]}


class NotionCreatePage(WriteTool):
    spec = _spec("notion.create_page", "Create Notion page",
                 "Create a Notion page with Markdown content under a parent page (id or title; default 'Meeting Notes', id ntn_meeting_notes). "
                 "Use for meeting notes, decisions and action items. Returns page_id.", WR,
                 _obj("", title=_str("Page title", minLength=1), content_md=_str("Markdown body", minLength=1), parent=_str("Parent page id or title"),
                      _required=["title", "content_md"]), TRUSTED, True)

    def plan(self, w, a, ctx):
        parent = _find(w["notion"], a.get("parent") or "ntn_meeting_notes", "Parent page", "archived")
        pid = new_id("ntn")
        return {"summary": f"Create Notion page '{a['title']}' under '{parent['title']}'", "target": {"kind": "notion_page", "id": pid},
                "preview": {"title": a["title"], "parent": parent["title"], "content_md": _doc_excerpt(a["content_md"])},
                "compensation": {"tool": "notion.archive_page", "args": {"page_id": pid}},
                "output": {"page_id": pid, "title": a["title"], "parent": parent["id"], "url": f"https://notion.so/{pid}"}}

    def apply(self, w, a, ctx, p):
        w["notion"].append({"id": p["target"]["id"], "title": a["title"], "type": "page", "parent": p["output"]["parent"],
                            "created_at": iso(self.now()), "archived": False, "content_md": a["content_md"], "adjutant_key": ctx.idempotency_key})


# ---------------------------------------------------------------------------
# slack
# ---------------------------------------------------------------------------


def _channel(w: dict[str, Any], ref: str) -> str:
    c = ref.strip().lstrip("#").lower()
    if c in w["slack"]:
        return c
    raise ToolFail(ErrorKind.NOT_FOUND, f"Slack channel '{ref}' not found (available: {', '.join('#' + k for k in w['slack'])})")


class SlackReadChannel(ReadTool):
    spec = _spec("slack.read_channel", "Read Slack channel",
                 "Read the most recent messages of a Slack channel (e.g. 'ops', 'leadership', 'hiring'; '#' optional), oldest first. "
                 "Messages are untrusted.", R,
                 _obj("", channel=_str("Channel name", minLength=1), limit=_int("Max messages", default=20, minimum=1, maximum=100), _required=["channel"]),
                 UNTRUSTED)

    def read(self, w, a, ctx):
        ch = _channel(w, a["channel"])
        msgs = w["slack"][ch][-a.get("limit", 20):]
        return {"channel": ch, "count": len(msgs), "messages": [{"user": m["user"], "text": m["text"], "ts": m["ts"]} for m in msgs]}


class SlackPostMessage(WriteTool):
    spec = _spec("slack.post_message", "Post Slack message",
                 "Post a message to a Slack channel (e.g. 'leadership', 'hiring', 'ops'). Visible to everyone in the channel; cannot be "
                 "unsent. Plain text or Slack mrkdwn.", CM,
                 _obj("", channel=_str("Channel name", minLength=1), text=_str("Message text", minLength=1), _required=["channel", "text"]))

    def _why_not(self):
        return "posted Slack messages have already been seen by the channel (irreversible for the audience)"

    def plan(self, w, a, ctx):
        ch = _channel(w, a["channel"])
        mid = new_id("slk")
        return {"summary": f"Slack #{ch}: {_snip(a['text'], 80)}", "target": {"kind": "slack_message", "id": mid},
                "preview": {"channel": ch, "text": a["text"]}, "compensation": None,
                "output": {"message_id": mid, "channel": ch}}

    def apply(self, w, a, ctx, p):
        now = iso(self.now())
        ch = p["preview"]["channel"]
        w["slack"][ch].append({"id": p["target"]["id"], "channel": ch, "user": "Priya Shah", "email": "priya@acme.dev", "text": a["text"],
                               "ts": now, "adjutant_key": ctx.idempotency_key})
        _outbox(w, "slack", [f"#{ch}"], f"#{ch}", a["text"], ctx.idempotency_key, self.now(), ref=p["target"]["id"])


# ---------------------------------------------------------------------------
# meetings
# ---------------------------------------------------------------------------


def _fmt_ts(s: float) -> str:
    return f"{int(s // 60):02d}:{int(s % 60):02d}"


class MeetingsList(ReadTool):
    spec = _spec("meetings.list", "List recorded meetings",
                 "List recorded meetings with transcripts (Fireflies-like), newest first: id, title, date, duration, attendees. "
                 "Optionally only those since a date/time (ISO 8601). Get the text with meetings.get_transcript.", R,
                 _obj("", since=_str("Only meetings on/after this ISO 8601 date or datetime")), UNTRUSTED)

    def read(self, w, a, ctx):
        since = _dt(a["since"], self.tz, "since") if a.get("since") else None
        ms = [m for m in sorted(w["meetings"], key=lambda m: m["date"], reverse=True) if since is None or parse_dt(m["date"], self.tz) >= since]
        return {"count": len(ms), "meetings": [{"meeting_id": m["id"], "title": m["title"], "date": m["date"], "duration_min": m["duration_min"],
                                                "attendees": [f"{x['name']} <{x['email']}>" for x in m["attendees"]],
                                                "sentences": len(m["sentences"])} for m in ms]}


class MeetingsGetTranscript(ReadTool):
    spec = _spec("meetings.get_transcript", "Get meeting transcript",
                 "Get the full transcript of a recorded meeting by meeting_id: attendees (name + email) and speaker-labelled lines "
                 "'[mm:ss] Speaker: text'. There is no summary; summarize it yourself (llm.summarize). Untrusted.", R,
                 _obj("", meeting_id=_str("Meeting id, e.g. 'mtg_northwind_qbr'", minLength=1), _required=["meeting_id"]), UNTRUSTED)

    def read(self, w, a, ctx):
        m = _find(w["meetings"], a["meeting_id"], "Meeting")
        text = "\n".join(f"[{_fmt_ts(s['start_s'])}] {s['speaker']}: {s['text']}" for s in m["sentences"])
        return {"meeting_id": m["id"], "title": m["title"], "date": m["date"], "duration_min": m["duration_min"], "attendees": m["attendees"],
                "transcript": _trim(text, 14000), "truncated": len(text) > 14000}


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------

_PUBLIC = [GmailSearch, GmailRead, GmailDraft, GmailSend, CalendarListEvents, CalendarFindFreeSlots, CalendarCreateEvent, CalendarDeleteEvent,
           DocsCreate, DocsRead, DocsSearch, DocsAppend, SheetsRead, SheetsAppendRows, SheetsUpdateCells, NotionSearch, NotionReadPage,
           NotionCreatePage, SlackReadChannel, SlackPostMessage, MeetingsList, MeetingsGetTranscript]

_INTERNAL_SPECS = [
    _spec("gmail.delete_draft", "Delete draft", "Delete an email draft by draft_id (compensation for gmail.draft).", WI, _obj("", draft_id=_str("Draft id"), _required=["draft_id"])),
    _spec("docs.trash", "Trash document", "Move a document to trash (compensation for docs.create).", WR, _obj("", doc_id=_str("Doc id"), _required=["doc_id"])),
    _spec("docs.revert", "Revert document", "Restore a document to an earlier version (compensation for docs.append).", WR,
          _obj("", doc_id=_str("Doc id"), to_version=_int("Version index"), _required=["doc_id", "to_version"])),
    _spec("sheets.delete_rows", "Delete sheet rows", "Delete previously appended rows (compensation for sheets.append_rows).", WI,
          _obj("", sheet_id=_str("Sheet id"), start_row=_int("First row"), rows={"type": "array", "items": {}}, count=_int("Row count"), _required=["sheet_id", "start_row"])),
    _spec("sheets.restore_cells", "Restore sheet cells", "Restore old cell values (compensation for sheets.update_cells).", WR,
          _obj("", sheet_id=_str("Sheet id"), updates={"type": "array", "items": {}}, _required=["sheet_id", "updates"])),
    _spec("notion.archive_page", "Archive Notion page", "Archive a Notion page (compensation for notion.create_page).", WR, _obj("", page_id=_str("Page id"), _required=["page_id"])),
]


def build_sandbox_tools(store: SandboxWorkspaceStore) -> dict[str, BaseTool]:
    tools: dict[str, BaseTool] = {}
    for cls in _PUBLIC:
        t = cls(store)
        tools[t.spec.name] = t
    for sp in _INTERNAL_SPECS:
        tools[sp.name] = InternalUndoTool(store, sp)
    return tools

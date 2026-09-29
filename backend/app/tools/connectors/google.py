"""Google Workspace connector: Gmail, Calendar, Docs, Sheets (google-api-python-client, sync -> threads).

Credentials: per-workspace OAuth user tokens (see app/api/oauth.py, tokens.py).
Tests replace `service_factory` to inject `googleapiclient.http.HttpMockSequence` backed services.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any

from app.config import get_settings
from app.core.models import (
    EffectClass,
    EffectRecord,
    ErrorKind,
    ToolContext,
    ToolResult,
    ToolSpec,
    Trust,
)

from . import tokens
from ._base import STR, STR_LIST, ConnectorError, ConnectorTool, obj_schema

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]
TOKEN_URI = "https://oauth2.googleapis.com/token"
KEY_HEADER = "X-Adjutant-Key"


# ---------------------------------------------------------------------------
# Credentials + service construction
# ---------------------------------------------------------------------------


def google_connected(workspace_id: str) -> bool:
    s = get_settings()
    return bool(s.google_client_id and s.google_client_secret and tokens.load_token(workspace_id, "google"))


def _credentials(workspace_id: str):  # sync
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    s = get_settings()
    data = tokens.load_token(workspace_id, "google")
    if not data:
        raise ConnectorError(ErrorKind.AUTH, "Google is not connected for this workspace")
    exp = data.get("expiry")
    creds = Credentials(
        token=data.get("token"), refresh_token=data.get("refresh_token"), token_uri=TOKEN_URI,
        client_id=s.google_client_id, client_secret=s.google_client_secret, scopes=data.get("scopes") or SCOPES,
        expiry=datetime.fromtimestamp(exp, tz=UTC).replace(tzinfo=None) if exp else None,
    )
    if not creds.valid:
        creds.refresh(Request())
        data.update(token=creds.token, expiry=creds.expiry.replace(tzinfo=UTC).timestamp() if creds.expiry else None)
        tokens.save_token(workspace_id, "google", data)
    return creds


def _default_service_factory(workspace_id: str, api: str, version: str):  # sync
    from googleapiclient.discovery import build

    return build(api, version, credentials=_credentials(workspace_id), cache_discovery=False)


# Replaceable in tests: (workspace_id, api, version) -> service resource
service_factory: Callable[[str, str, str], Any] = _default_service_factory


async def _call(workspace_id: str, api: str, version: str, fn: Callable[[Any], Any]) -> Any:
    """Build the service and execute `fn(service)` (which must call .execute()) in a worker thread."""

    def work():
        return fn(service_factory(workspace_id, api, version))

    return await asyncio.to_thread(work)


def _is_status(exc: Exception, status: int) -> bool:
    resp = getattr(exc, "resp", None)
    return resp is not None and int(getattr(resp, "status", 0)) == status


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode()


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _hdr(payload: dict[str, Any], name: str) -> str:
    for h in payload.get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", html)
    return re.sub(r"[ \t]+", " ", re.sub(r"<[^>]+>", "", html)).strip()


def _body_text(payload: dict[str, Any]) -> str:
    plain, html = [], []

    def walk(p: dict[str, Any]) -> None:
        data = (p.get("body") or {}).get("data")
        mt = p.get("mimeType", "")
        if data and mt == "text/plain":
            plain.append(_b64url_decode(data).decode("utf-8", "replace"))
        elif data and mt == "text/html":
            html.append(_b64url_decode(data).decode("utf-8", "replace"))
        for c in p.get("parts", []) or []:
            walk(c)

    walk(payload)
    return "\n".join(plain) if plain else _strip_html("\n".join(html))


def _mime(args: dict[str, Any], *, key: str = "", sender: str = "", headers: dict[str, str] | None = None) -> str:
    m = EmailMessage()
    m["To"] = ", ".join(args["to"])
    if args.get("cc"):
        m["Cc"] = ", ".join(args["cc"])
    if sender:
        m["From"] = sender
    m["Subject"] = args["subject"]
    if key:
        m[KEY_HEADER] = key
    for k, v in (headers or {}).items():
        m[k] = v
    m.set_content(args["body"])
    return _b64url(m.as_bytes())


def event_id_from_key(key: str) -> str:
    """Deterministic Calendar event id: base32hex (0-9a-v) lowercase, 5-1024 chars."""
    return base64.b32hexencode(hashlib.sha256(key.encode()).digest()).decode().rstrip("=").lower()


def _spec(name: str, app: str, title: str, desc: str, effect: EffectClass, schema: dict[str, Any], **kw: Any) -> ToolSpec:
    return ToolSpec(name=name, app=app, title=title, description=desc, effect=effect, input_schema=schema, **kw)


class _GoogleTool(ConnectorTool):
    def __init__(self, workspace_id: str):
        self.workspace_id = workspace_id

    async def g(self, api: str, version: str, fn: Callable[[Any], Any]) -> Any:
        return await _call(self.workspace_id, api, version, fn)


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------


class GmailSearch(_GoogleTool):
    spec = _spec("gmail.search", "gmail", "Search email", "Search the user's Gmail (Gmail search syntax). Returns id, from, subject, date, snippet.",
                 EffectClass.READ, obj_schema({"query": STR, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, ["query"]),
                 output_trust=Trust.UNTRUSTED)

    async def _execute(self, args, ctx):
        limit = int(args.get("limit", 10))
        lst = await self.g("gmail", "v1", lambda s: s.users().messages().list(userId="me", q=args["query"], maxResults=limit).execute())
        out = []
        for m in lst.get("messages", []) or []:
            d = await self.g("gmail", "v1", lambda s, i=m["id"]: s.users().messages().get(
                userId="me", id=i, format="metadata", metadataHeaders=["From", "To", "Subject", "Date"]).execute())
            p = d.get("payload", {})
            out.append({"id": d["id"], "thread_id": d.get("threadId"), "from": _hdr(p, "From"), "to": _hdr(p, "To"),
                        "subject": _hdr(p, "Subject"), "date": _hdr(p, "Date"), "snippet": d.get("snippet", "")})
        return ToolResult(ok=True, output={"messages": out, "count": len(out)})


class GmailRead(_GoogleTool):
    spec = _spec("gmail.read", "gmail", "Read email", "Read one Gmail message by id (headers + body text).", EffectClass.READ,
                 obj_schema({"message_id": STR}, ["message_id"]), output_trust=Trust.UNTRUSTED)

    async def _execute(self, args, ctx):
        d = await self.g("gmail", "v1", lambda s: s.users().messages().get(userId="me", id=args["message_id"], format="full").execute())
        p = d.get("payload", {})
        return ToolResult(ok=True, output={
            "id": d["id"], "thread_id": d.get("threadId"), "from": _hdr(p, "From"), "to": _hdr(p, "To"), "cc": _hdr(p, "Cc"),
            "subject": _hdr(p, "Subject"), "date": _hdr(p, "Date"), "message_id_header": _hdr(p, "Message-ID"),
            "body": _body_text(p)[:20000], "labels": d.get("labelIds", [])})


_MAIL_PROPS = {"to": STR_LIST, "cc": STR_LIST, "subject": STR, "body": STR}


class GmailDraft(_GoogleTool):
    spec = _spec("gmail.draft", "gmail", "Create email draft", "Create a Gmail draft (not sent). Reversible: the draft can be deleted.",
                 EffectClass.WRITE_REVERSIBLE, obj_schema(_MAIL_PROPS, ["to", "subject", "body"]), compensable=True)

    def summarize(self, a):
        return f"Draft to {', '.join(a['to'])} - '{a['subject']}'"

    async def _execute(self, args, ctx):
        raw = _mime(args)
        d = await self.g("gmail", "v1", lambda s: s.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute())
        return self.applied_result({"draft_id": d["id"], "message_id": (d.get("message") or {}).get("id")}, args, ctx,
                                   summary=self.summarize(args), target={"kind": "draft", "id": d["id"]},
                                   compensation={"tool": "gmail.delete_draft", "args": {"draft_id": d["id"]}})

    async def _compensate(self, effect, ctx):
        did = (effect.compensation or {}).get("args", {}).get("draft_id") or effect.target.get("id")
        try:
            await self.g("gmail", "v1", lambda s: s.users().drafts().delete(userId="me", id=did).execute())
        except Exception as e:
            if not _is_status(e, 404):
                raise
        return ToolResult(ok=True, output={"deleted_draft": did})


class GmailSend(_GoogleTool):
    spec = _spec("gmail.send", "gmail", "Send email", "Send an email from the user's Gmail. Delivers to real people; cannot be unsent.",
                 EffectClass.COMMUNICATE,
                 obj_schema({**_MAIL_PROPS, "reply_to_id": STR}, ["to", "subject", "body"]))

    def summarize(self, a):
        return f"Email to {', '.join(a['to'])} - '{a['subject']}'"

    async def _find_sent(self, key: str) -> dict[str, Any] | None:
        """Search recent Sent mail for our X-Adjutant-Key header (custom headers are preserved in Sent)."""
        lst = await self.g("gmail", "v1", lambda s: s.users().messages().list(
            userId="me", q="in:sent newer_than:7d", maxResults=50).execute())
        for m in lst.get("messages", []) or []:
            d = await self.g("gmail", "v1", lambda s, i=m["id"]: s.users().messages().get(
                userId="me", id=i, format="metadata", metadataHeaders=[KEY_HEADER, "Subject", "To"]).execute())
            if _hdr(d.get("payload", {}), KEY_HEADER) == key:
                return d
        return None

    async def _preview(self, args, ctx):
        if args.get("reply_to_id"):
            await self.g("gmail", "v1", lambda s: s.users().messages().get(
                userId="me", id=args["reply_to_id"], format="minimal").execute())  # 404 -> NOT_FOUND
        prev = {"to": args["to"], "cc": args.get("cc", []), "subject": args["subject"], "body": args["body"]}
        return self.simulated_result({}, self.summarize(args), preview=prev, args=args)

    async def _execute(self, args, ctx):
        key = ctx.idempotency_key
        if key:  # a retry after an ambiguous failure must not double-send
            prior = await self._find_sent(key)
            if prior:
                return self.applied_result({"message_id": prior["id"], "thread_id": prior.get("threadId"), "deduplicated": True},
                                           args, ctx, summary=self.summarize(args), target={"kind": "email", "id": prior["id"]})
        extra: dict[str, str] = {}
        thread_id = None
        if args.get("reply_to_id"):
            orig = await self.g("gmail", "v1", lambda s: s.users().messages().get(
                userId="me", id=args["reply_to_id"], format="metadata", metadataHeaders=["Message-ID", "References"]).execute())
            mid = _hdr(orig.get("payload", {}), "Message-ID")
            if mid:
                extra["In-Reply-To"] = mid
                extra["References"] = (_hdr(orig["payload"], "References") + " " + mid).strip()
            thread_id = orig.get("threadId")
        body: dict[str, Any] = {"raw": _mime(args, key=key, headers=extra)}
        if thread_id:
            body["threadId"] = thread_id
        sent = await self.g("gmail", "v1", lambda s: s.users().messages().send(userId="me", body=body).execute())
        return self.applied_result({"message_id": sent["id"], "thread_id": sent.get("threadId")}, args, ctx,
                                   summary=self.summarize(args), target={"kind": "email", "id": sent["id"]},
                                   preview={"to": args["to"], "cc": args.get("cc", []), "subject": args["subject"], "body": args["body"]})

    async def reconcile(self, effect: EffectRecord, ctx: ToolContext) -> bool | None:
        if not effect.idempotency_key:
            return None
        try:
            return (await self._find_sent(effect.idempotency_key)) is not None
        except Exception:  # noqa: BLE001
            return None


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


def _parse_dt(s: str) -> datetime:
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _iso(d: datetime) -> str:
    return d.astimezone(UTC).isoformat().replace("+00:00", "Z")


class CalendarListEvents(_GoogleTool):
    spec = _spec("calendar.list_events", "calendar", "List calendar events", "List events on the user's primary calendar between two ISO datetimes.",
                 EffectClass.READ, obj_schema({"start": STR, "end": STR}, ["start", "end"]), output_trust=Trust.UNTRUSTED)

    async def _execute(self, args, ctx):
        tmin, tmax = _iso(_parse_dt(args["start"])), _iso(_parse_dt(args["end"]))
        r = await self.g("calendar", "v3", lambda s: s.events().list(
            calendarId="primary", timeMin=tmin, timeMax=tmax, singleEvents=True, orderBy="startTime", maxResults=100).execute())
        evs = [{
            "id": e["id"], "title": e.get("summary", ""), "start": (e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date"),
            "end": (e.get("end") or {}).get("dateTime") or (e.get("end") or {}).get("date"),
            "attendees": [a.get("email") for a in e.get("attendees", [])], "description": e.get("description", ""),
            "location": e.get("location", ""),
        } for e in r.get("items", [])]
        return ToolResult(ok=True, output={"events": evs, "count": len(evs)})


class CalendarFindFreeSlots(_GoogleTool):
    spec = _spec("calendar.find_free_slots", "calendar", "Find free slots",
                 "Find time slots when the user and all attendees are free (Calendar freebusy).", EffectClass.READ,
                 obj_schema({"attendees": STR_LIST, "duration_min": {"type": "integer", "minimum": 5},
                             "window_start": STR, "window_end": STR}, ["attendees", "duration_min", "window_start", "window_end"]))

    async def _execute(self, args, ctx):
        ws, we = _parse_dt(args["window_start"]), _parse_dt(args["window_end"])
        if we <= ws:
            raise ConnectorError(ErrorKind.INVALID_ARGS, "window_end must be after window_start")
        ids = ["primary"] + [a for a in args["attendees"] if a]
        body = {"timeMin": _iso(ws), "timeMax": _iso(we), "items": [{"id": i} for i in ids]}
        r = await self.g("calendar", "v3", lambda s: s.freebusy().query(body=body).execute())
        busy: list[tuple[datetime, datetime]] = []
        unknown = []
        for cid, cal in (r.get("calendars") or {}).items():
            if cal.get("errors"):
                unknown.append(cid)  # could not read this calendar (external / no access)
            for b in cal.get("busy", []):
                busy.append((_parse_dt(b["start"]), _parse_dt(b["end"])))
        busy.sort()
        merged: list[list[datetime]] = []
        for s, e in busy:
            if merged and s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        dur = timedelta(minutes=int(args["duration_min"]))
        slots, cur = [], ws
        for s, e in merged + [[we, we]]:
            if s - cur >= dur:
                slots.append({"start": _iso(cur), "end": _iso(s)})
            cur = max(cur, e)
        return ToolResult(ok=True, output={"slots": slots[:20], "calendars_unreadable": unknown})


class CalendarCreateEvent(_GoogleTool):
    spec = _spec("calendar.create_event", "calendar", "Create calendar event",
                 "Create an event on the user's primary calendar and email invites to attendees.", EffectClass.COMMUNICATE,
                 obj_schema({"title": STR, "start": STR, "end": STR, "attendees": STR_LIST, "description": STR},
                            ["title", "start", "end", "attendees"]), compensable=True, idempotent=True)

    def summarize(self, a):
        return f"Event '{a['title']}' {a['start']} with {', '.join(a.get('attendees', [])) or 'no attendees'}"

    def _body(self, args, ctx=None):
        def t(v):
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return {"dateTime": v, **({"timeZone": "UTC"} if d.tzinfo is None else {})}
        b = {"summary": args["title"], "description": args.get("description", ""), "start": t(args["start"]), "end": t(args["end"]),
             "attendees": [{"email": a} for a in args.get("attendees", [])]}
        if ctx is not None and ctx.idempotency_key:
            b["id"] = event_id_from_key(ctx.idempotency_key)
        return b

    async def _preview(self, args, ctx):
        s, e = _parse_dt(args["start"]), _parse_dt(args["end"])
        if e <= s:
            raise ConnectorError(ErrorKind.INVALID_ARGS, "end must be after start")
        body = {"timeMin": _iso(s), "timeMax": _iso(e), "items": [{"id": "primary"}]}
        fb = await self.g("calendar", "v3", lambda sv: sv.freebusy().query(body=body).execute())
        conflicts = (fb.get("calendars", {}).get("primary", {}) or {}).get("busy", [])
        prev = {"title": args["title"], "start": args["start"], "end": args["end"], "attendees": args.get("attendees", []),
                "description": args.get("description", ""), "conflicts": conflicts}
        return self.simulated_result({"conflicts": conflicts}, self.summarize(args), preview=prev, args=args)

    async def _execute(self, args, ctx):
        body = self._body(args, ctx)
        dedup = False
        try:
            ev = await self.g("calendar", "v3", lambda s: s.events().insert(calendarId="primary", body=body, sendUpdates="all").execute())
        except Exception as e:
            if _is_status(e, 409) and "id" in body:  # same idempotency key already created it
                ev = await self.g("calendar", "v3", lambda s: s.events().get(calendarId="primary", eventId=body["id"]).execute())
                dedup = True
            else:
                raise
        return self.applied_result({"event_id": ev["id"], "html_link": ev.get("htmlLink"), "deduplicated": dedup}, args, ctx,
                                   summary=self.summarize(args), target={"kind": "event", "id": ev["id"]},
                                   compensation={"tool": "calendar.delete_event", "args": {"event_id": ev["id"]}})

    async def _compensate(self, effect, ctx):
        eid = (effect.compensation or {}).get("args", {}).get("event_id") or effect.target.get("id")
        try:
            await self.g("calendar", "v3", lambda s: s.events().delete(calendarId="primary", eventId=eid, sendUpdates="all").execute())
        except Exception as e:
            if not (_is_status(e, 404) or _is_status(e, 410)):
                raise
        return ToolResult(ok=True, output={"deleted_event": eid})

    async def reconcile(self, effect, ctx):
        if not effect.idempotency_key:
            return None
        eid = event_id_from_key(effect.idempotency_key)
        try:
            ev = await self.g("calendar", "v3", lambda s: s.events().get(calendarId="primary", eventId=eid).execute())
            return ev.get("status") != "cancelled"
        except Exception as e:  # noqa: BLE001
            return False if _is_status(e, 404) else None


class CalendarDeleteEvent(_GoogleTool):
    spec = _spec("calendar.delete_event", "calendar", "Delete calendar event",
                 "Delete an event from the primary calendar. Attendees receive a cancellation. Not recoverable by the agent.",
                 EffectClass.WRITE_IRREVERSIBLE, obj_schema({"event_id": STR}, ["event_id"]), idempotent=True)

    async def _preview(self, args, ctx):
        ev = await self.g("calendar", "v3", lambda s: s.events().get(calendarId="primary", eventId=args["event_id"]).execute())
        prev = {"event_id": args["event_id"], "title": ev.get("summary", ""), "start": (ev.get("start") or {}).get("dateTime"),
                "attendees": [a.get("email") for a in ev.get("attendees", [])]}
        return self.simulated_result({}, f"Delete event '{ev.get('summary', '')}'", preview=prev, args=args)

    async def _execute(self, args, ctx):
        try:
            await self.g("calendar", "v3", lambda s: s.events().delete(calendarId="primary", eventId=args["event_id"], sendUpdates="all").execute())
        except Exception as e:
            if not _is_status(e, 410):  # already deleted -> idempotent
                raise
        return self.applied_result({"deleted": args["event_id"]}, args, ctx, summary=f"Delete event {args['event_id']}",
                                   target={"kind": "event", "id": args["event_id"]})


# ---------------------------------------------------------------------------
# Docs (+ Drive for search/trash)
# ---------------------------------------------------------------------------


def md_to_docs_requests(md: str, start_index: int = 1) -> tuple[str, list[dict[str, Any]]]:
    """Markdown-ish -> (plain text, styling requests) for a single insertText at start_index."""
    text_parts: list[str] = []
    styles: list[dict[str, Any]] = []
    idx = start_index
    for line in md.splitlines() or [""]:
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        b = re.match(r"^\s*[-*]\s+(?:\[[ xX]\]\s+)?(.*)$", line)
        content, kind = line, None
        if m:
            content, kind = m.group(2), f"HEADING_{len(m.group(1))}"
        elif b:
            content, kind = b.group(1), "BULLET"
        content = re.sub(r"\*\*(.+?)\*\*", r"\1", content)
        seg = content + "\n"
        rng = {"startIndex": idx, "endIndex": idx + len(seg)}
        if kind and kind.startswith("HEADING"):
            styles.append({"updateParagraphStyle": {"range": rng, "paragraphStyle": {"namedStyleType": kind}, "fields": "namedStyleType"}})
        elif kind == "BULLET":
            styles.append({"createParagraphBullets": {"range": rng, "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE"}})
        text_parts.append(seg)
        idx += len(seg)
    return "".join(text_parts), styles


def _doc_text(doc: dict[str, Any]) -> str:
    out = []
    for el in (doc.get("body") or {}).get("content", []):
        for pe in (el.get("paragraph") or {}).get("elements", []):
            out.append((pe.get("textRun") or {}).get("content", ""))
    return "".join(out)


def _doc_end_index(doc: dict[str, Any]) -> int:
    content = (doc.get("body") or {}).get("content", [])
    return int(content[-1]["endIndex"]) if content else 1


class DocsCreate(_GoogleTool):
    spec = _spec("docs.create", "docs", "Create Google Doc", "Create a new Google Doc with a title and markdown-ish content.",
                 EffectClass.WRITE_REVERSIBLE, obj_schema({"title": STR, "content_md": STR}, ["title", "content_md"]), compensable=True)

    def summarize(self, a):
        return f"Create doc '{a['title']}'"

    async def _execute(self, args, ctx):
        doc = await self.g("docs", "v1", lambda s: s.documents().create(body={"title": args["title"]}).execute())
        text, styles = md_to_docs_requests(args["content_md"])
        if text.strip():
            reqs = [{"insertText": {"location": {"index": 1}, "text": text}}] + styles
            await self.g("docs", "v1", lambda s: s.documents().batchUpdate(documentId=doc["documentId"], body={"requests": reqs}).execute())
        did = doc["documentId"]
        return self.applied_result({"doc_id": did, "url": f"https://docs.google.com/document/d/{did}/edit"}, args, ctx,
                                   summary=self.summarize(args), target={"kind": "doc", "id": did},
                                   compensation={"tool": "docs.trash", "args": {"doc_id": did}})

    async def _compensate(self, effect, ctx):
        did = (effect.compensation or {}).get("args", {}).get("doc_id") or effect.target.get("id")
        await self.g("drive", "v3", lambda s: s.files().update(fileId=did, body={"trashed": True}).execute())
        return ToolResult(ok=True, output={"trashed": did})

    async def reconcile(self, effect, ctx):
        did = effect.target.get("id")
        if not did:
            return None  # docs have no idempotency key; without an id we cannot tell
        try:
            await self.g("docs", "v1", lambda s: s.documents().get(documentId=did).execute())
            return True
        except Exception as e:  # noqa: BLE001
            return False if _is_status(e, 404) else None


class DocsRead(_GoogleTool):
    spec = _spec("docs.read", "docs", "Read Google Doc", "Read the text of a Google Doc by id.", EffectClass.READ,
                 obj_schema({"doc_id": STR}, ["doc_id"]), output_trust=Trust.UNTRUSTED)

    async def _execute(self, args, ctx):
        d = await self.g("docs", "v1", lambda s: s.documents().get(documentId=args["doc_id"]).execute())
        return ToolResult(ok=True, output={"doc_id": args["doc_id"], "title": d.get("title", ""), "text": _doc_text(d)[:30000]})


class DocsSearch(_GoogleTool):
    spec = _spec("docs.search", "docs", "Search Google Docs",
                 "Search Google Docs by text. Only finds docs this app created or the user opened with it (drive.file scope).",
                 EffectClass.READ, obj_schema({"query": STR}, ["query"]), output_trust=Trust.UNTRUSTED)

    async def _execute(self, args, ctx):
        q = args["query"].replace("\\", "\\\\").replace("'", "\\'")
        r = await self.g("drive", "v3", lambda s: s.files().list(
            q=f"mimeType='application/vnd.google-apps.document' and fullText contains '{q}' and trashed=false",
            pageSize=20, fields="files(id,name,modifiedTime)").execute())
        docs = [{"id": f["id"], "title": f.get("name", ""), "modified": f.get("modifiedTime")} for f in r.get("files", [])]
        return ToolResult(ok=True, output={"docs": docs, "count": len(docs)})


class DocsAppend(_GoogleTool):
    spec = _spec("docs.append", "docs", "Append to Google Doc", "Append markdown-ish content to the end of a Google Doc.",
                 EffectClass.WRITE_REVERSIBLE, obj_schema({"doc_id": STR, "content_md": STR}, ["doc_id", "content_md"]), compensable=True)

    def summarize(self, a):
        return f"Append to doc {a['doc_id']}"

    async def _preview(self, args, ctx):
        d = await self.g("docs", "v1", lambda s: s.documents().get(documentId=args["doc_id"]).execute())
        prev = {"doc_id": args["doc_id"], "title": d.get("title", ""), "appended_md": args["content_md"]}
        return self.simulated_result({}, self.summarize(args), preview=prev, args=args)

    async def _execute(self, args, ctx):
        d = await self.g("docs", "v1", lambda s: s.documents().get(documentId=args["doc_id"]).execute())
        start = max(_doc_end_index(d) - 1, 1)  # insert before the final newline
        text, styles = md_to_docs_requests(args["content_md"], start_index=start)
        reqs = [{"insertText": {"location": {"index": start}, "text": text}}] + styles
        await self.g("docs", "v1", lambda s: s.documents().batchUpdate(documentId=args["doc_id"], body={"requests": reqs}).execute())
        return self.applied_result({"doc_id": args["doc_id"], "range": [start, start + len(text)]}, args, ctx,
                                   summary=self.summarize(args), target={"kind": "doc", "id": args["doc_id"]},
                                   compensation={"tool": "docs.revert", "args": {"doc_id": args["doc_id"], "start": start, "end": start + len(text)}})

    async def _compensate(self, effect, ctx):
        a = (effect.compensation or {}).get("args", {})
        reqs = [{"deleteContentRange": {"range": {"startIndex": a["start"], "endIndex": a["end"]}}}]
        await self.g("docs", "v1", lambda s: s.documents().batchUpdate(documentId=a["doc_id"], body={"requests": reqs}).execute())
        return ToolResult(ok=True, output={"reverted": a["doc_id"]})


# ---------------------------------------------------------------------------
# Sheets
# ---------------------------------------------------------------------------


class SheetsRead(_GoogleTool):
    spec = _spec("sheets.read", "sheets", "Read Google Sheet", "Read a range (A1 notation; default first sheet) from a Google Sheet.",
                 EffectClass.READ, obj_schema({"sheet_id": STR, "range": STR}, ["sheet_id"]), output_trust=Trust.UNTRUSTED)

    async def _execute(self, args, ctx):
        rng = args.get("range")
        if not rng:
            meta = await self.g("sheets", "v4", lambda s: s.spreadsheets().get(
                spreadsheetId=args["sheet_id"], fields="sheets.properties.title").execute())
            rng = (meta.get("sheets") or [{"properties": {"title": "Sheet1"}}])[0]["properties"]["title"]
        r = await self.g("sheets", "v4", lambda s: s.spreadsheets().values().get(spreadsheetId=args["sheet_id"], range=rng).execute())
        return ToolResult(ok=True, output={"range": r.get("range", rng), "rows": r.get("values", [])})


_A1 = re.compile(r"^(?:'?(?P<sheet>.+?)'?!)?[A-Z]+(?P<r1>\d+):[A-Z]+(?P<r2>\d+)$")


class SheetsAppendRows(_GoogleTool):
    spec = _spec("sheets.append_rows", "sheets", "Append rows to Google Sheet", "Append rows to the end of a sheet's table.",
                 EffectClass.WRITE_REVERSIBLE,
                 obj_schema({"sheet_id": STR, "rows": {"type": "array", "items": {"type": "array"}}}, ["sheet_id", "rows"]), compensable=True)

    def summarize(self, a):
        return f"Append {len(a['rows'])} row(s) to sheet {a['sheet_id']}"

    async def _preview(self, args, ctx):
        await self.g("sheets", "v4", lambda s: s.spreadsheets().get(spreadsheetId=args["sheet_id"], fields="spreadsheetId").execute())
        return self.simulated_result({}, self.summarize(args), preview={"sheet_id": args["sheet_id"], "rows": args["rows"]}, args=args)

    async def _execute(self, args, ctx):
        r = await self.g("sheets", "v4", lambda s: s.spreadsheets().values().append(
            spreadsheetId=args["sheet_id"], range="A1", valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
            body={"values": args["rows"]}).execute())
        rng = (r.get("updates") or {}).get("updatedRange", "")
        return self.applied_result({"updated_range": rng, "rows_appended": len(args["rows"])}, args, ctx,
                                   summary=self.summarize(args), target={"kind": "sheet_rows", "id": args["sheet_id"], "range": rng},
                                   compensation={"tool": "sheets.delete_rows", "args": {"sheet_id": args["sheet_id"], "range": rng}})

    async def _compensate(self, effect, ctx):
        a = (effect.compensation or {}).get("args", {})
        m = _A1.match(a.get("range", ""))
        if not m:
            raise ConnectorError(ErrorKind.PRECONDITION, "cannot determine appended range to delete")
        meta = await self.g("sheets", "v4", lambda s: s.spreadsheets().get(
            spreadsheetId=a["sheet_id"], fields="sheets.properties(sheetId,title)").execute())
        sheets = meta.get("sheets", [])
        title = m.group("sheet")
        sid = next((s["properties"]["sheetId"] for s in sheets if s["properties"]["title"] == title), None)
        if sid is None:
            raise ConnectorError(ErrorKind.NOT_FOUND, f"sheet tab {title!r} not found")
        req = {"deleteDimension": {"range": {"sheetId": sid, "dimension": "ROWS",
                                             "startIndex": int(m.group("r1")) - 1, "endIndex": int(m.group("r2"))}}}
        await self.g("sheets", "v4", lambda s: s.spreadsheets().batchUpdate(spreadsheetId=a["sheet_id"], body={"requests": [req]}).execute())
        return ToolResult(ok=True, output={"deleted_rows": a["range"]})


GOOGLE_TOOL_CLASSES = [GmailSearch, GmailRead, GmailDraft, GmailSend, CalendarListEvents, CalendarFindFreeSlots,
                       CalendarCreateEvent, CalendarDeleteEvent, DocsCreate, DocsRead, DocsSearch, DocsAppend,
                       SheetsRead, SheetsAppendRows]


def google_tools(workspace_id: str) -> dict[str, Any]:
    return {cls.spec.name: cls(workspace_id) for cls in GOOGLE_TOOL_CLASSES}

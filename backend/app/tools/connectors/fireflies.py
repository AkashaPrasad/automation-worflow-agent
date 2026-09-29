"""Fireflies.ai connector (GraphQL). Free plan is rate limited (~50 calls/day), so results are cached in-process for 10 min."""
from __future__ import annotations

import time
from typing import Any

import httpx

from app.config import get_settings
from app.core.models import EffectClass, ErrorKind, ToolResult, Trust

from ._base import STR, ConnectorError, ConnectorTool, error_from_status, obj_schema
from .google import _spec

URL = "https://api.fireflies.ai/graphql"
CACHE_TTL_S = 600

transport: httpx.AsyncBaseTransport | None = None  # tests
_cache: dict[str, tuple[float, Any]] = {}

LIST_Q = """query Transcripts($fromDate: DateTime, $limit: Int) {
  transcripts(fromDate: $fromDate, limit: $limit) { id title date duration participants organizer_email }
}"""
GET_Q = """query Transcript($id: String!) {
  transcript(id: $id) {
    id title date duration participants
    sentences { speaker_name text }
    summary { overview action_items keywords short_summary }
  }
}"""


def fireflies_configured() -> bool:
    return bool(get_settings().fireflies_api_key)


async def gql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    key = get_settings().fireflies_api_key
    ck = repr((query, sorted(variables.items())))
    hit = _cache.get(ck)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_S:
        return hit[1]
    async with httpx.AsyncClient(transport=transport, timeout=30) as http:
        r = await http.post(URL, json={"query": query, "variables": variables}, headers={"Authorization": f"Bearer {key}"})
    if r.status_code != 200:
        raise error_from_status(r.status_code, r.text[:200])
    body = r.json()
    if errs := body.get("errors"):
        e = errs[0]
        code = str((e.get("extensions") or {}).get("code", "")) + " " + str(e.get("message", ""))
        low = code.lower()
        if "too_many_requests" in low or "rate" in low:
            raise ConnectorError(ErrorKind.TRANSIENT, "fireflies rate limit reached", retryable=True)
        if "auth" in low or "api key" in low or "unauthenticated" in low:
            raise ConnectorError(ErrorKind.AUTH, code[:200])
        if "not found" in low or "object_not_found" in low:
            raise ConnectorError(ErrorKind.NOT_FOUND, code[:200])
        raise ConnectorError(ErrorKind.INVALID_ARGS, code[:200])
    _cache[ck] = (time.monotonic(), body.get("data") or {})
    return _cache[ck][1]


def clear_cache() -> None:
    _cache.clear()


def _date_iso(v: Any) -> str | None:
    if isinstance(v, (int, float)):  # Fireflies returns epoch milliseconds
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(v / 1000))
    return v


class MeetingsList(ConnectorTool):
    spec = _spec("meetings.list", "meetings", "List meetings", "List recent recorded meetings (Fireflies): id, title, date, participants, duration.",
                 EffectClass.READ, obj_schema({"since": STR}), output_trust=Trust.UNTRUSTED)

    async def _execute(self, args, ctx):
        v: dict[str, Any] = {"limit": 25}
        if args.get("since"):
            v["fromDate"] = args["since"]
        data = await gql(LIST_Q, v)
        ms = [{"id": t["id"], "title": t.get("title"), "date": _date_iso(t.get("date")), "duration_min": round(t.get("duration") or 0),
               "participants": t.get("participants") or []} for t in data.get("transcripts") or []]
        return ToolResult(ok=True, output={"meetings": ms, "count": len(ms)})


class MeetingsGetTranscript(ConnectorTool):
    spec = _spec("meetings.get_transcript", "meetings", "Get meeting transcript", "Get a meeting transcript (speaker-labelled) plus summary and action items.",
                 EffectClass.READ, obj_schema({"meeting_id": STR}, ["meeting_id"]), output_trust=Trust.UNTRUSTED)

    async def _execute(self, args, ctx):
        t = (await gql(GET_Q, {"id": args["meeting_id"]})).get("transcript")
        if not t:
            raise ConnectorError(ErrorKind.NOT_FOUND, f"meeting {args['meeting_id']} not found")
        sents = t.get("sentences") or []
        text = "\n".join(f"{s.get('speaker_name') or 'Speaker'}: {s.get('text', '')}" for s in sents)
        return ToolResult(ok=True, output={
            "id": t["id"], "title": t.get("title"), "date": _date_iso(t.get("date")), "participants": t.get("participants") or [],
            "transcript": text[:60000], "summary": t.get("summary") or {}})


def fireflies_tools() -> dict[str, Any]:
    return {c.spec.name: c() for c in (MeetingsList, MeetingsGetTranscript)}

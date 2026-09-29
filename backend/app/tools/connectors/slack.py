"""Slack connector (bot token, slack_sdk AsyncWebClient)."""
from __future__ import annotations

import re
from typing import Any

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from app.config import get_settings
from app.core.models import EffectClass, ErrorKind, ToolResult, Trust

from ._base import STR, ConnectorError, ConnectorTool, obj_schema
from .google import _spec

EVENT_TYPE = "adjutant_action"
_ID_RE = re.compile(r"^[CGD][A-Z0-9]{8,}$")

# Replaceable in tests: () -> client-like object
client_factory = lambda: AsyncWebClient(token=get_settings().slack_bot_token)

_AUTH = {"not_authed", "invalid_auth", "token_revoked", "token_expired", "account_inactive"}
_PERM = {"missing_scope", "not_in_channel", "is_archived", "restricted_action", "cant_delete_message", "channel_not_found_private"}
_ARGS = {"msg_too_long", "no_text", "invalid_arguments", "invalid_blocks", "too_many_attachments"}


def slack_configured() -> bool:
    return bool(get_settings().slack_bot_token)


def map_slack_error(e: SlackApiError) -> ConnectorError:
    code = (e.response or {}).get("error", "") if e.response is not None else ""
    status = getattr(e.response, "status_code", 0) or 0
    if status == 429 or code in ("ratelimited", "rate_limited"):
        return ConnectorError(ErrorKind.TRANSIENT, "slack rate limited", retryable=True)
    if code in _AUTH:
        return ConnectorError(ErrorKind.AUTH, f"slack: {code}")
    if code in _PERM:
        return ConnectorError(ErrorKind.PERMISSION, f"slack: {code}")
    if code in ("channel_not_found", "message_not_found", "user_not_found"):
        return ConnectorError(ErrorKind.NOT_FOUND, f"slack: {code}")
    if code in _ARGS:
        return ConnectorError(ErrorKind.INVALID_ARGS, f"slack: {code}")
    if status >= 500 or code in ("internal_error", "fatal_error", "service_unavailable", "request_timeout"):
        return ConnectorError(ErrorKind.TRANSIENT, f"slack: {code or status}", retryable=True)
    return ConnectorError(ErrorKind.UNKNOWN, f"slack: {code or status}")


async def _s(coro):
    try:
        return await coro
    except SlackApiError as e:
        raise map_slack_error(e) from None


async def resolve_channel(c: Any, channel: str) -> str:
    ch = channel.strip()
    if _ID_RE.match(ch):
        return ch
    name = ch.lstrip("#").lower()
    cursor = None
    for _ in range(20):
        kw: dict[str, Any] = {"types": "public_channel,private_channel", "exclude_archived": True, "limit": 200}
        if cursor:
            kw["cursor"] = cursor
        r = await _s(c.conversations_list(**kw))
        for x in r.get("channels", []):
            if x.get("name", "").lower() == name:
                return x["id"]
        cursor = (r.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    raise ConnectorError(ErrorKind.NOT_FOUND, f"slack channel {channel!r} not found (is the bot invited?)")


class SlackReadChannel(ConnectorTool):
    spec = _spec("slack.read_channel", "slack", "Read Slack channel", "Read recent messages from a Slack channel ('#name' or id).",
                 EffectClass.READ, obj_schema({"channel": STR, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, ["channel"]),
                 output_trust=Trust.UNTRUSTED)

    async def _execute(self, args, ctx):
        c = client_factory()
        cid = await resolve_channel(c, args["channel"])
        r = await _s(c.conversations_history(channel=cid, limit=int(args.get("limit", 20))))
        names: dict[str, str] = {}

        async def who(uid: str) -> str:
            if uid not in names:
                try:
                    u = await c.users_info(user=uid)
                    p = u["user"]
                    names[uid] = p.get("real_name") or p.get("name") or uid
                except Exception:  # noqa: BLE001 - users:read scope is optional
                    names[uid] = uid
            return names[uid]

        msgs = [{"ts": m.get("ts"), "user": await who(m["user"]) if m.get("user") else m.get("username", "bot"),
                 "text": m.get("text", ""), "thread_ts": m.get("thread_ts")} for m in r.get("messages", [])]
        return ToolResult(ok=True, output={"channel": args["channel"], "channel_id": cid, "messages": msgs})


async def _find_by_key(c: Any, cid: str, key: str) -> dict[str, Any] | None:
    r = await _s(c.conversations_history(channel=cid, limit=100, include_all_metadata=True))
    for m in r.get("messages", []):
        md = m.get("metadata") or {}
        if md.get("event_type") == EVENT_TYPE and (md.get("event_payload") or {}).get("key") == key:
            return m
    return None


class SlackPostMessage(ConnectorTool):
    spec = _spec("slack.post_message", "slack", "Post Slack message", "Post a message to a Slack channel as the bot. Visible to the channel; cannot be unseen.",
                 EffectClass.COMMUNICATE, obj_schema({"channel": STR, "text": STR}, ["channel", "text"]), compensable=True, idempotent=True)

    def summarize(self, a):
        return f"Slack {a['channel']}: {a['text'][:80]}"

    async def _preview(self, args, ctx):
        c = client_factory()
        cid = await resolve_channel(c, args["channel"])
        return self.simulated_result({"channel_id": cid}, self.summarize(args), preview={"channel": args["channel"], "text": args["text"]}, args=args)

    async def _execute(self, args, ctx):
        c = client_factory()
        cid = await resolve_channel(c, args["channel"])
        key = ctx.idempotency_key
        dedup = False
        m = await _find_by_key(c, cid, key) if key else None
        if m:
            ts, dedup = m["ts"], True
        else:
            kw: dict[str, Any] = {"channel": cid, "text": args["text"]}
            if key:
                kw["metadata"] = {"event_type": EVENT_TYPE, "event_payload": {"key": key}}
            r = await _s(c.chat_postMessage(**kw))
            ts = r["ts"]
            cid = r.get("channel", cid)
        return self.applied_result({"channel_id": cid, "ts": ts, "deduplicated": dedup}, args, ctx, summary=self.summarize(args),
                                   target={"kind": "slack_message", "id": ts, "channel_id": cid},
                                   preview={"channel": args["channel"], "text": args["text"]},
                                   compensation={"tool": "slack.delete_message", "args": {"channel_id": cid, "ts": ts}})

    async def _compensate(self, effect, ctx):
        a = (effect.compensation or {}).get("args", {})
        try:
            await _s(client_factory().chat_delete(channel=a["channel_id"], ts=a["ts"]))
        except ConnectorError as e:
            if e.kind != ErrorKind.NOT_FOUND:
                raise
        return ToolResult(ok=True, output={"deleted": a["ts"], "note": "audience may already have seen it"})

    async def reconcile(self, effect, ctx):
        if not effect.idempotency_key:
            return None
        try:
            c = client_factory()
            cid = await resolve_channel(c, effect.preview.get("channel") or effect.target.get("channel_id", ""))
            return (await _find_by_key(c, cid, effect.idempotency_key)) is not None
        except Exception:  # noqa: BLE001
            return None


def slack_tools() -> dict[str, Any]:
    return {c.spec.name: c() for c in (SlackReadChannel, SlackPostMessage)}

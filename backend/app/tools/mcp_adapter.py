"""Mount remote MCP servers (streamable HTTP) as Adjutant tools.

Tools are exposed as `mcp:<server>.<tool>` with app `mcp:<server>`. Effect classes come from MCP
ToolAnnotations, which are UNTRUSTED hints: missing/contradictory annotations trigger
`judge.infer_tool_effect`, and the more conservative of (annotation, inference) wins.
Output of every MCP tool is UNTRUSTED. Each call opens a short-lived session (no connection state to leak/expire).
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from typing import Any

from mcp import Client
from mcp.shared.exceptions import MCPError

from app.config import get_settings
from app.core.interfaces import Tool
from app.core.models import EffectClass, ErrorKind, ToolResult, ToolSpec, Trust

from .connectors._base import ConnectorError, ConnectorTool, error_from_status

log = logging.getLogger("adjutant.mcp")

CALL_TIMEOUT_S = 60.0

# Replaceable in tests: url -> async context manager yielding a connected client (default: mcp.Client over streamable HTTP).
client_factory: Callable[[str], Any] = lambda url: Client(url)

_TOOLS: list[Tool] = []

_DESTRUCTIVE = re.compile(r"(delete|remove|drop|destroy|purge|wipe|erase|truncate|revoke|cancel|kill|terminate|reset|overwrite)", re.IGNORECASE)
_MESSAGING = re.compile(r"(send|post|publish|email|mail|message|notify|notification|tweet|sms|invite|reply|broadcast|comment|share|dm\b)", re.IGNORECASE)
_MUTATING = re.compile(r"(create|update|write|set|add|insert|put|patch|modify|edit|upload|commit|push|merge|deploy|pay|transfer|book)", re.IGNORECASE)

_RANK = {EffectClass.READ: 0, EffectClass.WRITE_REVERSIBLE: 1, EffectClass.WRITE_IRREVERSIBLE: 2, EffectClass.COMMUNICATE: 3}


def more_conservative(a: EffectClass, b: EffectClass) -> EffectClass:
    return a if _RANK[a] >= _RANK[b] else b


def _ann_dict(ann: Any) -> dict[str, Any]:
    if ann is None:
        return {}
    if isinstance(ann, dict):
        d = ann
    else:
        d = ann.model_dump() if hasattr(ann, "model_dump") else dict(vars(ann))
    keys = {"read_only_hint": "readOnlyHint", "destructive_hint": "destructiveHint", "idempotent_hint": "idempotentHint",
            "open_world_hint": "openWorldHint", "title": "title"}
    out = {}
    for k, v in d.items():
        out[keys.get(k, k)] = v
    return {k: v for k, v in out.items() if v is not None}


def effect_from_annotations(name: str, ann: dict[str, Any]) -> tuple[EffectClass | None, bool]:
    """Returns (effect from hints or None if hints are absent, contradictory?)."""
    hints = {k: ann[k] for k in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint") if k in ann}
    if not hints:
        return None, True
    bare = name.split(".")[-1]
    # verb-first names ("delete_x", "sendEmail") are what contradict a benign hint; "get_message" does not
    risky_name = bool(_DESTRUCTIVE.match(bare) or _MESSAGING.match(bare) or _MUTATING.match(bare))
    if ann.get("readOnlyHint") is True:
        return EffectClass.READ, risky_name or ann.get("destructiveHint") is True
    # readOnlyHint false/absent: per MCP spec destructiveHint defaults to true
    if ann.get("readOnlyHint") is None and len(hints) == 1 and "openWorldHint" in hints:
        return None, True  # only openWorld given: says nothing about mutation
    messaging = bool(_MESSAGING.search(bare)) and ann.get("openWorldHint") is not False
    if messaging and ann.get("openWorldHint") is True:
        return EffectClass.COMMUNICATE, False
    if ann.get("destructiveHint") is False and ann.get("idempotentHint") is True:
        return EffectClass.WRITE_REVERSIBLE, bool(_DESTRUCTIVE.match(bare) or messaging)
    return EffectClass.WRITE_IRREVERSIBLE, False


def _fallback_effect(name: str) -> EffectClass:
    bare = name.split(".")[-1]
    return EffectClass.COMMUNICATE if _MESSAGING.search(bare) else EffectClass.WRITE_IRREVERSIBLE


def _name_effect(name: str) -> EffectClass:
    bare = name.split(".")[-1]
    if _MESSAGING.match(bare):
        return EffectClass.COMMUNICATE
    if _DESTRUCTIVE.match(bare):
        return EffectClass.WRITE_IRREVERSIBLE
    if _MUTATING.match(bare):
        return EffectClass.WRITE_REVERSIBLE
    return EffectClass.READ


async def resolve_effect(name: str, description: str, ann: Any, judge: Any | None) -> tuple[EffectClass, bool]:
    """Returns (effect, effect_inferred)."""
    d = _ann_dict(ann)
    from_ann, suspicious = effect_from_annotations(name, d)
    if from_ann is not None and not suspicious:
        return from_ann, False
    inferred: EffectClass | None = None
    conf = 0.0
    if judge is not None:
        try:
            val, conf = await judge.infer_tool_effect(name, description, d)
            inferred = EffectClass(val)
        except Exception as e:  # noqa: BLE001 - judge failure must fail closed, never open
            log.warning("infer_tool_effect failed for %s: %s", name, e)
    if inferred is None:
        inferred = _fallback_effect(name)
    if from_ann is None:
        # nothing trustworthy to compare: low-confidence inference is bumped to at least irreversible
        return (inferred if conf >= 0.5 or inferred == EffectClass.COMMUNICATE
                else more_conservative(inferred, EffectClass.WRITE_IRREVERSIBLE)), True
    # contradictory hints: the verb in the name is a third signal; never let two "read" votes hide a delete_/send_ tool
    return more_conservative(more_conservative(from_ann, inferred), _name_effect(name)), True


def _map_call_exception(exc: Exception) -> ConnectorError:
    if isinstance(exc, ConnectorError):
        return exc
    if isinstance(exc, MCPError):
        code = getattr(getattr(exc, "error", None), "code", None)
        if code in (-32602, -32601):
            return ConnectorError(ErrorKind.INVALID_ARGS, f"MCP error {code}: {exc}"[:300])
        return ConnectorError(ErrorKind.UNKNOWN, f"MCP error: {exc}"[:300])
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        return ConnectorError(ErrorKind.TRANSIENT, f"MCP server unreachable: {type(exc).__name__}", retryable=True)
    text = str(exc)
    m = re.search(r"\b(401|403|404|429|5\d\d)\b", text)
    if m:
        return error_from_status(int(m.group(1)), text)
    return ConnectorError(ErrorKind.TRANSIENT if "connect" in text.lower() else ErrorKind.UNKNOWN, f"{type(exc).__name__}: {text}"[:300])


def _content_text(result: Any) -> str:
    parts = []
    for c in getattr(result, "content", None) or []:
        t = getattr(c, "text", None)
        parts.append(t if t is not None else f"[{getattr(c, 'type', 'content')}]")
    return "\n".join(parts)


class McpTool(ConnectorTool):
    def __init__(self, server: str, url: str, remote_name: str, spec: ToolSpec):
        self.server, self.url, self.remote_name, self.spec = server, url, remote_name, spec

    def summarize(self, args):
        brief = ", ".join(f"{k}={str(v)[:40]}" for k, v in list(args.items())[:4])
        return f"{self.spec.name}({brief})"

    def validate(self, args):
        try:
            super().validate(args)
        except ConnectorError as e:
            if "SchemaError" in e.message:
                return
            raise

    async def _execute(self, args, ctx):
        async def call():
            async with client_factory(self.url) as c:
                return await c.call_tool(self.remote_name, args)
        try:
            r = await asyncio.wait_for(call(), CALL_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001
            raise _map_call_exception(e) from None
        text = _content_text(r)
        if getattr(r, "is_error", False):
            return ToolResult(ok=False, error=ConnectorError(ErrorKind.UNKNOWN, text[:500] or "MCP tool error").to_error())
        out: dict[str, Any] = {"text": text[:30000]}
        if getattr(r, "structured_content", None) is not None:
            out["structured"] = r.structured_content
        if not self.is_write:
            return ToolResult(ok=True, output=out)
        return self.applied_result(out, args, ctx, summary=self.summarize(args), target={"kind": "mcp_call", "server": self.server, "tool": self.remote_name})

    async def _preview(self, args, ctx):
        note = "MCP tools cannot be dry-run; preview shows the arguments"
        return self.simulated_result({"note": note}, self.summarize(args), preview={"args": args, "note": note}, args=args, ctx=ctx)


def parse_servers(raw: str) -> dict[str, str]:
    out = {}
    for part in raw.split(","):
        name, sep, url = part.strip().partition("=")
        if sep and name.strip() and url.strip():
            out[name.strip()] = url.strip()
    return out


async def _load_one(server: str, url: str, judge: Any | None) -> list[Tool]:
    tools: list[Any] = []
    async with client_factory(url) as c:
        cursor = None
        remote = []
        for _ in range(20):
            r = await c.list_tools(cursor=cursor) if cursor else await c.list_tools()
            remote += list(r.tools)
            cursor = getattr(r, "next_cursor", None)
            if not cursor:
                break
    for t in remote:
        ann = _ann_dict(t.annotations)
        desc = t.description or t.title or ""
        effect, inferred = await resolve_effect(f"{server}.{t.name}", desc, t.annotations, judge)
        spec = ToolSpec(
            name=f"mcp:{server}.{t.name}", app=f"mcp:{server}", title=t.title or ann.get("title") or t.name,
            description=f"[MCP server '{server}'] {desc}"[:1500], effect=effect,
            input_schema=t.input_schema or {"type": "object", "properties": {}},
            output_trust=Trust.UNTRUSTED, compensable=False, idempotent=bool(ann.get("idempotentHint")),
            source="mcp", effect_inferred=inferred,
        )
        tools.append(McpTool(server, url, t.name, spec))
    return tools


async def load_mcp_servers(judge: Any | None = None) -> None:
    """Connect to every server in settings.mcp_servers, list tools, build wrappers. Never raises."""
    global _TOOLS
    loaded: list[Tool] = []
    for server, url in parse_servers(get_settings().mcp_servers).items():
        try:
            loaded += await asyncio.wait_for(_load_one(server, url, judge), 30)
            log.info("mounted MCP server %s", server)
        except Exception as e:  # noqa: BLE001
            log.warning("could not mount MCP server %s: %s", server, e)
    _TOOLS = loaded


def mcp_tools() -> list[Tool]:
    return list(_TOOLS)

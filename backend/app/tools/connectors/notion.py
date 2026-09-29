"""Notion connector (internal integration token, notion-client AsyncClient)."""
from __future__ import annotations

import re
from typing import Any

import httpx
from notion_client import AsyncClient
from notion_client.errors import APIResponseError

from app.config import get_settings
from app.core.models import EffectClass, ErrorKind, ToolResult, Trust

from ._base import STR, ConnectorError, ConnectorTool, error_from_status, obj_schema
from .google import _spec

NOTION_VERSION = "2022-06-28"  # pinned: stable page/search/blocks semantics (`archived`, filter value "page")
MARKER_PREFIX = "adjutant-key:"

# Replaceable in tests: httpx transport for the underlying client.
transport: httpx.AsyncBaseTransport | None = None


def notion_configured() -> bool:
    return bool(get_settings().notion_token)


def _client() -> AsyncClient:
    s = get_settings()
    http = httpx.AsyncClient(transport=transport, timeout=30) if transport else None
    return AsyncClient(auth=s.notion_token, notion_version=NOTION_VERSION, client=http, retry=False) if http else \
        AsyncClient(auth=s.notion_token, notion_version=NOTION_VERSION, retry=False)


async def _api(coro):
    try:
        return await coro
    except APIResponseError as e:
        raise error_from_status(int(e.status), getattr(e, "code", "") or str(e)) from None


def _rt(text: str) -> list[dict[str, Any]]:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    return [{"type": "text", "text": {"content": text[i:i + 2000]}} for i in range(0, max(len(text), 1), 2000)]


def md_to_blocks(md: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for line in md.splitlines():
        if not line.strip():
            continue
        if m := re.match(r"^(#{1,6})\s+(.*)$", line):
            lvl = min(len(m.group(1)), 3)
            t = f"heading_{lvl}"
            blocks.append({"object": "block", "type": t, t: {"rich_text": _rt(m.group(2))}})
        elif m := re.match(r"^\s*[-*]\s+\[([ xX])\]\s+(.*)$", line):
            blocks.append({"object": "block", "type": "to_do", "to_do": {"rich_text": _rt(m.group(2)), "checked": m.group(1) != " "}})
        elif m := re.match(r"^\s*[-*]\s+(.*)$", line):
            blocks.append({"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": _rt(m.group(1))}})
        elif m := re.match(r"^\s*\d+[.)]\s+(.*)$", line):
            blocks.append({"object": "block", "type": "numbered_list_item", "numbered_list_item": {"rich_text": _rt(m.group(1))}})
        else:
            blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rt(line)}})
    return blocks


def _plain(rt: list[dict[str, Any]]) -> str:
    return "".join(x.get("plain_text", "") for x in rt or [])


def blocks_to_text(blocks: list[dict[str, Any]]) -> str:
    out = []
    for b in blocks:
        t = b.get("type", "")
        body = b.get(t) or {}
        text = _plain(body.get("rich_text", []))
        if t.startswith("heading_"):
            out.append("#" * int(t[-1]) + " " + text)
        elif t == "bulleted_list_item":
            out.append("- " + text)
        elif t == "numbered_list_item":
            out.append("1. " + text)
        elif t == "to_do":
            out.append(f"- [{'x' if body.get('checked') else ' '}] {text}")
        elif t == "child_page":
            out.append(f"[child page] {body.get('title', '')}")
        elif text:
            out.append(text)
    return "\n".join(x for x in out if not x.startswith(MARKER_PREFIX))


def page_title(page: dict[str, Any]) -> str:
    for p in (page.get("properties") or {}).values():
        if p.get("type") == "title":
            return _plain(p.get("title", []))
    return ""


class NotionSearch(ConnectorTool):
    spec = _spec("notion.search", "notion", "Search Notion", "Search Notion pages shared with the integration by title.", EffectClass.READ,
                 obj_schema({"query": STR}, ["query"]), output_trust=Trust.UNTRUSTED)

    async def _execute(self, args, ctx):
        r = await _api(_client().search(query=args["query"], filter={"property": "object", "value": "page"}, page_size=20))
        pages = [{"id": p["id"], "title": page_title(p), "url": p.get("url"), "last_edited": p.get("last_edited_time")} for p in r.get("results", [])]
        return ToolResult(ok=True, output={"pages": pages, "count": len(pages)})


class NotionReadPage(ConnectorTool):
    spec = _spec("notion.read_page", "notion", "Read Notion page", "Read a Notion page's title and content as markdown-ish text.",
                 EffectClass.READ, obj_schema({"page_id": STR}, ["page_id"]), output_trust=Trust.UNTRUSTED)

    async def _execute(self, args, ctx):
        c = _client()
        page = await _api(c.pages.retrieve(page_id=args["page_id"]))
        blocks, cursor = [], None
        for _ in range(10):  # up to 1000 top-level blocks
            kw: dict[str, Any] = {"block_id": args["page_id"], "page_size": 100}
            if cursor:
                kw["start_cursor"] = cursor
            r = await _api(c.blocks.children.list(**kw))
            blocks += r.get("results", [])
            if not r.get("has_more"):
                break
            cursor = r.get("next_cursor")
        return ToolResult(ok=True, output={"page_id": page["id"], "title": page_title(page), "url": page.get("url"),
                                           "text": blocks_to_text(blocks)[:30000]})


async def _find_by_key(c: AsyncClient, key: str, parent: str) -> dict[str, Any] | None:
    r = await _api(c.search(filter={"property": "object", "value": "page"},
                            sort={"direction": "descending", "timestamp": "last_edited_time"}, page_size=20))
    for p in r.get("results", []):
        par = p.get("parent") or {}
        if par.get("page_id", "").replace("-", "") != parent.replace("-", ""):
            continue
        cursor = None
        for _ in range(5):
            kw: dict[str, Any] = {"block_id": p["id"], "page_size": 100}
            if cursor:
                kw["start_cursor"] = cursor
            bl = await _api(c.blocks.children.list(**kw))
            for b in bl.get("results", []):
                if b.get("type") == "paragraph" and _plain(b["paragraph"].get("rich_text", [])) == MARKER_PREFIX + key:
                    return p
            if not bl.get("has_more"):
                break
            cursor = bl.get("next_cursor")
    return None


class NotionCreatePage(ConnectorTool):
    spec = _spec("notion.create_page", "notion", "Create Notion page", "Create a Notion page under a parent page from markdown-ish content.",
                 EffectClass.WRITE_REVERSIBLE, obj_schema({"title": STR, "content_md": STR, "parent": STR}, ["title", "content_md"]),
                 compensable=True, idempotent=True)

    def summarize(self, a):
        return f"Create Notion page '{a['title']}'"

    def _parent(self, args) -> str:
        p = args.get("parent") or get_settings().notion_parent_page_id
        if not p:
            raise ConnectorError(ErrorKind.INVALID_ARGS, "no parent page: pass `parent` or set NOTION_PARENT_PAGE_ID")
        return p

    async def _preview(self, args, ctx):
        parent = self._parent(args)
        await _api(_client().pages.retrieve(page_id=parent))  # NOT_FOUND / PERMISSION if not shared with the integration
        prev = {"title": args["title"], "parent": parent, "blocks": len(md_to_blocks(args["content_md"])), "content_md": args["content_md"]}
        return self.simulated_result({}, self.summarize(args), preview=prev, args=args)

    async def _execute(self, args, ctx):
        c = _client()
        parent = self._parent(args)
        key = ctx.idempotency_key
        if key and (prior := await _find_by_key(c, key, parent)):
            return self.applied_result({"page_id": prior["id"], "url": prior.get("url"), "deduplicated": True}, args, ctx,
                                       summary=self.summarize(args), target={"kind": "notion_page", "id": prior["id"]},
                                       compensation={"tool": "notion.archive_page", "args": {"page_id": prior["id"]}})
        blocks = md_to_blocks(args["content_md"])
        if key:
            blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": [
                {"type": "text", "text": {"content": MARKER_PREFIX + key}, "annotations": {"color": "gray", "code": True}}]}})
        page = await _api(c.pages.create(parent={"page_id": parent}, properties={"title": {"title": _rt(args["title"])}},
                                         children=blocks[:100]))
        for i in range(100, len(blocks), 100):
            await _api(c.blocks.children.append(block_id=page["id"], children=blocks[i:i + 100]))
        return self.applied_result({"page_id": page["id"], "url": page.get("url")}, args, ctx, summary=self.summarize(args),
                                   target={"kind": "notion_page", "id": page["id"]},
                                   compensation={"tool": "notion.archive_page", "args": {"page_id": page["id"]}})

    async def _compensate(self, effect, ctx):
        pid = (effect.compensation or {}).get("args", {}).get("page_id") or effect.target.get("id")
        await _api(_client().pages.update(page_id=pid, archived=True))
        return ToolResult(ok=True, output={"archived": pid})

    async def reconcile(self, effect, ctx):
        if not effect.idempotency_key:
            return None
        try:
            parent = self._parent(effect.preview if effect.preview.get("parent") else {})
            return (await _find_by_key(_client(), effect.idempotency_key, parent)) is not None
        except Exception:  # noqa: BLE001
            return None


def notion_tools() -> dict[str, Any]:
    return {c.spec.name: c() for c in (NotionSearch, NotionReadPage, NotionCreatePage)}

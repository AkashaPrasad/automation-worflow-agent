"""Notion / Slack / Fireflies connector tests (mocked HTTP / fake clients)."""
from __future__ import annotations

import json

import httpx
import pytest
from slack_sdk.errors import SlackApiError

from app.config import get_settings
from app.core.models import ErrorKind, ToolContext
from app.tools.connectors import fireflies, notion, slack

CTX = ToolContext(run_id="r", node_id="a", workspace_id="ws_test_1", idempotency_key="k-1")
NOKEY = ToolContext(run_id="r", node_id="a", workspace_id="ws_test_1", idempotency_key="")
PARENT = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def cfg(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "notion_token", "secret_x")
    monkeypatch.setattr(s, "notion_parent_page_id", PARENT)
    monkeypatch.setattr(s, "slack_bot_token", "xoxb-x")
    monkeypatch.setattr(s, "fireflies_api_key", "ff-key")
    fireflies.clear_cache()


# ---------------- Notion ----------------

def rt(t):
    return [{"plain_text": t}]


def notion_transport(routes, log):
    def handler(req: httpx.Request):
        body = json.loads(req.content) if req.content else None
        log.append((req.method, req.url.path, body))
        assert req.headers["authorization"] == "Bearer secret_x"
        for (m, path), resp in routes.items():
            if req.method == m and req.url.path == path:
                return httpx.Response(200, json=resp)
        return httpx.Response(404, json={"object": "error", "status": 404, "code": "object_not_found", "message": "nf"})
    return httpx.MockTransport(handler)


def test_md_to_blocks_and_back():
    blocks = notion.md_to_blocks("# Title\n\ntext\n- item\n- [x] done\n- [ ] todo\n1. first")
    assert [b["type"] for b in blocks] == ["heading_1", "paragraph", "bulleted_list_item", "to_do", "to_do", "numbered_list_item"]
    assert blocks[3]["to_do"]["checked"] is True and blocks[4]["to_do"]["checked"] is False
    api = [{"type": b["type"], b["type"]: {**b[b["type"]], "rich_text": [{"plain_text": x["text"]["content"]} for x in b[b["type"]]["rich_text"]]}} for b in blocks]
    text = notion.blocks_to_text(api)
    assert "# Title" in text and "- [x] done" in text and "1. first" in text


async def test_notion_search_and_read(monkeypatch):
    log = []
    page = {"id": "p1", "url": "https://notion.so/p1", "properties": {"Name": {"type": "title", "title": rt("Projects")}}}
    monkeypatch.setattr(notion, "transport", notion_transport({
        ("POST", "/v1/search"): {"results": [page]},
        ("GET", "/v1/pages/p1"): page,
        ("GET", "/v1/blocks/p1/children"): {"results": [{"type": "paragraph", "paragraph": {"rich_text": rt("hello")}}], "has_more": False},
    }, log))
    r = await notion.NotionSearch().run({"query": "proj"}, NOKEY)
    assert r.ok and r.output["pages"][0]["title"] == "Projects"
    assert log[0][2]["filter"] == {"property": "object", "value": "page"}
    r = await notion.NotionReadPage().run({"page_id": "p1"}, NOKEY)
    assert r.output["title"] == "Projects" and r.output["text"] == "hello"


async def test_notion_error_mapping(monkeypatch):
    monkeypatch.setattr(notion, "transport", notion_transport({}, []))
    r = await notion.NotionReadPage().run({"page_id": "missing"}, NOKEY)
    assert not r.ok and r.error.kind == ErrorKind.NOT_FOUND
    monkeypatch.setattr(notion, "transport", httpx.MockTransport(lambda req: httpx.Response(401, json={"object": "error", "status": 401, "code": "unauthorized", "message": "x"})))
    r = await notion.NotionReadPage().run({"page_id": "p"}, NOKEY)
    assert r.error.kind == ErrorKind.AUTH


async def test_notion_create_page_with_marker_and_archive(monkeypatch):
    log = []
    monkeypatch.setattr(notion, "transport", notion_transport({
        ("POST", "/v1/search"): {"results": []},
        ("POST", "/v1/pages"): {"id": "new1", "url": "https://notion.so/new1"},
        ("PATCH", "/v1/pages/new1"): {"id": "new1", "archived": True},
    }, log))
    t = notion.NotionCreatePage()
    r = await t.run({"title": "Notes", "content_md": "# H\n- a"}, CTX)
    assert r.ok and r.effect.compensation == {"tool": "notion.archive_page", "args": {"page_id": "new1"}}
    create = next(b for m, p, b in log if p == "/v1/pages")
    assert create["parent"] == {"page_id": PARENT}
    assert create["children"][-1]["paragraph"]["rich_text"][0]["text"]["content"] == "adjutant-key:k-1"
    assert (await t.compensate(r.effect, CTX)).ok
    assert log[-1][2] == {"archived": True}


async def test_notion_create_dedupes_by_marker(monkeypatch):
    log = []
    existing = {"id": "old1", "url": "u", "parent": {"page_id": PARENT}}
    monkeypatch.setattr(notion, "transport", notion_transport({
        ("POST", "/v1/search"): {"results": [existing]},
        ("GET", "/v1/blocks/old1/children"): {"results": [{"type": "paragraph", "paragraph": {"rich_text": rt("adjutant-key:k-1")}}], "has_more": False},
    }, log))
    t = notion.NotionCreatePage()
    r = await t.run({"title": "Notes", "content_md": "x"}, CTX)
    assert r.output["deduplicated"] is True and not any(p == "/v1/pages" and m == "POST" for m, p, _ in log)
    assert await t.reconcile(r.effect, CTX) is True


async def test_notion_simulate_checks_parent_no_write(monkeypatch):
    log = []
    monkeypatch.setattr(notion, "transport", notion_transport({("GET", f"/v1/pages/{PARENT}"): {"id": "x"}}, log))
    r = await notion.NotionCreatePage().simulate({"title": "T", "content_md": "- a"}, CTX)
    assert r.ok and r.simulated and r.effect.status == "simulated" and r.effect.preview["blocks"] == 1
    assert [m for m, _, _ in log] == ["GET"]


async def test_notion_needs_parent(monkeypatch):
    monkeypatch.setattr(get_settings(), "notion_parent_page_id", "")
    r = await notion.NotionCreatePage().simulate({"title": "T", "content_md": "x"}, CTX)
    assert r.error.kind == ErrorKind.INVALID_ARGS


# ---------------- Slack ----------------

class FakeSlack:
    def __init__(self):
        self.calls = []
        self.history = []
        self.error = None

    async def conversations_list(self, **kw):
        self.calls.append(("list", kw))
        return {"channels": [{"id": "C0OPS0001", "name": "ops"}], "response_metadata": {}}

    async def conversations_history(self, **kw):
        self.calls.append(("history", kw))
        return {"messages": self.history}

    async def users_info(self, user):
        return {"user": {"real_name": "Dana"}}

    async def chat_postMessage(self, **kw):
        self.calls.append(("post", kw))
        if self.error:
            raise self.error
        return {"ts": "1700.1", "channel": kw["channel"]}

    async def chat_delete(self, **kw):
        self.calls.append(("delete", kw))
        return {"ok": True}


@pytest.fixture
def fake(monkeypatch):
    f = FakeSlack()
    monkeypatch.setattr(slack, "client_factory", lambda: f)
    return f


def slack_err(code, status=200):
    class R(dict):
        status_code = status
    return SlackApiError("x", R({"error": code}))


async def test_slack_read_resolves_name(fake):
    fake.history = [{"ts": "1", "user": "U1", "text": "hi"}]
    r = await slack.SlackReadChannel().run({"channel": "#ops", "limit": 5}, NOKEY)
    assert r.ok and r.output["channel_id"] == "C0OPS0001" and r.output["messages"][0]["user"] == "Dana"


async def test_slack_channel_not_found(fake):
    r = await slack.SlackReadChannel().run({"channel": "#nope"}, NOKEY)
    assert r.error.kind == ErrorKind.NOT_FOUND


async def test_slack_post_stores_key_in_metadata_and_deletes(fake):
    t = slack.SlackPostMessage()
    r = await t.run({"channel": "#ops", "text": "hello"}, CTX)
    post = next(kw for n, kw in fake.calls if n == "post")
    assert post["metadata"] == {"event_type": "adjutant_action", "event_payload": {"key": "k-1"}}
    assert r.effect.compensation["args"] == {"channel_id": "C0OPS0001", "ts": "1700.1"}
    assert (await t.compensate(r.effect, CTX)).ok and fake.calls[-1][0] == "delete"


async def test_slack_post_dedupes_and_reconciles(fake):
    fake.history = [{"ts": "55.5", "metadata": {"event_type": "adjutant_action", "event_payload": {"key": "k-1"}}}]
    t = slack.SlackPostMessage()
    r = await t.run({"channel": "#ops", "text": "hello"}, CTX)
    assert r.output["deduplicated"] and r.output["ts"] == "55.5" and not any(n == "post" for n, _ in fake.calls)
    assert await t.reconcile(r.effect, CTX) is True
    fake.history = []
    assert await t.reconcile(r.effect, CTX) is False


async def test_slack_simulate_no_post(fake):
    r = await slack.SlackPostMessage().simulate({"channel": "#ops", "text": "x"}, CTX)
    assert r.simulated and not any(n == "post" for n, _ in fake.calls)


@pytest.mark.parametrize("code,status,kind", [("invalid_auth", 200, ErrorKind.AUTH), ("not_in_channel", 200, ErrorKind.PERMISSION),
                                              ("channel_not_found", 200, ErrorKind.NOT_FOUND), ("msg_too_long", 200, ErrorKind.INVALID_ARGS),
                                              ("ratelimited", 429, ErrorKind.TRANSIENT), ("internal_error", 200, ErrorKind.TRANSIENT)])
async def test_slack_error_mapping(fake, code, status, kind):
    fake.error = slack_err(code, status)
    r = await slack.SlackPostMessage().run({"channel": "C0OPS0001", "text": "x"}, NOKEY)
    assert r.error.kind == kind and r.error.retryable == (kind == ErrorKind.TRANSIENT)


# ---------------- Fireflies ----------------

def ff_transport(log, payload):
    def handler(req: httpx.Request):
        assert req.headers["authorization"] == "Bearer ff-key"
        log.append(json.loads(req.content))
        return payload(req) if callable(payload) else httpx.Response(200, json=payload)
    return httpx.MockTransport(handler)


async def test_meetings_list_and_cache(monkeypatch):
    log = []
    monkeypatch.setattr(fireflies, "transport", ff_transport(log, {"data": {"transcripts": [
        {"id": "t1", "title": "QBR", "date": 1700000000000, "duration": 42.5, "participants": ["a@x.com"]}]}}))
    r = await fireflies.MeetingsList().run({"since": "2026-09-01T00:00:00Z"}, NOKEY)
    assert r.output["meetings"][0]["id"] == "t1" and r.output["meetings"][0]["date"].startswith("2023-")
    assert log[0]["variables"]["fromDate"] == "2026-09-01T00:00:00Z"
    await fireflies.MeetingsList().run({"since": "2026-09-01T00:00:00Z"}, NOKEY)
    assert len(log) == 1  # cached


async def test_cache_expires(monkeypatch):
    log = []
    monkeypatch.setattr(fireflies, "transport", ff_transport(log, {"data": {"transcripts": []}}))
    await fireflies.MeetingsList().run({}, NOKEY)
    k = next(iter(fireflies._cache))
    ts, v = fireflies._cache[k]
    fireflies._cache[k] = (ts - fireflies.CACHE_TTL_S - 1, v)
    await fireflies.MeetingsList().run({}, NOKEY)
    assert len(log) == 2


async def test_get_transcript(monkeypatch):
    log = []
    monkeypatch.setattr(fireflies, "transport", ff_transport(log, {"data": {"transcript": {
        "id": "t1", "title": "QBR", "date": 1, "participants": [], "sentences": [{"speaker_name": "Dana", "text": "Ship it"}],
        "summary": {"action_items": "- Priya to send recap", "overview": "o"}}}}))
    r = await fireflies.MeetingsGetTranscript().run({"meeting_id": "t1"}, NOKEY)
    assert r.output["transcript"] == "Dana: Ship it" and "recap" in r.output["summary"]["action_items"]
    assert log[0]["variables"] == {"id": "t1"}


async def test_fireflies_errors(monkeypatch):
    monkeypatch.setattr(fireflies, "transport", ff_transport([], {"data": None, "errors": [{"message": "Too many requests", "extensions": {"code": "too_many_requests"}}]}))
    r = await fireflies.MeetingsList().run({}, NOKEY)
    assert r.error.kind == ErrorKind.TRANSIENT and r.error.retryable
    monkeypatch.setattr(fireflies, "transport", ff_transport([], lambda req: httpx.Response(401, text="no")))
    assert (await fireflies.MeetingsList().run({"since": "x"}, NOKEY)).error.kind == ErrorKind.AUTH
    monkeypatch.setattr(fireflies, "transport", ff_transport([], {"data": {"transcript": None}}))
    assert (await fireflies.MeetingsGetTranscript().run({"meeting_id": "zz"}, NOKEY)).error.kind == ErrorKind.NOT_FOUND

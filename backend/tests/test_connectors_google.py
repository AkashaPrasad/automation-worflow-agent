"""Google connector tests: googleapiclient with HttpMockSequence (no network)."""
from __future__ import annotations

import base64
import json
import sqlite3

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from googleapiclient.discovery import build
from googleapiclient.http import HttpMockSequence

from app.api import oauth
from app.config import get_settings
from app.core.models import EffectClass, ErrorKind, ToolContext
from app.tools.connectors import google, live_integrations, live_tools, tokens

CTX = ToolContext(run_id="run_1", node_id="a1", workspace_id="ws_test_1", idempotency_key="key-123")
NOKEY = ToolContext(run_id="run_1", node_id="a1", workspace_id="ws_test_1", idempotency_key="")
OK = {"status": "200"}


def J(o):
    return json.dumps(o)


@pytest.fixture(autouse=True)
def settings(tmp_path, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "database_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(s, "google_client_id", "cid")
    monkeypatch.setattr(s, "google_client_secret", "csecret")
    monkeypatch.setattr(s, "public_base_url", "http://api.test")
    monkeypatch.setattr(s, "frontend_url", "http://ui.test")
    monkeypatch.delenv("OAUTH_ENC_KEY", raising=False)
    monkeypatch.setattr(google, "service_factory", google._default_service_factory)


def mock_google(monkeypatch, responses):
    http = HttpMockSequence(responses)
    monkeypatch.setattr(google, "service_factory",
                        lambda ws, api, ver: build(api, ver, http=http, static_discovery=True))
    return http


def tool(cls):
    return cls("ws_test_1")


def sent_raw(http, idx=-1):
    body = json.loads(http.request_sequence[idx][2])
    payload = body["message"]["raw"] if "message" in body else body["raw"]
    return base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode(), body


# ---------------- tool surface ----------------

def test_tool_names_match_spec_table():
    names = {c.spec.name for c in google.GOOGLE_TOOL_CLASSES}
    assert names == {
        "gmail.search", "gmail.read", "gmail.draft", "gmail.send", "calendar.list_events", "calendar.find_free_slots",
        "calendar.create_event", "calendar.delete_event", "docs.create", "docs.read", "docs.search", "docs.append",
        "sheets.read", "sheets.append_rows"}
    eff = {c.spec.name: c.spec.effect for c in google.GOOGLE_TOOL_CLASSES}
    assert eff["gmail.send"] == EffectClass.COMMUNICATE
    assert eff["calendar.create_event"] == EffectClass.COMMUNICATE
    assert eff["calendar.delete_event"] == EffectClass.WRITE_IRREVERSIBLE
    assert eff["gmail.draft"] == EffectClass.WRITE_REVERSIBLE
    assert eff["gmail.search"] == EffectClass.READ


# ---------------- gmail ----------------

async def test_gmail_search(monkeypatch):
    mock_google(monkeypatch, [
        (OK, J({"messages": [{"id": "m1"}]})),
        (OK, J({"id": "m1", "threadId": "t1", "snippet": "hello", "payload": {"headers": [
            {"name": "From", "value": "dana@northwind.com"}, {"name": "Subject", "value": "Delay"}]}})),
    ])
    r = await tool(google.GmailSearch).run({"query": "from:dana", "limit": 5}, NOKEY)
    assert r.ok and r.output["messages"][0]["from"] == "dana@northwind.com"


async def test_gmail_read_decodes_body(monkeypatch):
    data = base64.urlsafe_b64encode(b"Where is my shipment?").decode()
    mock_google(monkeypatch, [(OK, J({"id": "m1", "payload": {"mimeType": "multipart/alternative", "headers": [],
                "parts": [{"mimeType": "text/plain", "body": {"data": data}}]}}))])
    r = await tool(google.GmailRead).run({"message_id": "m1"}, NOKEY)
    assert r.output["body"] == "Where is my shipment?"


async def test_gmail_send_adds_key_header_and_result(monkeypatch):
    http = mock_google(monkeypatch, [(OK, J({})), (OK, J({"id": "sent1", "threadId": "t9"}))])  # empty Sent, then send
    args = {"to": ["a@x.com"], "subject": "Hi", "body": "Body"}
    r = await tool(google.GmailSend).run(args, CTX)
    assert r.ok and r.output["message_id"] == "sent1"
    assert r.effect.status == "applied" and r.effect.idempotency_key == "key-123"
    mime, _ = sent_raw(http)
    assert "X-Adjutant-Key: key-123" in mime and "To: a@x.com" in mime


async def test_gmail_send_dedupes_on_retry(monkeypatch):
    http = mock_google(monkeypatch, [
        (OK, J({"messages": [{"id": "old"}]})),
        (OK, J({"id": "old", "threadId": "t", "payload": {"headers": [{"name": "X-Adjutant-Key", "value": "key-123"}]}})),
    ])
    r = await tool(google.GmailSend).run({"to": ["a@x.com"], "subject": "Hi", "body": "B"}, CTX)
    assert r.ok and r.output["deduplicated"] is True
    assert len(http.request_sequence) == 2  # list + get; no send


async def test_gmail_send_reply_sets_threading(monkeypatch):
    http = mock_google(monkeypatch, [
        (OK, J({})),
        (OK, J({"threadId": "T1", "payload": {"headers": [{"name": "Message-ID", "value": "<abc@mail>"}]}})),
        (OK, J({"id": "s1", "threadId": "T1"})),
    ])
    r = await tool(google.GmailSend).run({"to": ["a@x.com"], "subject": "Re: Hi", "body": "B", "reply_to_id": "m0"}, CTX)
    assert r.ok
    mime, body = sent_raw(http)
    assert "In-Reply-To: <abc@mail>" in mime and body["threadId"] == "T1"


async def test_gmail_send_simulate_never_mutates(monkeypatch):
    http = mock_google(monkeypatch, [])
    r = await tool(google.GmailSend).simulate({"to": ["a@x.com"], "subject": "Hi", "body": "B"}, CTX)
    assert r.ok and r.simulated and r.effect.status == "simulated" and r.effect.simulated
    assert r.effect.preview["to"] == ["a@x.com"]
    assert http.request_sequence == []


async def test_gmail_send_simulate_checks_reply_target(monkeypatch):
    mock_google(monkeypatch, [({"status": "404"}, J({"error": {"message": "nf"}}))])
    r = await tool(google.GmailSend).simulate({"to": ["a@x.com"], "subject": "S", "body": "B", "reply_to_id": "nope"}, CTX)
    assert not r.ok and r.error.kind == ErrorKind.NOT_FOUND


async def test_gmail_reconcile(monkeypatch):
    t = tool(google.GmailSend)
    fx = (await t.simulate({"to": ["a@x.com"], "subject": "S", "body": "B"}, CTX)).effect
    mock_google(monkeypatch, [(OK, J({"messages": [{"id": "m"}]})),
                              (OK, J({"id": "m", "payload": {"headers": [{"name": "X-Adjutant-Key", "value": "key-123"}]}}))])
    assert await t.reconcile(fx, CTX) is True
    mock_google(monkeypatch, [(OK, J({}))])
    assert await t.reconcile(fx, CTX) is False
    mock_google(monkeypatch, [({"status": "500"}, "x")])
    assert await t.reconcile(fx, CTX) is None


async def test_gmail_draft_and_compensate(monkeypatch):
    http = mock_google(monkeypatch, [(OK, J({"id": "d1", "message": {"id": "m1"}})), (OK, "")])
    t = tool(google.GmailDraft)
    r = await t.run({"to": ["a@x.com"], "subject": "S", "body": "B"}, CTX)
    assert r.effect.compensation == {"tool": "gmail.delete_draft", "args": {"draft_id": "d1"}}
    c = await t.compensate(r.effect, CTX)
    assert c.ok and "DELETE" == http.request_sequence[-1][1]


# ---------------- errors + validation ----------------

@pytest.mark.parametrize("status,kind,retry", [(401, ErrorKind.AUTH, False), (403, ErrorKind.PERMISSION, False),
                                               (404, ErrorKind.NOT_FOUND, False), (412, ErrorKind.PRECONDITION, False),
                                               (400, ErrorKind.INVALID_ARGS, False), (429, ErrorKind.TRANSIENT, True),
                                               (503, ErrorKind.TRANSIENT, True)])
async def test_http_status_mapping(monkeypatch, status, kind, retry):
    mock_google(monkeypatch, [({"status": str(status)}, J({"error": {"message": "boom"}}))])
    r = await tool(google.GmailRead).run({"message_id": "m"}, NOKEY)
    assert not r.ok and r.error.kind == kind and r.error.retryable is retry


async def test_google_rate_limit_403_is_transient(monkeypatch):
    mock_google(monkeypatch, [({"status": "403"}, J({"error": {"errors": [{"reason": "rateLimitExceeded"}], "message": "rateLimitExceeded"}}))])
    r = await tool(google.GmailRead).run({"message_id": "m"}, NOKEY)
    assert r.error.kind == ErrorKind.TRANSIENT and r.error.retryable


async def test_invalid_args():
    r = await tool(google.GmailSend).run({"to": "not-a-list", "subject": "s"}, CTX)
    assert not r.ok and r.error.kind == ErrorKind.INVALID_ARGS
    r = await tool(google.GmailSend).simulate({"subject": "s"}, CTX)
    assert not r.ok and r.error.kind == ErrorKind.INVALID_ARGS


async def test_missing_credentials_is_auth_error():
    r = await tool(google.GmailSearch).run({"query": "x"}, NOKEY)  # default factory, no tokens stored
    assert not r.ok and r.error.kind == ErrorKind.AUTH


# ---------------- calendar ----------------

def test_event_id_is_valid_base32hex():
    eid = google.event_id_from_key("some-key")
    assert 5 <= len(eid) <= 1024 and set(eid) <= set("0123456789abcdefghijklmnopqrstuv")
    assert eid == google.event_id_from_key("some-key") != google.event_id_from_key("other")


EV = {"title": "Sync", "start": "2026-10-01T10:00:00Z", "end": "2026-10-01T10:30:00Z", "attendees": ["b@x.com"], "description": "d"}


async def test_calendar_create_uses_deterministic_id(monkeypatch):
    http = mock_google(monkeypatch, [(OK, J({"id": google.event_id_from_key("key-123"), "htmlLink": "http://cal"}))])
    r = await tool(google.CalendarCreateEvent).run(EV, CTX)
    assert r.ok and r.effect.compensation["tool"] == "calendar.delete_event"
    body = json.loads(http.request_sequence[0][2])
    assert body["id"] == google.event_id_from_key("key-123") and "sendUpdates=all" in http.request_sequence[0][0]


async def test_calendar_create_409_treated_as_already_applied(monkeypatch):
    eid = google.event_id_from_key("key-123")
    mock_google(monkeypatch, [({"status": "409"}, J({"error": {"message": "exists"}})), (OK, J({"id": eid, "status": "confirmed"}))])
    r = await tool(google.CalendarCreateEvent).run(EV, CTX)
    assert r.ok and r.output["deduplicated"] is True and r.output["event_id"] == eid


async def test_calendar_simulate_reports_conflicts_without_writing(monkeypatch):
    http = mock_google(monkeypatch, [(OK, J({"calendars": {"primary": {"busy": [{"start": "2026-10-01T10:00:00Z", "end": "2026-10-01T11:00:00Z"}]}}}))])
    r = await tool(google.CalendarCreateEvent).simulate(EV, CTX)
    assert r.ok and r.simulated and r.effect.preview["conflicts"]
    assert len(http.request_sequence) == 1 and "freeBusy" in http.request_sequence[0][0]


async def test_calendar_reconcile(monkeypatch):
    t = tool(google.CalendarCreateEvent)
    fx = t.effect(EV, CTX, summary="x")
    mock_google(monkeypatch, [(OK, J({"id": "e", "status": "confirmed"}))])
    assert await t.reconcile(fx, CTX) is True
    mock_google(monkeypatch, [({"status": "404"}, J({"error": {"message": "nf"}}))])
    assert await t.reconcile(fx, CTX) is False


async def test_find_free_slots(monkeypatch):
    mock_google(monkeypatch, [(OK, J({"calendars": {
        "primary": {"busy": [{"start": "2026-10-01T09:00:00Z", "end": "2026-10-01T10:00:00Z"}]},
        "b@x.com": {"busy": [{"start": "2026-10-01T09:30:00Z", "end": "2026-10-01T11:00:00Z"}]}}}))])
    r = await tool(google.CalendarFindFreeSlots).run({"attendees": ["b@x.com"], "duration_min": 30,
                                                      "window_start": "2026-10-01T08:00:00Z", "window_end": "2026-10-01T12:00:00Z"}, NOKEY)
    assert r.output["slots"] == [{"start": "2026-10-01T08:00:00Z", "end": "2026-10-01T09:00:00Z"},
                                 {"start": "2026-10-01T11:00:00Z", "end": "2026-10-01T12:00:00Z"}]


async def test_calendar_delete_gone_is_idempotent(monkeypatch):
    mock_google(monkeypatch, [({"status": "410"}, J({"error": {"message": "gone"}}))])
    r = await tool(google.CalendarDeleteEvent).run({"event_id": "e1"}, CTX)
    assert r.ok


# ---------------- docs + sheets ----------------

def test_md_to_docs_requests():
    text, styles = google.md_to_docs_requests("# Title\n- one\nplain **bold**")
    assert text == "Title\none\nplain bold\n"
    assert styles[0]["updateParagraphStyle"]["paragraphStyle"]["namedStyleType"] == "HEADING_1"
    assert styles[0]["updateParagraphStyle"]["range"] == {"startIndex": 1, "endIndex": 7}
    assert "createParagraphBullets" in styles[1]


async def test_docs_create_and_trash(monkeypatch):
    http = mock_google(monkeypatch, [(OK, J({"documentId": "doc1"})), (OK, J({})), (OK, J({"id": "doc1"}))])
    t = tool(google.DocsCreate)
    r = await t.run({"title": "OKRs", "content_md": "# Goals\n- ship"}, CTX)
    assert r.ok and r.output["doc_id"] == "doc1" and r.effect.compensation["tool"] == "docs.trash"
    reqs = json.loads(http.request_sequence[1][2])["requests"]
    assert reqs[0]["insertText"]["text"] == "Goals\nship\n"
    assert (await t.compensate(r.effect, CTX)).ok
    assert json.loads(http.request_sequence[2][2]) == {"trashed": True}


async def test_docs_append_and_revert(monkeypatch):
    doc = {"title": "T", "body": {"content": [{"endIndex": 20}]}}
    http = mock_google(monkeypatch, [(OK, J(doc)), (OK, J({})), (OK, J({}))])
    t = tool(google.DocsAppend)
    r = await t.run({"doc_id": "d", "content_md": "more"}, CTX)
    assert r.effect.compensation["args"] == {"doc_id": "d", "start": 19, "end": 24}
    assert (await t.compensate(r.effect, CTX)).ok
    assert json.loads(http.request_sequence[2][2])["requests"][0]["deleteContentRange"]["range"] == {"startIndex": 19, "endIndex": 24}


async def test_docs_read(monkeypatch):
    mock_google(monkeypatch, [(OK, J({"title": "T", "body": {"content": [{"paragraph": {"elements": [{"textRun": {"content": "hello\n"}}]}}]}}))])
    r = await tool(google.DocsRead).run({"doc_id": "d"}, NOKEY)
    assert r.output["text"] == "hello\n"


async def test_sheets_append_and_delete_rows(monkeypatch):
    http = mock_google(monkeypatch, [
        (OK, J({"updates": {"updatedRange": "Sheet1!A5:B6"}})),
        (OK, J({"sheets": [{"properties": {"sheetId": 77, "title": "Sheet1"}}]})),
        (OK, J({})),
    ])
    t = tool(google.SheetsAppendRows)
    r = await t.run({"sheet_id": "s1", "rows": [["a", 1], ["b", 2]]}, CTX)
    assert r.ok and r.effect.compensation["args"]["range"] == "Sheet1!A5:B6"
    assert (await t.compensate(r.effect, CTX)).ok
    req = json.loads(http.request_sequence[2][2])["requests"][0]["deleteDimension"]["range"]
    assert req == {"sheetId": 77, "dimension": "ROWS", "startIndex": 4, "endIndex": 6}


async def test_sheets_read_default_tab(monkeypatch):
    mock_google(monkeypatch, [(OK, J({"sheets": [{"properties": {"title": "Vendors"}}]})),
                              (OK, J({"range": "Vendors!A1:B2", "values": [["a", "b"]]}))])
    r = await tool(google.SheetsRead).run({"sheet_id": "s"}, NOKEY)
    assert r.output["rows"] == [["a", "b"]]


# ---------------- oauth + token store ----------------

def app_client():
    app = FastAPI()
    app.include_router(oauth.router)
    return TestClient(app, follow_redirects=False)


def test_oauth_start_redirects_with_scopes_and_signed_state():
    r = app_client().get("/api/oauth/google/start", params={"workspace_id": "ws_test_1"})
    assert r.status_code == 302
    loc = httpx.URL(r.headers["location"])
    q = dict(loc.params)
    assert loc.host == "accounts.google.com" and q["access_type"] == "offline" and q["prompt"] == "consent"
    assert q["redirect_uri"] == "http://api.test/api/oauth/google/callback"
    assert "gmail.send" in q["scope"] and "spreadsheets" in q["scope"] and "drive.file" in q["scope"]
    assert oauth.verify_state(q["state"]) == "ws_test_1"


def test_oauth_disabled_without_credentials(monkeypatch):
    monkeypatch.setattr(get_settings(), "google_client_id", "")
    assert app_client().get("/api/oauth/google/start", params={"workspace_id": "ws_test_1"}).status_code == 501


def test_state_tamper_and_expiry():
    st = oauth.sign_state("ws_test_1", now=1000)
    assert oauth.verify_state(st, now=1100) == "ws_test_1"
    assert oauth.verify_state(st, now=1000 + oauth.STATE_TTL_S + 5) is None
    assert oauth.verify_state(st[:-2] + "zz") is None


def test_oauth_callback_stores_encrypted_tokens(monkeypatch, tmp_path):
    monkeypatch.setenv("OAUTH_ENC_KEY", "unit-test-key")

    def handler(req: httpx.Request):
        assert req.url.host == "oauth2.googleapis.com"
        assert b"code=abc" in req.content
        return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT-secret", "expires_in": 3600, "scope": "a b"})

    monkeypatch.setattr(oauth, "_transport", httpx.MockTransport(handler))
    r = app_client().get("/api/oauth/google/callback", params={"code": "abc", "state": oauth.sign_state("ws_test_1")})
    assert r.status_code == 302 and r.headers["location"] == "http://ui.test/workspace?connected=google"
    stored = tokens.load_token("ws_test_1", "google")
    assert stored["refresh_token"] == "RT-secret" and stored["token"] == "AT"
    raw = sqlite3.connect(get_settings().database_path).execute("SELECT data FROM oauth_tokens").fetchone()[0]
    assert raw.startswith("fernet:") and "RT-secret" not in raw
    monkeypatch.delenv("OAUTH_ENC_KEY")
    assert tokens.load_token("ws_test_1", "google") is None  # encrypted, key gone


def test_oauth_callback_rejects_bad_state():
    assert app_client().get("/api/oauth/google/callback", params={"code": "abc", "state": "bad.state"}).status_code == 400


def test_credentials_refresh_persists(monkeypatch):
    tokens.save_token("ws_test_1", "google", {"token": "old", "refresh_token": "rt", "expiry": 1.0, "scopes": google.SCOPES})

    def fake_refresh(self, request):
        self.token = "new"
        import datetime
        self.expiry = datetime.datetime.utcnow() + datetime.timedelta(hours=1)

    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", fake_refresh)
    creds = google._credentials("ws_test_1")
    assert creds.token == "new" and tokens.load_token("ws_test_1", "google")["token"] == "new"


def test_live_tools_and_integrations(monkeypatch):
    s = get_settings()
    for f in ("notion_token", "slack_bot_token", "fireflies_api_key"):
        monkeypatch.setattr(s, f, "")
    assert live_tools("ws_test_1") == {}
    integ = live_integrations("ws_test_1")
    assert {i["app"] for i in integ} == {"gmail", "calendar", "docs", "sheets", "notion", "slack", "meetings"}
    assert all(i["mode"] == "live" and i["connected"] is False for i in integ)
    assert "oauth/google/start?workspace_id=ws_test_1" in integ[0]["detail"]
    tokens.save_token("ws_test_1", "google", {"token": "t", "refresh_token": "r"})
    monkeypatch.setattr(s, "slack_bot_token", "xoxb-test")
    lt = live_tools("ws_test_1")
    assert "gmail.send" in lt and "slack.post_message" in lt and "notion.search" not in lt
    assert live_tools("other_ws_1").keys() == {"slack.read_channel", "slack.post_message"}
    assert next(i for i in live_integrations("ws_test_1") if i["app"] == "gmail")["connected"] is True

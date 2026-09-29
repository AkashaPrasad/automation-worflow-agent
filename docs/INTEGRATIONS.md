# Live integrations and MCP

A workspace switches from the sandbox to real services by adding credentials. Live tools use the same names, args and effect classes as the sandbox (SPEC section 5), so plans are portable.

Code: `backend/app/tools/connectors/` (`live_tools(workspace_id)`, `live_integrations(workspace_id)`), `backend/app/tools/mcp_adapter.py`, `backend/app/api/oauth.py`.

## Connecting each service

| Service | Credentials (env) | Scope / permissions | Per-workspace? |
|---|---|---|---|
| Google (Gmail, Calendar, Docs, Sheets) | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`; user consents at `GET /api/oauth/google/start?workspace_id=...` | gmail.readonly, gmail.send, gmail.compose, calendar.events, calendar.readonly, documents, spreadsheets, drive.file | Yes (tokens in SQLite `oauth_tokens`) |
| Notion | `NOTION_TOKEN` (internal integration), `NOTION_PARENT_PAGE_ID` | Share the parent page and any pages to read with the integration | Global |
| Slack | `SLACK_BOT_TOKEN` | Bot scopes: `channels:read`, `channels:history`, `chat:write` (add `groups:read`/`groups:history` for private, `users:read` for names). Invite the bot to channels | Global |
| Fireflies | `FIREFLIES_API_KEY` | API key from Fireflies Settings > Developer | Global |
| MCP | `MCP_SERVERS="name=https://host/mcp,..."` | Streamable HTTP servers | Global |

Google setup: create an OAuth client (Web application) in Google Cloud, add redirect URI `${PUBLIC_BASE_URL}/api/oauth/google/callback`, enable the Gmail, Calendar, Docs, Sheets and Drive APIs. After consent the callback redirects to `${FRONTEND_URL}/workspace?connected=google`. Other endpoints: `GET /api/oauth/google/status?workspace_id=`, `DELETE /api/oauth/google?workspace_id=`. The router must be included by the app (`app.include_router(oauth.router)`); it returns 501 unless the Google client vars are set. State is an HMAC-signed, 10 minute token (`OAUTH_STATE_SECRET`, defaults to the client secret).

Token storage: SQLite table `oauth_tokens(workspace_id, provider, data, updated_at)` in `DATABASE_PATH`. Encrypted with Fernet when `OAUTH_ENC_KEY` is set (key = SHA-256 of that value); otherwise stored plain (prefix `plain:`). Set it in any shared deployment. Access tokens refresh automatically via `google.auth.transport.requests` and are re-saved.

Gmail restricted scopes: `gmail.readonly`, `gmail.send` and `gmail.compose` are Google "restricted" scopes. An app using them with more than 100 users must pass OAuth verification plus a CASA security assessment (annual). In "Testing" mode only listed test users (max 100) can connect and refresh tokens expire after 7 days. Plan for this before any public launch. `docs.search` uses Drive `files.list`, and with only `drive.file` it sees just files this app created or opened.

## Idempotency and reconciliation

| Tool | Key placement | Retry behaviour | `reconcile()` |
|---|---|---|---|
| gmail.send | MIME header `X-Adjutant-Key` | `run` first scans recent Sent (`in:sent newer_than:7d`, up to 50, header metadata only) and returns the existing message if found | same scan: True/False; None on API failure |
| calendar.create_event | Deterministic event `id` = base32hex(sha256(key)) lowercase | Insert returns 409 for the same id: fetch and treat as applied | `events.get(id)`: exists (not cancelled) True, 404 False |
| slack.post_message | Message `metadata` `{event_type: adjutant_action, event_payload: {key}}` | `run` checks `conversations.history(include_all_metadata)` (last 100) first | same lookup |
| notion.create_page | Last block is a gray code paragraph `adjutant-key:<key>` | `run` searches recent pages under the parent, checks their blocks for the marker | same lookup |
| docs.create, docs.append, sheets.append_rows, gmail.draft | none available in the API | Not idempotent on retry; reconcile returns None (docs.create checks the doc exists if an id is known) | mostly None |

Compensation (implemented on the creating tool via `compensate(effect, ctx)`, also recorded in `effect.compensation` with the SPEC tool name): gmail.draft deletes the draft; calendar.create_event deletes the event (`sendUpdates=all`, so attendees get a cancellation); docs.create moves the doc to Drive trash; docs.append deletes the appended index range; sheets.append_rows deletes the appended rows (parsed from `updatedRange`); notion.create_page archives the page; slack.post_message calls `chat.delete` (audience may already have seen it). The named compensation tools (`gmail.delete_draft`, `docs.trash`, ...) are not registered as separate planner tools.

`simulate()` for writes validates args and does read-only checks only: reply target exists (Gmail), freebusy conflicts (Calendar), event exists (delete_event), doc/spreadsheet exists (Docs/Sheets), parent page reachable (Notion), channel resolves (Slack).

Errors map to `ToolError`: 401 AUTH, 403 PERMISSION (Google rate-limit 403s are TRANSIENT), 404 NOT_FOUND, 409/412 PRECONDITION, 400/422 INVALID_ARGS, 429/5xx/timeouts TRANSIENT with `retryable=true`. Slack and Fireflies error codes are mapped similarly. Fireflies results are cached in-process for 10 minutes (free plan is rate limited to about 50 calls/day; `limit` max 50 per query).

## MCP

Servers in `MCP_SERVERS` are mounted at startup by `load_mcp_servers(judge)` (failures are logged, never fatal). Tools are named `mcp:<server>.<tool>`, app `mcp:<server>`, source `mcp`, output trust UNTRUSTED, not compensable. Each call opens a short-lived streamable HTTP session (60 s timeout). Args are validated against the tool's JSON Schema first.

Effect inference (annotations are untrusted hints):

1. `readOnlyHint: true` and a benign name: READ.
2. `destructiveHint: false` and `idempotentHint: true`: WRITE_REVERSIBLE.
3. `openWorldHint: true` with a messaging-ish name (send, post, email, notify, ...): COMMUNICATE.
4. Any other write (including destructive or unspecified destructiveHint): WRITE_IRREVERSIBLE.
5. Annotations missing, or contradictory (read-only hint on a verb-first name like `delete_x` or `send_x`, or a reversible hint on a destructive name): call `judge.infer_tool_effect`, then take the most conservative of annotation, inference and the name-verb heuristic (READ < REVERSIBLE < IRREVERSIBLE < COMMUNICATE). `effect_inferred=true`. Low-confidence (under 0.5) inference with no annotation is raised to at least WRITE_IRREVERSIBLE. If the judge is absent or fails: fail closed (COMMUNICATE for messaging-ish names, otherwise WRITE_IRREVERSIBLE).

`simulate()`: READ tools run for real; writes return a preview built from the args with the note "MCP tools cannot be dry-run; preview shows the arguments" and never contact the server. Note for the planner integration: tool names contain `:`, which some LLM function-calling APIs reject; map names if needed.

## Verified vs mocked

| Area | How tested | Live-verified? |
|---|---|---|
| Google API calls (Gmail, Calendar, Docs, Sheets, Drive) | `googleapiclient` with `HttpMockSequence` and the real bundled discovery docs: request shapes, headers, idempotency, error mapping | No. Response shapes and quota behaviour are from documentation |
| Google OAuth start/callback, state signing, token store, encryption, refresh | FastAPI TestClient, mocked token endpoint, real SQLite and Fernet, refresh monkeypatched | No real Google consent or refresh |
| Gmail Sent reconcile via `X-Adjutant-Key` | Mocked; relies on Gmail preserving custom headers in Sent | No (assumption) |
| Calendar deterministic id / 409 | Mocked 409 | No |
| Notion | Real `notion-client` over `httpx.MockTransport` (API version pinned to 2022-06-28) | No |
| Slack | Fake client object (no HTTP layer); error mapping by code | No. `metadata` on `chat.postMessage` and `include_all_metadata` history are per docs |
| Fireflies | `httpx.MockTransport`; query text follows docs.fireflies.ai | No. Field names/`duration` units unconfirmed |
| MCP | Real `mcp` 2.x SDK: in-process `MCPServer` fixture and a real uvicorn streamable HTTP server on a free port; annotation mapping, calls, errors | Yes against the local fixture only; no third-party server |

"""Live workspace connectors. Public surface: `live_tools`, `live_integrations`."""
from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.core.interfaces import Tool

from .fireflies import fireflies_configured, fireflies_tools
from .google import google_connected, google_tools
from .notion import notion_configured, notion_tools
from .slack import slack_configured, slack_tools

__all__ = ["live_integrations", "live_tools"]


def live_tools(workspace_id: str) -> dict[str, Tool]:
    """Tools whose credentials are present for this workspace (Google per-workspace; the rest global env)."""
    tools: dict[str, Any] = {}
    if google_connected(workspace_id):
        tools.update(google_tools(workspace_id))
    if notion_configured():
        tools.update(notion_tools())
    if slack_configured():
        tools.update(slack_tools())
    if fireflies_configured():
        tools.update(fireflies_tools())
    return tools


def live_integrations(workspace_id: str) -> list[dict[str, Any]]:
    s = get_settings()
    g_cfg = bool(s.google_client_id and s.google_client_secret)
    g_ok = google_connected(workspace_id)
    if g_ok:
        g_detail = "Connected via OAuth (Gmail, Calendar, Docs, Sheets)."
    elif g_cfg:
        g_detail = f"Not connected. Open {s.public_base_url}/api/oauth/google/start?workspace_id={workspace_id} to authorize."
    else:
        g_detail = "Not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET, then connect via /api/oauth/google/start."
    google_apps = [("gmail", "Gmail"), ("calendar", "Google Calendar"), ("docs", "Google Docs"), ("sheets", "Google Sheets")]
    out = [{"app": a, "title": t, "mode": "live", "connected": g_ok, "detail": g_detail} for a, t in google_apps]
    out.append({"app": "notion", "title": "Notion", "mode": "live", "connected": notion_configured(),
                "detail": "Internal integration token set." if notion_configured()
                else "Set NOTION_TOKEN (internal integration) and NOTION_PARENT_PAGE_ID; share pages with the integration."})
    out.append({"app": "slack", "title": "Slack", "mode": "live", "connected": slack_configured(),
                "detail": "Bot token set." if slack_configured()
                else "Set SLACK_BOT_TOKEN (scopes: channels:read, channels:history, chat:write, chat:write.public optional) and invite the bot to channels."})
    out.append({"app": "meetings", "title": "Fireflies", "mode": "live", "connected": fireflies_configured(),
                "detail": "API key set (results cached 10 min)." if fireflies_configured() else "Set FIREFLIES_API_KEY (Settings > Developer)."})
    return out

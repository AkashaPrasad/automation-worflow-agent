"""SandboxFirstRegistry: sandbox tools by default, live connectors override per tool name, MCP tools on top."""
from __future__ import annotations

import logging
from typing import Any

from app.core.interfaces import Tool
from app.core.models import ToolSpec

from .llm_tools import llm_tools
from .sandbox.tools import INTERNAL_TOOLS, build_sandbox_tools
from .sandbox.world import SandboxWorkspaceStore
from .web_tools import WebFetch

log = logging.getLogger("adjutant.tools")

APP_TITLES = {"gmail": "Gmail", "calendar": "Google Calendar", "docs": "Google Docs", "sheets": "Google Sheets", "notion": "Notion",
              "slack": "Slack", "meetings": "Fireflies meetings", "web": "Web"}


class SandboxFirstRegistry:
    def __init__(self, store: SandboxWorkspaceStore):
        self.store = store
        self._base: dict[str, Any] = {}
        self._base.update(build_sandbox_tools(store))
        self._base.update(llm_tools())
        web = WebFetch()
        self._base[web.spec.name] = web

    # ---- defensive access to the other engineers' modules ---------------------
    def _live(self, workspace_id: str) -> dict[str, Tool]:
        try:
            from app.tools.connectors import live_tools  # type: ignore[attr-defined]
        except ImportError:
            return {}
        try:
            return dict(live_tools(workspace_id) or {})
        except Exception as e:  # a broken connector must never take the sandbox down
            log.warning("live_tools failed: %s", e)
            return {}

    def _live_integrations(self, workspace_id: str) -> list[dict[str, Any]]:
        try:
            from app.tools.connectors import live_integrations  # type: ignore[attr-defined]
        except ImportError:
            return []
        try:
            return list(live_integrations(workspace_id) or [])
        except Exception as e:
            log.warning("live_integrations failed: %s", e)
            return []

    def _mcp(self) -> list[Tool]:
        try:
            from app.tools.mcp_adapter import mcp_tools  # type: ignore[attr-defined]
        except ImportError:
            return []
        try:
            return list(mcp_tools() or [])
        except Exception as e:
            log.warning("mcp_tools failed: %s", e)
            return []

    def _merged(self, workspace_id: str) -> dict[str, Tool]:
        tools: dict[str, Tool] = dict(self._base)
        tools.update(self._live(workspace_id))
        for t in self._mcp():
            tools[t.spec.name] = t
        return tools

    # ---- ToolRegistry protocol -------------------------------------------------
    def specs(self, workspace_id: str) -> list[ToolSpec]:
        return [t.spec for name, t in self._merged(workspace_id).items() if name not in INTERNAL_TOOLS]

    def get(self, name: str, workspace_id: str) -> Tool | None:
        return self._merged(workspace_id).get(name)

    def integrations(self, workspace_id: str) -> list[dict[str, Any]]:
        live = {i.get("app"): i for i in self._live_integrations(workspace_id)}
        out: list[dict[str, Any]] = []
        for app, title in APP_TITLES.items():
            if app in live:
                entry = {"app": app, "title": title, "mode": "sandbox", "connected": True, "detail": ""}
                entry.update(live[app])
            elif app == "web":
                entry = {"app": app, "title": title, "mode": "live", "connected": True, "detail": "Public web fetch (SSRF-guarded, text extraction)"}
            else:
                entry = {"app": app, "title": title, "mode": "sandbox", "connected": True,
                         "detail": "Acme Robotics sandbox workspace (no OAuth needed)"}
            out.append(entry)
        servers: dict[str, int] = {}
        for t in self._mcp():
            if t.spec.app.startswith("mcp:"):
                servers[t.spec.app] = servers.get(t.spec.app, 0) + 1
        for app, n in servers.items():
            out.append({"app": app, "title": f"MCP server {app[4:]}", "mode": "live", "connected": True, "detail": f"{n} tool(s)"})
        for app, i in live.items():
            if app not in APP_TITLES and app not in servers:
                out.append({"app": app, "title": i.get("title", app), "mode": i.get("mode", "live"),
                            "connected": i.get("connected", True), "detail": i.get("detail", "")})
        return out

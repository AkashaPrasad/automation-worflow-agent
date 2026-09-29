"""Shared request dependencies and the components container."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from fastapi import Header, HTTPException, Request

from app.api.ratelimit import SlidingWindowLimiter
from app.core.models import Run

_WS_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")


@dataclass
class Components:
    store: Any = None
    bus: Any = None
    orchestrator: Any = None
    registry: Any = None
    workspaces: Any = None
    judge: Any = None
    llm: Any = None
    limiter: SlidingWindowLimiter | None = None
    errors: dict[str, str] = field(default_factory=dict)  # component name -> why it is unavailable


def valid_workspace(value: str | None) -> str:
    if not value or not _WS_RE.match(value):
        raise HTTPException(400, "invalid workspace id: 8-64 characters of A-Z a-z 0-9 -")
    return value


def workspace_id(x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id")) -> str:
    if x_workspace_id is None:
        raise HTTPException(400, "missing X-Workspace-Id header")
    return valid_workspace(x_workspace_id)


def components(request: Request) -> Components:
    return request.app.state.components


def need(comp: Components, name: str) -> Any:
    obj = getattr(comp, name)
    if obj is None:
        raise HTTPException(503, f"{name} unavailable: {comp.errors.get(name, 'not initialised')}")
    return obj


def owned_run(comp: Components, ws: str, run_id: str) -> Run:
    run = need(comp, "store").get_run(run_id)
    if run is None or run.workspace_id != ws:
        raise HTTPException(404, "run not found")
    return run

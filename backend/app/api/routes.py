"""HTTP API (SPEC section 7)."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.api import agui
from app.api.deps import Components, components, need, owned_run, valid_workspace, workspace_id
from app.api.templates import DEMO_TEMPLATES
from app.config import get_settings
from app.core.interfaces import ConflictError
from app.core.models import TERMINAL_STATUSES, Autonomy, Budget, RunEvent, RunStatus

log = logging.getLogger("adjutant.api")
router = APIRouter(prefix="/api")

VERSION = "0.1.0"

AUTONOMY_LEVELS = [
    {"id": "cautious", "title": "Cautious", "description": "Every write asks for your approval before it runs."},
    {"id": "balanced", "title": "Balanced",
     "description": "Low-risk reversible writes run on their own; messages to others ask unless clearly safe."},
    {"id": "autonomous", "title": "Autonomous",
     "description": "Only risky, irreversible, or untrusted-content actions ask."},
]


# ---------------------------------------------------------------------------
# request models
# ---------------------------------------------------------------------------


class CreateRun(BaseModel):
    request: str = Field(min_length=1, max_length=8000)
    autonomy: Autonomy = Autonomy.BALANCED


class ResolveApproval(BaseModel):
    decisions: dict[str, Literal["approved", "rejected"]] = {}
    edits: dict[str, dict[str, Any]] = {}
    note: str = Field(default="", max_length=2000)


class Clarify(BaseModel):
    answer: str = Field(min_length=1, max_length=4000)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _orch(comp: Components, fn: str, *args: Any, **kwargs: Any) -> Any:
    orch = need(comp, "orchestrator")
    try:
        return await getattr(orch, fn)(*args, **kwargs)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(400, str(e) or "bad request") from e
    except LookupError as e:  # KeyError is a LookupError
        raise HTTPException(404, (e.args[0] if e.args and isinstance(e.args[0], str) else str(e)) or "not found") from e
    except PermissionError as e:
        raise HTTPException(403, str(e) or "forbidden") from e
    except ConflictError as e:
        raise HTTPException(409, str(e) or "conflict") from e


def _terminal_status_of(ev: RunEvent) -> bool:
    return agui.is_terminal(ev) is not None


async def _stream_events(comp: Components, run_id: str, after: int):
    """Yield RunEvents after `after`. Ends after a terminal run.status (or replay of a finished run)."""
    store, bus = comp.store, comp.bus
    run = store.get_run(run_id)
    if run is not None and run.status in TERMINAL_STATUSES:
        for ev in store.events(run_id, after):
            yield ev
        return
    last = after
    async for ev in bus.subscribe(run_id, after):
        last = max(last, ev.seq)
        yield ev
        if _terminal_status_of(ev):
            await asyncio.sleep(0.05)  # pick up trailing events published just after the status change
            for extra in store.events(run_id, last):
                yield extra
            return


def _after(request: Request, after: int) -> int:
    hdr = request.headers.get("last-event-id")
    if hdr and hdr.strip().isdigit():
        return max(after, int(hdr.strip()))
    return after


# ---------------------------------------------------------------------------
# meta
# ---------------------------------------------------------------------------


@router.get("/health")
async def health() -> dict:
    s = get_settings()
    return {
        "ok": True,
        "version": VERSION,
        "models": {"planner": s.model_name, "judge": s.jev_model, "fallback": "laya"},
        "llm_configured": bool(s.model_api_key),
        "judge_configured": bool(s.typesafe_api_key),
    }


@router.get("/config")
async def config(ws: str = Depends(workspace_id), comp: Components = Depends(components)) -> dict:
    s = get_settings()
    try:
        from app.judgment import policy

        policy_desc = policy.describe()
    except Exception as e:  # noqa: BLE001
        log.warning("policy.describe unavailable: %s", e)
        policy_desc = {}
    try:
        integrations = comp.registry.integrations(ws) if comp.registry is not None else []
    except Exception as e:  # noqa: BLE001
        log.warning("integrations unavailable: %s", e)
        integrations = []
    return {
        "autonomy_levels": AUTONOMY_LEVELS,
        "policy": policy_desc,
        "integrations": integrations,
        "budget_defaults": Budget().model_dump(),
        "templates": DEMO_TEMPLATES,
        "models": {"planner": s.model_name, "judge": s.jev_model, "fallback": "laya"},
    }


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------


@router.post("/runs")
async def create_run(body: CreateRun, ws: str = Depends(workspace_id), comp: Components = Depends(components)):
    need(comp, "orchestrator")
    if comp.limiter is not None:
        wait = comp.limiter.check(ws)
        if wait:
            raise HTTPException(
                429,
                f"rate limit reached ({comp.limiter.limit} runs per hour per workspace); retry in {int(wait)}s",
                headers={"Retry-After": str(int(wait))},
            )
    return await _orch(comp, "start_run", ws, body.request, body.autonomy)


@router.get("/runs")
async def list_runs(limit: int = Query(50, ge=1, le=200), ws: str = Depends(workspace_id),
                    comp: Components = Depends(components)):
    return need(comp, "store").list_runs(ws, limit)


@router.get("/runs/{run_id}")
async def get_run(run_id: str, ws: str = Depends(workspace_id), comp: Components = Depends(components)):
    run = owned_run(comp, ws, run_id)
    store = comp.store
    latest = getattr(store, "latest_approval", None)
    approval = latest(run_id) if latest else store.pending_approval(run_id)
    return {"run": run, "approval": approval, "effects": store.effects(run_id)}


@router.get("/runs/{run_id}/events")
async def run_events(run_id: str, after: int = Query(0, ge=0), ws: str = Depends(workspace_id),
                     comp: Components = Depends(components)):
    owned_run(comp, ws, run_id)
    return comp.store.events(run_id, after)


@router.get("/runs/{run_id}/stream")
async def run_stream(run_id: str, request: Request, after: int = Query(0, ge=0), workspace_id: str = Query(""),
                     comp: Components = Depends(components)):
    ws = valid_workspace(workspace_id)
    owned_run(comp, ws, run_id)
    need(comp, "bus")
    start = _after(request, after)

    async def gen():
        async for ev in _stream_events(comp, run_id, start):
            yield {"id": str(ev.seq), "event": "run_event", "data": ev.model_dump_json()}

    return EventSourceResponse(gen(), ping=15)


@router.get("/runs/{run_id}/agui")
async def run_agui(run_id: str, request: Request, after: int = Query(0, ge=0), workspace_id: str = Query(""),
                   comp: Components = Depends(components)):
    ws = valid_workspace(workspace_id)
    owned_run(comp, ws, run_id)
    need(comp, "bus")
    start = _after(request, after)

    async def gen():
        yield agui.encode(agui.run_started(run_id))
        finished = False
        async for ev in _stream_events(comp, run_id, start):
            msg = agui.encode(agui.map_event(ev))
            msg["id"] = str(ev.seq)
            yield msg
            st = agui.is_terminal(ev)
            if st is not None and not finished:
                finished = True
                yield agui.encode(agui.terminal_event(run_id, st, comp.store.get_run(run_id)))
                return
        if not finished:
            run = comp.store.get_run(run_id)
            if run is not None and run.status in TERMINAL_STATUSES:
                yield agui.encode(agui.terminal_event(run_id, run.status, run))

    return EventSourceResponse(gen(), ping=15)


@router.post("/runs/{run_id}/approvals/{approval_id}")
async def resolve_approval(run_id: str, approval_id: str, body: ResolveApproval, ws: str = Depends(workspace_id),
                           comp: Components = Depends(components)):
    owned_run(comp, ws, run_id)
    apv = comp.store.get_approval(approval_id)
    if apv is None or apv.run_id != run_id:
        raise HTTPException(404, "approval not found")
    if apv.status != "pending":
        raise HTTPException(409, "approval already resolved")
    nodes = {i.node_id for i in apv.items}
    unknown = (set(body.decisions) | set(body.edits)) - nodes
    if unknown:
        raise HTTPException(400, f"unknown node ids in approval: {sorted(unknown)}")
    return await _orch(comp, "resolve_approval", run_id, approval_id, body.decisions, body.edits, body.note)


@router.post("/runs/{run_id}/clarify")
async def clarify(run_id: str, body: Clarify, ws: str = Depends(workspace_id),
                  comp: Components = Depends(components)):
    run = owned_run(comp, ws, run_id)
    if run.status != RunStatus.CLARIFYING:
        raise HTTPException(409, f"run is not waiting for clarification (status: {run.status.value})")
    return await _orch(comp, "answer_clarification", run_id, body.answer)


def _control(name: str):
    async def handler(run_id: str, ws: str = Depends(workspace_id), comp: Components = Depends(components)):
        owned_run(comp, ws, run_id)
        return await _orch(comp, name, run_id)

    handler.__name__ = f"run_{name}"
    router.post(f"/runs/{{run_id}}/{name}")(handler)


for _name in ("pause", "resume", "cancel", "rollback"):
    _control(_name)


# ---------------------------------------------------------------------------
# workspace, tools, memory
# ---------------------------------------------------------------------------


@router.get("/workspace")
async def workspace(ws: str = Depends(workspace_id), comp: Components = Depends(components)):
    return need(comp, "workspaces").snapshot(ws)


@router.post("/workspace/reset")
async def workspace_reset(ws: str = Depends(workspace_id), comp: Components = Depends(components)):
    return need(comp, "workspaces").reset(ws)


@router.get("/tools")
async def tools(ws: str = Depends(workspace_id), comp: Components = Depends(components)):
    return need(comp, "registry").specs(ws)


@router.get("/memory")
async def memory(ws: str = Depends(workspace_id), comp: Components = Depends(components)):
    return need(comp, "store").memories(ws)


@router.delete("/memory/{memory_id}")
async def delete_memory(memory_id: str, ws: str = Depends(workspace_id), comp: Components = Depends(components)):
    store = need(comp, "store")
    if not any(m.id == memory_id for m in store.memories(ws)):
        raise HTTPException(404, "memory not found")
    store.delete_memory(memory_id)
    return {"ok": True}

import json

import httpx
import pytest

from app.api.deps import Components
from app.core.interfaces import ConflictError
from app.api.ratelimit import SlidingWindowLimiter
from app.core.models import (
    Approval, ApprovalItem, Autonomy, EffectClass, GateDecision, MemoryItem, Run, RunEvent, RunStatus, ToolSpec,
    Verdict,
)
from app.main import create_app
from app.store.bus import LocalEventBus
from app.store.sqlite import SqliteRunStore

WS = "ws-testtest-1"
OTHER = "ws-other-0000"
H = {"X-Workspace-Id": WS}


class FakeOrch:
    def __init__(self, store, bus):
        self.store, self.bus = store, bus
        self.calls = []

    async def start_run(self, ws, request, autonomy):
        if request == "boom":
            raise ValueError("bad request text")
        run = self.store.create_run(Run(workspace_id=ws, request=request, autonomy=autonomy))
        await self.bus.publish(RunEvent(run_id=run.id, type="run.created"))
        return run

    async def resolve_approval(self, run_id, approval_id, decisions, edits, note=""):
        self.calls.append(("resolve", decisions, edits, note))
        return self.store.get_run(run_id)

    async def answer_clarification(self, run_id, answer):
        self.calls.append(("clarify", answer))
        return self.store.get_run(run_id)

    async def _do(self, name, run_id):
        self.calls.append((name, run_id))
        if name == "resume":
            raise ConflictError("not paused")
        if name == "rollback":
            raise PermissionError("nope")
        if name == "cancel":
            raise LookupError("gone")
        return self.store.get_run(run_id)

    async def pause(self, r): return await self._do("pause", r)
    async def resume(self, r): return await self._do("resume", r)
    async def cancel(self, r): return await self._do("cancel", r)
    async def rollback(self, r): return await self._do("rollback", r)
    async def recover_unfinished(self): pass


class FakeRegistry:
    def specs(self, ws):
        return [ToolSpec(name="gmail.send", app="gmail", title="t", description="d", effect=EffectClass.COMMUNICATE,
                         input_schema={"type": "object"})]

    def integrations(self, ws):
        return [{"app": "gmail", "title": "Gmail", "mode": "sandbox", "connected": True, "detail": ""}]


class FakeWorkspaces:
    def snapshot(self, ws): return {"profile": {"user_name": "Priya Shah"}, "mail": []}
    def reset(self, ws): return {"profile": {}, "reset": True}


@pytest.fixture
def env(tmp_path):
    store = SqliteRunStore(str(tmp_path / "a.db"))
    bus = LocalEventBus(store)
    comp = Components(store=store, bus=bus, orchestrator=FakeOrch(store, bus), registry=FakeRegistry(),
                      workspaces=FakeWorkspaces(), limiter=SlidingWindowLimiter(3))
    app = create_app(comp)
    return app, comp


@pytest.fixture
async def client(env):
    app, _ = env
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


def parse_sse(text):
    msgs, cur = [], {}
    for line in text.replace("\r\n", "\n").split("\n"):
        if line == "":
            if cur:
                msgs.append(cur)
                cur = {}
        elif line.startswith(":"):
            continue
        else:
            k, _, v = line.partition(":")
            cur[k] = v.lstrip(" ")
    if cur:
        msgs.append(cur)
    return msgs


async def new_run(client, request="hello", ws=WS):
    r = await client.post("/api/runs", json={"request": request}, headers={"X-Workspace-Id": ws})
    assert r.status_code == 200, r.text
    return r.json()


async def test_health_no_workspace_and_no_secrets(client):
    r = await client.get("/api/health")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] and set(j["models"]) == {"planner", "judge", "fallback"}
    assert isinstance(j["llm_configured"], bool) and isinstance(j["judge_configured"], bool)
    assert "key" not in json.dumps(j).lower().replace("configured", "")


@pytest.mark.parametrize("ws", ["short", "x" * 65, "bad id here!", "under_score_1"])
async def test_workspace_validation(client, ws):
    r = await client.get("/api/runs", headers={"X-Workspace-Id": ws})
    assert r.status_code == 400 and "detail" in r.json()


async def test_workspace_required(client):
    r = await client.get("/api/runs")
    assert r.status_code == 400


async def test_create_list_get_and_ownership(client, env):
    run = await new_run(client)
    assert run["workspace_id"] == WS and run["autonomy"] == "balanced"
    assert [x["id"] for x in (await client.get("/api/runs", headers=H)).json()] == [run["id"]]
    other = {"X-Workspace-Id": OTHER}
    assert (await client.get("/api/runs", headers=other)).json() == []
    for path in ("", "/events", "/stream", "/agui"):
        url = f"/api/runs/{run['id']}{path}"
        r = await client.get(url, headers=other) if not path.startswith(("/stream", "/agui")) else \
            await client.get(url, params={"workspace_id": OTHER})
        assert r.status_code == 404, url
    assert (await client.post(f"/api/runs/{run['id']}/pause", headers=other)).status_code == 404
    d = (await client.get(f"/api/runs/{run['id']}", headers=H)).json()
    assert d["run"]["id"] == run["id"] and d["approval"] is None and d["effects"] == []
    assert (await client.get("/api/runs/run_missing", headers=H)).status_code == 404


async def test_create_run_validation(client):
    assert (await client.post("/api/runs", json={"request": ""}, headers=H)).status_code == 422
    assert (await client.post("/api/runs", json={"request": "x", "autonomy": "wild"}, headers=H)).status_code == 422
    r = await client.post("/api/runs", json={"request": "boom"}, headers=H)
    assert r.status_code == 400 and r.json()["detail"] == "bad request text"


async def test_rate_limit(client):
    for _ in range(3):
        await new_run(client)
    r = await client.post("/api/runs", json={"request": "again"}, headers=H)
    assert r.status_code == 429 and "rate limit" in r.json()["detail"] and "retry-after" in r.headers
    assert (await client.post("/api/runs", json={"request": "x"}, headers={"X-Workspace-Id": OTHER})).status_code == 200


async def test_error_mapping(client):
    run = await new_run(client)
    base = f"/api/runs/{run['id']}"
    assert (await client.post(f"{base}/pause", headers=H)).status_code == 200
    assert (await client.post(f"{base}/resume", headers=H)).status_code == 409
    assert (await client.post(f"{base}/rollback", headers=H)).status_code == 403
    assert (await client.post(f"{base}/cancel", headers=H)).status_code == 404


async def test_clarify(client, env):
    _, comp = env
    run = await new_run(client)
    assert (await client.post(f"/api/runs/{run['id']}/clarify", json={"answer": "x"}, headers=H)).status_code == 409
    r = comp.store.get_run(run["id"])
    r.status = RunStatus.CLARIFYING
    comp.store.save_run(r)
    assert (await client.post(f"/api/runs/{run['id']}/clarify", json={"answer": "x"}, headers=H)).status_code == 200
    assert comp.orchestrator.calls[-1] == ("clarify", "x")


async def test_approvals(client, env):
    _, comp = env
    run = await new_run(client)
    item = ApprovalItem(node_id="a1", tool="gmail.send", summary="s", effect=EffectClass.COMMUNICATE, args={},
                        args_hash="h", gate=GateDecision(verdict=Verdict.ASK))
    apv = Approval(run_id=run["id"], items=[item])
    comp.store.save_approval(apv)
    url = f"/api/runs/{run['id']}/approvals/{apv.id}"
    d = (await client.get(f"/api/runs/{run['id']}", headers=H)).json()
    assert d["approval"]["id"] == apv.id
    assert (await client.post(url, json={"decisions": {"zz": "approved"}}, headers=H)).status_code == 400
    assert (await client.post(url, json={"decisions": {"a1": "maybe"}}, headers=H)).status_code == 422
    r = await client.post(url, json={"decisions": {"a1": "approved"}, "edits": {"a1": {"body": "hi"}}, "note": "n"},
                          headers=H)
    assert r.status_code == 200
    assert comp.orchestrator.calls[-1] == ("resolve", {"a1": "approved"}, {"a1": {"body": "hi"}}, "n")
    apv.status = "resolved"
    comp.store.save_approval(apv)
    assert (await client.post(url, json={"decisions": {"a1": "approved"}}, headers=H)).status_code == 409
    assert (await client.post(f"/api/runs/{run['id']}/approvals/apv_nope", json={}, headers=H)).status_code == 404
    # latest (resolved) approval still returned
    assert (await client.get(f"/api/runs/{run['id']}", headers=H)).json()["approval"]["status"] == "resolved"


async def finished_run(env, statuses=("completed",)):
    _, comp = env
    run = comp.store.create_run(Run(workspace_id=WS, request="r"))
    await comp.bus.publish(RunEvent(run_id=run.id, type="run.created"))
    await comp.bus.publish(RunEvent(run_id=run.id, type="plan.created", data={"plan": {"root_id": "g1"}}))
    await comp.bus.publish(RunEvent(run_id=run.id, type="node.started", node_id="a1", data={"tool": "t"}))
    await comp.bus.publish(RunEvent(run_id=run.id, type="node.result", node_id="a1", data={"result": {}}))
    await comp.bus.publish(RunEvent(run_id=run.id, type="log", data={"message": "hi"}))
    for s in statuses:
        await comp.bus.publish(RunEvent(run_id=run.id, type="run.status", data={"status": s}))
    run.status = RunStatus(statuses[-1])
    run.summary = "all done"
    comp.store.save_run(run)
    return run


async def test_events_endpoint(client, env):
    run = await finished_run(env)
    r = await client.get(f"/api/runs/{run.id}/events", params={"after": 2}, headers=H)
    assert [e["seq"] for e in r.json()] == [3, 4, 5, 6]


async def test_sse_framing_and_termination(client, env):
    run = await finished_run(env)
    r = await client.get(f"/api/runs/{run.id}/stream", params={"workspace_id": WS})
    assert r.headers["content-type"].startswith("text/event-stream")
    msgs = parse_sse(r.text)
    assert [m["id"] for m in msgs] == ["1", "2", "3", "4", "5", "6"]
    assert all(m["event"] == "run_event" for m in msgs)
    last = json.loads(msgs[-1]["data"])
    assert last["type"] == "run.status" and last["seq"] == 6
    # Last-Event-ID and ?after=
    r = await client.get(f"/api/runs/{run.id}/stream", params={"workspace_id": WS}, headers={"Last-Event-ID": "4"})
    assert [m["id"] for m in parse_sse(r.text)] == ["5", "6"]
    r = await client.get(f"/api/runs/{run.id}/stream", params={"workspace_id": WS, "after": 5})
    assert [m["id"] for m in parse_sse(r.text)] == ["6"]


async def test_sse_requires_valid_workspace(client, env):
    run = await finished_run(env)
    assert (await client.get(f"/api/runs/{run.id}/stream")).status_code == 400


async def test_sse_live_ends_on_terminal(client, env):
    """Non-terminal run: stream stays open, then ends after the terminal status arrives."""
    import asyncio
    _, comp = env
    run = comp.store.create_run(Run(workspace_id=WS, request="live", status=RunStatus.EXECUTING))
    await comp.bus.publish(RunEvent(run_id=run.id, type="run.created"))

    async def later():
        await asyncio.sleep(0.1)
        await comp.bus.publish(RunEvent(run_id=run.id, type="log", data={"message": "x"}))
        await comp.bus.publish(RunEvent(run_id=run.id, type="run.status", data={"status": "failed"}))

    t = asyncio.create_task(later())
    r = await asyncio.wait_for(client.get(f"/api/runs/{run.id}/stream", params={"workspace_id": WS}), 5)
    await t
    assert [m["id"] for m in parse_sse(r.text)] == ["1", "2", "3"]


async def test_agui_mapping(client, env):
    run = await finished_run(env)
    r = await client.get(f"/api/runs/{run.id}/agui", params={"workspace_id": WS})
    msgs = parse_sse(r.text)
    payloads = [json.loads(m["data"]) for m in msgs]
    types = [p["type"] for p in payloads]
    assert types == ["RUN_STARTED", "CUSTOM", "STATE_SNAPSHOT", "STEP_STARTED", "STEP_FINISHED", "CUSTOM",
                     "CUSTOM", "RUN_FINISHED"]
    assert msgs[0]["event"] == "RUN_STARTED"
    assert payloads[0]["runId"] == run.id and payloads[0]["threadId"] == run.id
    assert payloads[1]["name"] == "run.created"
    assert payloads[2]["snapshot"] == {"plan": {"root_id": "g1"}}
    assert payloads[3]["stepName"] == "a1"
    assert payloads[-1]["result"]["summary"] == "all done"


async def test_agui_error_terminal(client, env):
    run = await finished_run(env, statuses=("failed",))
    r = await client.get(f"/api/runs/{run.id}/agui", params={"workspace_id": WS})
    types = [json.loads(m["data"])["type"] for m in parse_sse(r.text)]
    assert types[-1] == "RUN_ERROR"


async def test_config_shape(client):
    j = (await client.get("/api/config", headers=H)).json()
    assert [a["id"] for a in j["autonomy_levels"]] == ["cautious", "balanced", "autonomous"]
    assert j["integrations"][0]["app"] == "gmail"
    assert j["budget_defaults"]["max_replans"] == 3
    assert len(j["templates"]) == 4
    for t in j["templates"]:
        assert {"id", "title", "prompt", "apps", "highlights"} <= set(t)
    assert set(j["models"]) == {"planner", "judge", "fallback"} and j["models"]["fallback"] == "laya"
    assert "policy" in j


async def test_workspace_tools_memory(client, env):
    _, comp = env
    assert (await client.get("/api/workspace", headers=H)).json()["profile"]["user_name"] == "Priya Shah"
    assert (await client.post("/api/workspace/reset", headers=H)).json()["reset"] is True
    assert (await client.get("/api/tools", headers=H)).json()[0]["name"] == "gmail.send"
    m = comp.store.add_memory(MemoryItem(workspace_id=WS, text="fact"))
    assert [x["id"] for x in (await client.get("/api/memory", headers=H)).json()] == [m.id]
    assert (await client.delete(f"/api/memory/{m.id}", headers={"X-Workspace-Id": OTHER})).status_code == 404
    assert (await client.delete(f"/api/memory/{m.id}", headers=H)).status_code == 200
    assert (await client.get("/api/memory", headers=H)).json() == []


async def test_cors_and_unavailable_component(tmp_path):
    app = create_app(Components(store=SqliteRunStore(str(tmp_path / "x.db"))))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/api/health")).status_code == 200
        assert (await c.post("/api/runs", json={"request": "x"}, headers=H)).status_code == 503
        r = await c.options("/api/runs", headers={"Origin": "http://localhost:5173",
                                                  "Access-Control-Request-Method": "POST",
                                                  "Access-Control-Request-Headers": "x-workspace-id,last-event-id"})
        assert r.status_code == 200 and "x-workspace-id" in r.headers["access-control-allow-headers"].lower()

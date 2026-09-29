"""MCP adapter tests: local MCPServer fixture, in-process and over real streamable HTTP on a free port."""
from __future__ import annotations

import socket
import threading
import time

import pytest
import uvicorn
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from app.config import get_settings
from app.core.models import EffectClass, ErrorKind, ToolContext, Trust
from app.tools import mcp_adapter as m

CTX = ToolContext(run_id="r", node_id="a", workspace_id="ws_test_1", idempotency_key="k")


def build_server() -> MCPServer:
    s = MCPServer("fixture")

    @s.tool(annotations=ToolAnnotations(read_only_hint=True))
    def get_weather(city: str) -> str:
        """Weather lookup"""
        return f"sunny in {city}"

    @s.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True))
    def set_note(key: str, value: str) -> str:
        """Upsert a note"""
        return f"{key}={value}"

    @s.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True))
    def wipe_table(name: str) -> str:
        """Drop a table"""
        return "gone"

    @s.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True))
    def send_email(to: str) -> str:
        """Send mail"""
        return "sent"

    @s.tool(annotations=ToolAnnotations(read_only_hint=True))
    def delete_everything() -> str:
        """Lies about being read only"""
        return "deleted"

    @s.tool()
    def mystery(x: int) -> str:
        """No annotations"""
        return str(x)

    @s.tool()
    def explode() -> str:
        """always fails"""
        raise ValueError("kaboom")

    return s


class FakeJudge:
    def __init__(self, answer=("read", 0.9)):
        self.answer, self.calls = answer, []

    async def infer_tool_effect(self, name, description, annotations):
        self.calls.append((name, description, annotations))
        return self.answer


@pytest.fixture(autouse=True)
def inproc(monkeypatch):
    srv = build_server()
    monkeypatch.setattr(m, "client_factory", lambda url: Client(srv))
    monkeypatch.setattr(get_settings(), "mcp_servers", "fx=http://fixture.invalid/mcp")
    monkeypatch.setattr(m, "_TOOLS", [])


def by_name():
    return {t.spec.name: t for t in m.mcp_tools()}


def test_parse_servers():
    assert m.parse_servers("a=http://x/mcp, b=https://y/mcp,bad,=z") == {"a": "http://x/mcp", "b": "https://y/mcp"}


async def test_mount_and_annotation_mapping():
    judge = FakeJudge(("read", 0.9))
    await m.load_mcp_servers(judge)
    t = by_name()
    assert set(t) == {f"mcp:fx.{n}" for n in ["get_weather", "set_note", "wipe_table", "send_email", "delete_everything", "mystery", "explode"]}
    sp = {k: v.spec for k, v in t.items()}
    assert sp["mcp:fx.get_weather"].effect == EffectClass.READ and not sp["mcp:fx.get_weather"].effect_inferred
    assert sp["mcp:fx.set_note"].effect == EffectClass.WRITE_REVERSIBLE and sp["mcp:fx.set_note"].idempotent
    assert sp["mcp:fx.wipe_table"].effect == EffectClass.WRITE_IRREVERSIBLE
    assert sp["mcp:fx.send_email"].effect == EffectClass.COMMUNICATE
    # contradictory: readOnly but named delete_*; judge says read, but conservative rule keeps it a write
    de = sp["mcp:fx.delete_everything"]
    assert de.effect != EffectClass.READ and de.effect_inferred
    # missing annotations: judge said READ at 0.9 confidence -> accepted, marked inferred
    assert sp["mcp:fx.mystery"].effect == EffectClass.READ and sp["mcp:fx.mystery"].effect_inferred
    assert {c[0] for c in judge.calls} == {"fx.delete_everything", "fx.mystery", "fx.explode"}
    for s in sp.values():
        assert s.source == "mcp" and s.output_trust == Trust.UNTRUSTED and s.app == "mcp:fx" and s.compensable is False
    assert sp["mcp:fx.get_weather"].input_schema["properties"]["city"]["type"] == "string"


async def test_judge_takes_more_conservative_and_fails_closed():
    await m.load_mcp_servers(FakeJudge(("communicate", 0.8)))
    assert by_name()["mcp:fx.delete_everything"].spec.effect == EffectClass.COMMUNICATE
    assert by_name()["mcp:fx.get_weather"].spec.effect == EffectClass.READ  # consistent annotation: judge not consulted

    class Boom:
        async def infer_tool_effect(self, *a):
            raise RuntimeError("down")

    await m.load_mcp_servers(Boom())
    assert by_name()["mcp:fx.mystery"].spec.effect == EffectClass.WRITE_IRREVERSIBLE
    await m.load_mcp_servers(None)  # no judge at all
    assert by_name()["mcp:fx.mystery"].spec.effect == EffectClass.WRITE_IRREVERSIBLE
    assert by_name()["mcp:fx.delete_everything"].spec.effect == EffectClass.WRITE_IRREVERSIBLE


async def test_low_confidence_inference_is_bumped():
    await m.load_mcp_servers(FakeJudge(("read", 0.2)))
    assert by_name()["mcp:fx.mystery"].spec.effect == EffectClass.WRITE_IRREVERSIBLE


async def test_read_call_end_to_end():
    await m.load_mcp_servers(FakeJudge())
    t = by_name()["mcp:fx.get_weather"]
    r = await t.run({"city": "Oslo"}, CTX)
    assert r.ok and "sunny in Oslo" in r.output["text"] and r.effect is None
    r = await t.simulate({"city": "Oslo"}, CTX)  # reads run for real
    assert r.ok and not r.simulated


async def test_write_call_records_effect():
    await m.load_mcp_servers(FakeJudge())
    r = await by_name()["mcp:fx.set_note"].run({"key": "a", "value": "b"}, CTX)
    assert r.ok and r.output["text"] == "a=b"
    assert r.effect.status == "applied" and r.effect.app == "mcp:fx" and r.effect.compensation is None


async def test_write_simulate_never_calls_server(monkeypatch):
    await m.load_mcp_servers(FakeJudge())

    def boom(url):
        raise AssertionError("must not connect during simulate")

    monkeypatch.setattr(m, "client_factory", boom)
    r = await by_name()["mcp:fx.wipe_table"].simulate({"name": "users"}, CTX)
    assert r.ok and r.simulated and r.effect.status == "simulated"
    assert r.effect.preview["args"] == {"name": "users"} and "cannot be dry-run" in r.effect.preview["note"]


async def test_errors():
    await m.load_mcp_servers(FakeJudge())
    r = await by_name()["mcp:fx.get_weather"].run({}, CTX)  # schema violation caught before the call
    assert r.error.kind == ErrorKind.INVALID_ARGS
    r = await by_name()["mcp:fx.explode"].run({}, CTX)
    assert not r.ok and "explode" in r.error.message


async def test_unreachable_server_is_not_fatal_and_call_is_transient(monkeypatch):
    class Dead:
        async def __aenter__(self):
            raise ConnectionError("refused")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(m, "client_factory", lambda url: Dead())
    await m.load_mcp_servers(FakeJudge())  # must not raise
    assert m.mcp_tools() == []


async def test_call_connection_failure_maps_to_transient(monkeypatch):
    await m.load_mcp_servers(FakeJudge())

    class Dead:
        async def __aenter__(self):
            raise ConnectionError("refused")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(m, "client_factory", lambda url: Dead())
    r = await by_name()["mcp:fx.get_weather"].run({"city": "x"}, CTX)
    assert r.error.kind == ErrorKind.TRANSIENT and r.error.retryable


# ---- real streamable HTTP on a free port ----

async def test_over_real_http(monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(build_server().streamable_http_app(), host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        monkeypatch.setattr(m, "client_factory", lambda url: Client(url))
        monkeypatch.setattr(get_settings(), "mcp_servers", f"live=http://127.0.0.1:{port}/mcp")
        await m.load_mcp_servers(FakeJudge())
        tools = {t.spec.name: t for t in m.mcp_tools()}
        assert "mcp:live.get_weather" in tools
        r = await tools["mcp:live.get_weather"].run({"city": "Rome"}, CTX)
        assert r.ok and "sunny in Rome" in r.output["text"]
    finally:
        server.should_exit = True
        th.join(timeout=5)

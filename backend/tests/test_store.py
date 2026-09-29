import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core.models import Approval, EffectClass, EffectRecord, MemoryItem, Run, RunEvent, RunStatus
from app.store.bus import LocalEventBus
from app.store.sqlite import SqliteRunStore


@pytest.fixture
def store(tmp_path):
    return SqliteRunStore(str(tmp_path / "t.db"))


def ev(run_id, t="log", **d):
    return RunEvent(run_id=run_id, type=t, data=d)


def test_runs_roundtrip_and_listing(store):
    a = store.create_run(Run(workspace_id="ws-aaaaaaaa", request="a"))
    b = store.create_run(Run(workspace_id="ws-aaaaaaaa", request="b"))
    store.create_run(Run(workspace_id="ws-bbbbbbbb", request="c"))
    assert store.get_run(a.id).request == "a"
    assert store.get_run("nope") is None
    assert [r.request for r in store.list_runs("ws-aaaaaaaa")] == ["b", "a"]
    b.status = RunStatus.COMPLETED
    store.save_run(b)
    assert store.list_runs("ws-aaaaaaaa")[0].status == RunStatus.COMPLETED


def test_unfinished_runs(store):
    r1 = store.create_run(Run(workspace_id="w", request="1", status=RunStatus.EXECUTING))
    r2 = store.create_run(Run(workspace_id="w", request="2", status=RunStatus.COMPLETED))
    r3 = store.create_run(Run(workspace_id="w", request="3", status=RunStatus.PAUSED))
    for s in (RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.ROLLED_BACK):
        store.create_run(Run(workspace_id="w", request="x", status=s))
    ids = {r.id for r in store.unfinished_runs()}
    assert ids == {r1.id, r3.id} and r2.id not in ids


def test_event_seq_ordering_and_after(store):
    seqs = [store.append_event(ev("r1", n=i)).seq for i in range(5)]
    assert seqs == [1, 2, 3, 4, 5]
    assert store.append_event(ev("r2")).seq == 1
    assert [e.seq for e in store.events("r1", 2)] == [3, 4, 5]
    assert store.events("r1")[0].data == {"n": 0}


def test_event_seq_concurrent(store):
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(lambda i: store.append_event(ev("r1", i=i)).seq, range(200)))
    assert sorted(res) == list(range(1, 201))
    assert [e.seq for e in store.events("r1")] == list(range(1, 201))


def test_effects_and_unique_key(store):
    e = EffectRecord(run_id="r", tool="gmail.send", app="gmail", effect=EffectClass.COMMUNICATE, summary="s",
                     idempotency_key="k1")
    store.upsert_effect(e)
    e.status = "applied"
    store.upsert_effect(e)
    assert len(store.effects("r")) == 1
    assert store.effect_by_key("k1").status == "applied"
    assert store.effect_by_key("zzz") is None
    # empty keys never collide
    for _ in range(2):
        store.upsert_effect(EffectRecord(run_id="r", tool="t", app="a", effect=EffectClass.READ, summary="x"))
    assert len(store.effects("r")) == 3
    # same key, different id: superseded, still unique
    e2 = EffectRecord(run_id="r", tool="gmail.send", app="gmail", effect=EffectClass.COMMUNICATE, summary="s2",
                      idempotency_key="k1")
    store.upsert_effect(e2)
    assert store.effect_by_key("k1").id == e2.id


def test_approvals(store):
    a1 = Approval(run_id="r", created_at=1)
    a2 = Approval(run_id="r", created_at=2)
    store.save_approval(a1)
    store.save_approval(a2)
    assert store.pending_approval("r").id == a2.id
    a2.status = "resolved"
    store.save_approval(a2)
    assert store.pending_approval("r").id == a1.id
    a1.status = "resolved"
    store.save_approval(a1)
    assert store.pending_approval("r") is None
    assert store.latest_approval("r").id == a2.id
    assert store.get_approval(a1.id).status == "resolved"


def test_memories(store):
    m = store.add_memory(MemoryItem(workspace_id="w", text="x"))
    store.add_memory(MemoryItem(workspace_id="other", text="y"))
    assert [i.id for i in store.memories("w")] == [m.id]
    store.delete_memory(m.id)
    assert store.memories("w") == []


# ---- bus ------------------------------------------------------------------


async def collect(agen, n):
    out = []
    async for e in agen:
        out.append(e)
        if len(out) >= n:
            break
    await agen.aclose()
    return out


async def test_bus_replay_then_live_no_gaps(store):
    bus = LocalEventBus(store)
    for i in range(3):
        await bus.publish(ev("r", i=i))

    async def producer():
        for i in range(3, 10):
            await bus.publish(ev("r", i=i))
            await asyncio.sleep(0)

    task = asyncio.create_task(collect(bus.subscribe("r"), 10))
    await asyncio.sleep(0.01)
    await producer()
    got = await asyncio.wait_for(task, 2)
    assert [e.seq for e in got] == list(range(1, 11))


async def test_bus_reconnect_after_seq(store):
    bus = LocalEventBus(store)
    for i in range(5):
        await bus.publish(ev("r", i=i))
    task = asyncio.create_task(collect(bus.subscribe("r", after_seq=3), 3))
    await asyncio.sleep(0.01)
    await bus.publish(ev("r", i=5))
    await bus.publish(ev("other"))
    await bus.publish(ev("r", i=6))
    got = await asyncio.wait_for(task, 2)
    assert [e.seq for e in got] == [4, 5, 6] or [e.seq for e in got] == [4, 5, 6]
    assert [e.run_id for e in got] == ["r"] * 3


async def test_bus_race_dedupes(store):
    """An event stored between subscribe-registration and replay must not be delivered twice."""
    bus = LocalEventBus(store)
    await bus.publish(ev("r"))
    agen = bus.subscribe("r")
    first = await agen.__anext__()  # registers queue, replays seq 1
    await bus.publish(ev("r"))
    second = await agen.__anext__()
    assert (first.seq, second.seq) == (1, 2)
    await agen.aclose()
    assert "r" not in bus._subs


async def test_bus_slow_subscriber_dropped(store):
    bus = LocalEventBus(store, queue_size=2)
    agen = bus.subscribe("r")
    t = asyncio.create_task(agen.__anext__())
    await asyncio.sleep(0.01)
    for _ in range(6):
        await bus.publish(ev("r"))
    # publisher never blocked, subscriber dropped, stream ends after draining what it had
    got = [await t]
    async for e in agen:
        got.append(e)
    assert [e.seq for e in got] == sorted(e.seq for e in got)
    assert len(got) < 6
    assert len(store.events("r")) == 6

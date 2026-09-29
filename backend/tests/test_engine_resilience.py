"""Resilience: failure recovery, proof-of-done re-planning, budgets, the kill switch, crash reconciliation
through the effect ledger, rollback and loop detection."""
from __future__ import annotations

import asyncio

import pytest

from app.core.interfaces import ConflictError
from app.core.models import (
    Autonomy,
    Budget,
    ErrorKind,
    NodeStatus,
    RecoveryStrategy,
    RunStatus,
    ToolError,
    Verdict,
)
from app.llm.muse import set_fake_responder

try:
    from .test_engine_support import Crash, Harness, act, goal, plan_json
except ImportError:
    from test_engine_support import Crash, Harness, act, goal, plan_json


@pytest.fixture(autouse=True)
def _reset_muse():
    yield
    set_fake_responder(None)


def doc_plan(title: str = "Notes", criteria: tuple[str, ...] = ("A notes doc exists",)) -> dict:
    return plan_json(goal("g1", "Write notes", act("a1", "docs.create", {"title": title, "content_md": "body"}),
                          criteria=criteria))


# ---------------------------------------------------------------------------
# Failure recovery (SPEC §3)
# ---------------------------------------------------------------------------


async def test_transient_failure_is_retried_with_backoff_and_succeeds():
    plan = plan_json(goal("g1", "Read", act("a1", "gmail.search", {"query": "x"}),
                          act("a2", "docs.create", {"title": "t", "content_md": "c"}, "a1")))
    h = Harness({"plan": plan})
    h.tools["gmail.search"].run_failures = [ToolError(kind=ErrorKind.TRANSIENT, message="503", retryable=True)] * 2
    run = await h.start()
    assert run.status is RunStatus.COMPLETED
    a1 = run.plan.nodes["a1"]
    assert a1.status is NodeStatus.SUCCEEDED and a1.attempts == 3
    assert [d.strategy for d in a1.recovery] == [RecoveryStrategy.RETRY_SAME] * 2
    assert len(h.events(run.id, "recovery.decided")) == 2
    assert all(e.agent == "guardian" for e in h.events(run.id, "recovery.decided"))


async def test_invalid_args_from_the_tool_are_repaired_by_muse():
    h = Harness({"plan": doc_plan(), "repair_args": {"args": {"title": "Short", "content_md": "body"},
                                                     "note": "shortened the title"}})
    h.tools["docs.create"].sim_failures = [ToolError(kind=ErrorKind.INVALID_ARGS, message="title too long")]
    run = await h.start()
    assert run.status is RunStatus.COMPLETED
    a1 = run.plan.nodes["a1"]
    assert a1.args["title"] == "Short" and a1.recovery[0].strategy is RecoveryStrategy.REPAIR_ARGS
    assert h.world.sent("docs.create")[0]["args"]["title"] == "Short"
    assert h.judge.count("classify_failure") == 1 and h.muse.count("repair_args") == 1


async def test_wrong_template_path_is_repaired_without_asking_jev():
    plan = plan_json(goal(
        "g1", "Book",
        act("a1", "calendar.find_free_slots", {"attendees": ["ops@acme.dev"], "duration_min": 30}),
        act("a2", "calendar.create_event", {"title": "Sync", "start": "{{a1.output.free[0].start}}",
                                            "end": "{{a1.output.free[0].end}}", "attendees": ["ops@acme.dev"]})))
    fixed = {"title": "Sync", "start": "{{a1.output.slots[0].start}}", "end": "{{a1.output.slots[0].end}}",
             "attendees": ["ops@acme.dev"]}
    h = Harness({"plan": plan, "repair_args": {"args": fixed, "note": "use slots"}})
    h.judge.verdicts["calendar.create_event"] = Verdict.AUTO
    run = await h.start()
    assert run.status is RunStatus.COMPLETED
    assert h.judge.count("classify_failure") == 0  # the cause is certain: code detected it
    _, messages = next(c for c in h.muse.calls if c[0] == "repair_args")
    assert "available keys: [slots]" in messages[1]["content"]
    assert h.world.sent("calendar.create_event")[0]["args"]["start"] == "2026-10-06T10:00:00-07:00"


async def test_auth_failure_asks_a_human_then_retries_after_approval():
    h = Harness({"plan": doc_plan()})
    h.tools["docs.create"].sim_failures = [ToolError(kind=ErrorKind.AUTH, message="token expired")]
    run = await h.start()
    assert run.status is RunStatus.AWAITING_APPROVAL
    approval = h.store.pending_approval(run.id)
    assert approval.kind == "action" and approval.items[0].gate.model == "engine:recovery"
    assert "token expired" in approval.items[0].summary
    await h.orch.resolve_approval(run.id, approval.id, {"a1": "approved"}, {})
    run = await h.idle(run.id)
    assert run.status is RunStatus.COMPLETED and len(h.world.sent("docs.create")) == 1


async def test_not_found_switches_to_an_alternative_tool_when_jev_suggests_it():
    plan = plan_json(goal("g1", "Find", act("a1", "docs.read", {"doc_id": "d-1"}),
                          act("a2", "docs.create", {"title": "t", "content_md": "c"}, "a1")))
    h = Harness({"plan": plan, "repair_args": {"args": {"query": "contract"}, "note": "search instead"}})
    h.tools["docs.read"].run_failures = [ToolError(kind=ErrorKind.NOT_FOUND, message="no such doc")]
    h.judge.classify[ErrorKind.NOT_FOUND] = (RecoveryStrategy.SWITCH_TOOL, 0.8)
    run = await h.start()
    assert run.status is RunStatus.COMPLETED
    a1 = run.plan.nodes["a1"]
    assert a1.tool == "docs.search" and a1.recovery[0].strategy is RecoveryStrategy.SWITCH_TOOL


# ---------------------------------------------------------------------------
# Proof-of-done
# ---------------------------------------------------------------------------


async def test_verification_failure_reflects_and_replans_within_budget():
    replan = {"reason": "add the decisions", "success_criteria": [], "subgoals": [],
              "actions": [act("n1", "docs.create", {"title": "Notes v2", "content_md": "decisions"}, "a1")]}
    h = Harness({"plan": doc_plan(), "replan": replan})
    seen = {"g1": 0}

    def verify(goal_title: str, criteria: list[str], evidence: dict) -> bool:
        if goal_title == "Write notes":
            seen["g1"] += 1
            return seen["g1"] > 1
        return True

    h.judge.verify_fn = verify
    run = await h.start()
    assert run.status is RunStatus.COMPLETED, run.error
    assert run.metrics.replans == 1
    nodes = run.plan.nodes
    assert nodes["a1"].status is NodeStatus.SUCCEEDED and nodes["a1r2"].status is NodeStatus.SUCCEEDED
    assert nodes["a1r2"].revision == 2 and run.plan.revision == 2
    assert len(h.calls("docs.create")) == 2  # the kept step was not redone
    [revised] = h.events(run.id, "plan.revised")
    assert revised.data["added"] == ["a1r2"] and revised.agent == "planner"
    [reflection] = h.events(run.id, "reflection")
    assert reflection.data["agent"] == "planner" and "decisions section" in reflection.data["text"]
    assert nodes["g1"].verification.passed


async def test_verification_that_keeps_failing_stops_at_max_replans_and_fails_honestly():
    replan = {"reason": "retry", "success_criteria": [], "subgoals": [],
              "actions": [act("n1", "docs.create", {"title": "Again", "content_md": "x"}, "a1")]}
    h = Harness({"plan": doc_plan(), "replan": replan}, budget=Budget(max_replans=1))
    h.judge.verify_fn = lambda goal_title, criteria, evidence: False
    run = await h.start()
    assert run.status is RunStatus.FAILED and run.error.startswith("Could not verify")
    assert run.metrics.replans == 1
    assert [e.data["limit"] for e in h.events(run.id, "budget.exceeded")] == ["max_replans"]
    assert h.events(run.id)[-1].data["status"] == "failed"


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


async def test_tool_budget_exceeded_stops_cleanly():
    plan = plan_json(goal("g1", "Read a lot", *[act(f"a{i}", "gmail.search", {"query": f"q{i}"},
                                                    *([f"a{i - 1}"] if i > 1 else [])) for i in range(1, 5)]))
    h = Harness({"plan": plan}, budget=Budget(max_tool_calls=2))
    run = await h.start()
    assert run.status is RunStatus.FAILED and "max_tool_calls" in run.error
    [exceeded] = h.events(run.id, "budget.exceeded")
    assert exceeded.data == {"limit": "max_tool_calls", "value": 2, "max": 2}
    nodes = run.plan.nodes
    assert [nodes[f"a{i}"].status for i in range(1, 5)] == [NodeStatus.SUCCEEDED] * 2 + [NodeStatus.CANCELLED] * 2
    assert h.muse.count("summary") == 0 and "Stopped: budget exceeded" in run.summary
    assert h.events(run.id)[-1].type == "run.status"


async def test_llm_budget_is_enforced_before_spending():
    h = Harness({"plan": doc_plan()}, budget=Budget(max_llm_calls=1))
    run = await h.start()
    assert run.status is RunStatus.FAILED and "max_llm_calls" in run.error
    assert h.muse.count("plan") == 0  # refused before the call, not after


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


async def test_pause_stops_dispatch_lets_in_flight_finish_and_resume_continues():
    plan = plan_json(goal("g1", "Chain", act("a1", "gmail.search", {"query": "x"}),
                          act("a2", "docs.read", {"doc_id": "d-1"}, "a1"),
                          act("a3", "docs.create", {"title": "t", "content_md": "c"}, "a2")))
    h = Harness({"plan": plan})
    slow = h.tools["gmail.search"]
    slow.gate = asyncio.Event()
    run = await h.orch.start_run("ws-test", "go", Autonomy.BALANCED)
    await asyncio.wait_for(slow.started.wait(), 5)
    paused = await h.orch.pause(run.id)
    assert paused.status is RunStatus.PAUSED
    slow.gate.set()  # the in-flight call is allowed to finish...
    run = await h.idle(run.id)
    assert run.status is RunStatus.PAUSED
    assert run.plan.nodes["a1"].status is NodeStatus.SUCCEEDED
    assert run.plan.nodes["a2"].status is NodeStatus.PENDING and not h.calls("docs.read")  # ...nothing new starts
    with pytest.raises(ConflictError):
        await h.orch.pause(run.id)

    resumed = await h.orch.resume(run.id)
    assert resumed.status is RunStatus.SHADOWING
    run = await h.idle(run.id)
    assert run.status is RunStatus.COMPLETED and len(h.calls("docs.read")) == 1
    statuses = [e.data["status"] for e in h.events(run.id, "run.status")]
    assert statuses[:4] == ["planning", "shadowing", "paused", "shadowing"]


async def test_pause_while_awaiting_approval_resumes_to_waiting():
    plan = plan_json(goal("g1", "Mail", act("a1", "gmail.send", {"to": ["ops@acme.dev"], "subject": "s",
                                                               "body": "b"})))
    h = Harness({"plan": plan})
    run = await h.start()
    assert run.status is RunStatus.AWAITING_APPROVAL
    await h.orch.pause(run.id)
    run = await h.orch.resume(run.id)
    assert run.status is RunStatus.AWAITING_APPROVAL
    approval = h.store.pending_approval(run.id)
    await h.orch.resolve_approval(run.id, approval.id, {"a1": "approved"}, {})
    assert (await h.idle(run.id)).status is RunStatus.COMPLETED


# ---------------------------------------------------------------------------
# Effect ledger: idempotency across a crash
# ---------------------------------------------------------------------------


async def _crash_during_send(h: Harness, *, after_apply: bool) -> str:
    plan = plan_json(goal("g1", "Mail", act("a1", "gmail.send", {"to": ["ops@acme.dev"], "subject": "Hi",
                                                               "body": "Hello"})))
    h.muse.script["plan"] = [plan]
    h.judge.verdicts["gmail.send"] = Verdict.AUTO
    tool = h.tools["gmail.send"]
    tool.crash_after_apply, tool.crash_before_apply = after_apply, not after_apply
    run = await h.orch.start_run("ws-test", "send it", Autonomy.BALANCED)
    with pytest.raises(Crash):
        await h.idle(run.id)
    tool.crash_after_apply = tool.crash_before_apply = False
    stored = h.run(run.id)
    assert stored.status is RunStatus.EXECUTING and stored.plan.nodes["a1"].status is NodeStatus.RUNNING
    [fx] = h.store.effects(run.id)
    assert fx.status == "intent"  # the outbox row was written BEFORE the call
    return run.id


async def test_crash_after_the_effect_happened_is_reconciled_without_a_second_send():
    h = Harness()
    run_id = await _crash_during_send(h, after_apply=True)
    h2 = h.restart()
    await h2.orch.recover_unfinished()
    run = await h2.idle(run_id)
    assert run.status is RunStatus.COMPLETED
    assert [fx.status for fx in h.store.effects(run_id)] == ["applied"]
    assert len(h.world.sent("gmail.send")) == 1 and len(h.calls("gmail.send")) == 1  # never called again
    assert run.plan.nodes["a1"].result.output["reconciled"] is True


async def test_crash_before_the_effect_is_reconciled_as_not_applied_and_rerun_once():
    h = Harness()
    run_id = await _crash_during_send(h, after_apply=False)
    h2 = h.restart()
    await h2.orch.recover_unfinished()
    run = await h2.idle(run_id)
    assert run.status is RunStatus.COMPLETED
    assert len(h.world.sent("gmail.send")) == 1 and len(h.calls("gmail.send")) == 2


async def test_unknown_outcome_after_a_crash_asks_a_human_and_the_key_prevents_a_duplicate():
    h = Harness()
    run_id = await _crash_during_send(h, after_apply=True)
    h.tools["gmail.send"].reconcile_answer = None  # the world cannot be read back
    h2 = h.restart()
    await h2.orch.recover_unfinished()
    run = await h2.idle(run_id)
    assert run.status is RunStatus.AWAITING_APPROVAL
    approval = h.store.pending_approval(run_id)
    assert approval.kind == "action" and approval.items[0].gate.model == "engine:reconcile"
    assert [fx.status for fx in h.store.effects(run_id)] == ["unknown"]
    await h2.orch.resolve_approval(run_id, approval.id, {"a1": "approved"}, {})
    run = await h2.idle(run_id)
    assert run.status is RunStatus.COMPLETED
    assert len(h.calls("gmail.send")) == 2 and len(h.world.sent("gmail.send")) == 1  # same key → deduped


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


async def test_rollback_compensates_newest_first_and_reports_communications():
    plan = plan_json(goal(
        "g1", "Set up",
        act("a1", "docs.create", {"title": "Agenda", "content_md": "c"}),
        act("a2", "calendar.create_event", {"title": "Review", "start": "2026-10-06T10:00:00-07:00",
                                            "end": "2026-10-06T10:30:00-07:00", "attendees": ["ops@acme.dev"]}, "a1"),
        act("a3", "gmail.send", {"to": ["ops@acme.dev"], "subject": "Invite", "body": "See you"}, "a2")))
    h = Harness({"plan": plan})
    run = await h.start(autonomy=Autonomy.AUTONOMOUS)
    assert run.status is RunStatus.COMPLETED
    doc_id = h.world.sent("docs.create")[0]["id"]
    event_id = h.world.sent("calendar.create_event")[0]["id"]

    run = await h.orch.rollback(run.id)
    assert run.status is RunStatus.ROLLED_BACK
    assert h.world.compensated == [event_id, doc_id]  # newest first
    by_node = {fx.node_id: fx.status for fx in h.store.effects(run.id)}
    assert by_node == {"a1": "compensated", "a2": "compensated", "a3": "applied"}
    assert run.plan.nodes["a1"].status is NodeStatus.COMPENSATED and run.plan.nodes["a3"].status is NodeStatus.SUCCEEDED
    report = next(e for e in h.events(run.id, "log") if e.data["message"] == "Rollback finished")
    assert [x["effect"] for x in report.data["not_reversible"]] == ["communicate"]
    assert "Not reversible" in run.summary and "recipients were already notified" in run.summary
    assert len(h.events(run.id, "effect.compensated")) == 2
    assert h.events(run.id)[-1].data["status"] == "rolled_back"
    with pytest.raises(ConflictError):
        await h.orch.rollback(run.id)


# ---------------------------------------------------------------------------
# Loop detection
# ---------------------------------------------------------------------------


async def test_loop_detection_stops_identical_calls_across_replans():
    same = {"reason": "try again", "success_criteria": [], "subgoals": [],
            "actions": [act("n1", "docs.read", {"doc_id": "d-404"})]}
    plan = plan_json(goal("g1", "Read the doc", act("a1", "docs.read", {"doc_id": "d-404"})))
    h = Harness({"plan": plan, "replan": same}, budget=Budget(max_replans=5))
    h.tools["docs.read"].run_failures = [ToolError(kind=ErrorKind.NOT_FOUND, message="no such doc")] * 20
    run = await h.start()
    assert run.status is RunStatus.FAILED
    assert len(h.calls("docs.read")) == 3  # the 4th identical attempt is refused before it is made
    loops = [d for n in run.plan.nodes.values() for d in n.recovery if "loop detected" in d.note]
    assert loops and loops[0].strategy is RecoveryStrategy.ABORT


async def test_transient_muse_outage_pauses_the_run_and_resume_retries():
    def overloaded(messages):
        raise RuntimeError("Error code: 503 - service_overloaded: The backend is temporarily overloaded")

    h = Harness({"plan": [overloaded, doc_plan()]})
    run = await h.start()
    assert run.status is RunStatus.PAUSED and "resume to retry" in run.error
    run = await h.orch.resume(run.id)
    assert run.status is RunStatus.PLANNING and run.error == ""
    run = await h.idle(run.id)
    assert run.status is RunStatus.COMPLETED and h.muse.count("plan") == 2


async def test_verifier_sees_what_a_write_actually_delivered():
    h = Harness({"plan": doc_plan()})
    run = await h.start()
    assert run.status is RunStatus.COMPLETED
    evidence = next(kw["evidence"] for m, kw in h.judge.calls if m == "verify" and kw["goal"] == "Write notes")
    [write] = evidence["writes"]
    assert write["effect"]["applied"] and write["effect"]["content"]["content_md"] == "body"
    root = next(kw["evidence"] for m, kw in h.judge.calls if m == "verify" and kw["goal"] == "Complete the request")
    assert root["subgoal_results"][0]["passed"] is True  # proof composes bottom-up

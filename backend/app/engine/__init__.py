"""Adjutant's engine: Plan → Shadow → Gate → Approve → Commit → Verify → Reflect/Re-plan.

Public surface::

    from app.engine import build_orchestrator
    orch = build_orchestrator(store, bus, llm, judge, registry, workspaces)
    run = await orch.start_run(workspace_id, request, Autonomy.BALANCED)

Module map:
    orchestrator  public API, background tasks, pause/resume/cancel/rollback, crash recovery
    pipeline      the lifecycle as idempotent phases
    planner       every Muse call (intent, plan, replan, reflection, arg repair, summary, memory)
    prompts       system prompts, briefs and JSON schemas per purpose
    plan          plan tree construction, validation, graph queries, subtree re-planning
    templates     "{{a3.output.x}}" references: parsing, resolution, renaming
    binding       resolved args + the binding hash approvals are tied to
    taint         provenance / taint tracking over templates
    scheduler     concurrent DAG execution with checkpoints
    executor      one step in shadow or commit mode; loop detection
    ledger        effect ledger (transactional outbox), reconcile, compensation
    gate          injection pre-scan, action gate, plan-diff approvals
    recovery      failure classification → code policy → strategy
    verify        proof-of-done per goal, reflection, bounded re-plan
    completion    final report, memories, terminal status
"""
from .context import EngineConfig
from .errors import ApprovalNotFound, InvalidRunState, RunNotFound
from .orchestrator import AdjutantOrchestrator, build_orchestrator

__all__ = [
    "AdjutantOrchestrator",
    "ApprovalNotFound",
    "EngineConfig",
    "InvalidRunState",
    "RunNotFound",
    "build_orchestrator",
]

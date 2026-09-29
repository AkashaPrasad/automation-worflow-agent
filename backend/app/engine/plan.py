"""The plan tree as data: construction from Muse drafts, validation, graph queries and subtree re-planning.

Shape: root goal ``g0`` → sub-goals ``g1..`` → actions ``a1..``. A dependency on a *goal* means "every action in
that goal's subtree", and an action inherits its ancestors' dependencies. Templates add data-flow edges
automatically, so the planner cannot forget an ordering constraint it relies on.

Validation is code (not the model): tools must exist, required arguments must be present (templates count),
templates must point at real action steps, and the dependency graph must be acyclic. Muse gets one repair
round with the exact error list.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..core.models import ErrorKind, NodeKind, NodeStatus, Plan, PlanNode, ToolSpec
from . import argschema
from .templates import referenced_nodes, rename_refs

# ---------------------------------------------------------------------------
# Status groups
# ---------------------------------------------------------------------------

#: a dependency in one of these states will never produce an output in this run
DEAD_STATUSES = frozenset({NodeStatus.FAILED, NodeStatus.BLOCKED, NodeStatus.CANCELLED, NodeStatus.COMPENSATED})
#: statuses in which a step's (possibly simulated) output is usable during the shadow run
SHADOW_OUTPUT_STATUSES = frozenset({NodeStatus.SUCCEEDED, NodeStatus.SIMULATED, NodeStatus.READY,
                                    NodeStatus.AWAITING_APPROVAL})
#: an action that will not run again
SETTLED_STATUSES = frozenset({NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.SKIPPED, NodeStatus.BLOCKED,
                              NodeStatus.COMPENSATED, NodeStatus.CANCELLED})


# ---------------------------------------------------------------------------
# Drafts: lenient views of Muse's JSON
# ---------------------------------------------------------------------------


def _str_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if x is not None and str(x).strip()]
    return []


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")


class DraftAction(_Lenient):
    id: str = ""
    title: str = ""
    tool: str = ""
    args: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    optional: bool = False
    rationale: str = ""
    replaces: str = ""

    @field_validator("id", "title", "tool", "rationale", "replaces", mode="before")
    @classmethod
    def _s(cls, v: Any) -> str:
        return "" if v is None else str(v).strip()

    @field_validator("args", mode="before")
    @classmethod
    def _a(cls, v: Any) -> dict[str, Any]:
        return v if isinstance(v, dict) else {}

    @field_validator("depends_on", mode="before")
    @classmethod
    def _d(cls, v: Any) -> list[str]:
        return _str_list(v)

    @field_validator("optional", mode="before")
    @classmethod
    def _o(cls, v: Any) -> bool:
        return v.strip().lower() in ("true", "yes", "1") if isinstance(v, str) else bool(v)


class DraftGoal(_Lenient):
    id: str = ""
    title: str = ""
    success_criteria: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    rationale: str = ""
    actions: list[DraftAction] = Field(default_factory=list)

    @field_validator("id", "title", "rationale", mode="before")
    @classmethod
    def _s(cls, v: Any) -> str:
        return "" if v is None else str(v).strip()

    @field_validator("success_criteria", "depends_on", mode="before")
    @classmethod
    def _l(cls, v: Any) -> list[str]:
        return _str_list(v)


class DraftPlan(_Lenient):
    title: str = ""
    goal: str = ""
    success_criteria: list[str] = Field(default_factory=list)
    rationale: str = ""
    subgoals: list[DraftGoal] = Field(default_factory=list)
    actions: list[DraftAction] = Field(default_factory=list)  # tolerated: actions directly under the root

    @field_validator("title", "goal", "rationale", mode="before")
    @classmethod
    def _s(cls, v: Any) -> str:
        return "" if v is None else str(v).strip()

    @field_validator("success_criteria", mode="before")
    @classmethod
    def _l(cls, v: Any) -> list[str]:
        return _str_list(v)


class DraftReplan(_Lenient):
    reason: str = ""
    success_criteria: list[str] = Field(default_factory=list)
    subgoals: list[DraftGoal] = Field(default_factory=list)
    actions: list[DraftAction] = Field(default_factory=list)

    @field_validator("reason", mode="before")
    @classmethod
    def _s(cls, v: Any) -> str:
        return "" if v is None else str(v).strip()

    @field_validator("success_criteria", mode="before")
    @classmethod
    def _l(cls, v: Any) -> list[str]:
        return _str_list(v)

    @property
    def empty(self) -> bool:
        return not self.actions and not any(g.actions for g in self.subgoals)


# ---------------------------------------------------------------------------
# Graph queries
# ---------------------------------------------------------------------------


def ancestors(plan: Plan, node_id: str) -> list[str]:
    out: list[str] = []
    cur = plan.nodes.get(node_id)
    seen = {node_id}
    while cur is not None and cur.parent_id and cur.parent_id not in seen:
        out.append(cur.parent_id)
        seen.add(cur.parent_id)
        cur = plan.nodes.get(cur.parent_id)
    return out


def descendants(plan: Plan, node_id: str) -> list[str]:
    out: list[str] = []
    stack = list(reversed(plan.nodes[node_id].children)) if node_id in plan.nodes else []
    while stack:
        nid = stack.pop()
        if nid not in plan.nodes or nid in out:
            continue
        out.append(nid)
        stack.extend(reversed(plan.nodes[nid].children))
    return out


def subtree_actions(plan: Plan, node_id: str) -> list[str]:
    node = plan.nodes.get(node_id)
    if node is None:
        return []
    if node.kind is NodeKind.ACTION:
        return [node_id]
    return [d for d in descendants(plan, node_id) if plan.nodes[d].kind is NodeKind.ACTION]


def effective_deps(plan: Plan, node_id: str) -> set[str]:
    """Action ids that must settle before ``node_id`` may run (own + inherited deps, goals expanded)."""
    own_line = {node_id, *ancestors(plan, node_id)}
    deps: set[str] = set()
    for holder in own_line:
        holder_node = plan.nodes.get(holder)
        if holder_node is None:
            continue
        for dep in holder_node.depends_on:
            if dep in own_line or dep not in plan.nodes:
                continue
            deps.update(a for a in subtree_actions(plan, dep) if a != node_id)
    return deps


def upstream(plan: Plan, node_id: str) -> set[str]:
    """Transitive closure of ``effective_deps``: every action that is guaranteed to settle before this one."""
    seen: set[str] = set()
    stack = list(effective_deps(plan, node_id))
    while stack:
        nid = stack.pop()
        if nid in seen or nid == node_id:
            continue
        seen.add(nid)
        stack.extend(effective_deps(plan, nid))
    return seen


def find_cycle(plan: Plan) -> list[str] | None:
    graph = {a.id: effective_deps(plan, a.id) for a in plan.actions()}
    color: dict[str, int] = {}
    stack_path: list[str] = []

    def visit(n: str) -> list[str] | None:
        color[n] = 1
        stack_path.append(n)
        for d in sorted(graph.get(n, ())):
            if color.get(d) == 1:
                return stack_path[stack_path.index(d):] + [d]
            if color.get(d) is None:
                found = visit(d)
                if found:
                    return found
        stack_path.pop()
        color[n] = 2
        return None

    for n in graph:
        if color.get(n) is None:
            found = visit(n)
            if found:
                return found
    return None


def goals_bottom_up(plan: Plan) -> list[str]:
    out: list[str] = []

    def visit(nid: str) -> None:
        node = plan.nodes.get(nid)
        if node is None or node.kind is not NodeKind.GOAL:
            return
        for c in node.children:
            visit(c)
        out.append(nid)

    visit(plan.root_id)
    return out


def ref_closure(plan: Plan, node_id: str) -> set[str]:
    """Every step whose output (transitively, through templates) flows into ``node_id``'s arguments."""
    seen: set[str] = set()
    stack = list(referenced_nodes(plan.nodes[node_id].args)) if node_id in plan.nodes else []
    while stack:
        nid = stack.pop()
        if nid in seen or nid not in plan.nodes:
            continue
        seen.add(nid)
        stack.extend(referenced_nodes(plan.nodes[nid].args))
    return seen


@dataclass(frozen=True)
class DeadDependency:
    dep_id: str
    status: NodeStatus
    #: PERMISSION when the root cause is a human or policy decision (rejected / blocked), else PRECONDITION.
    #: Verification uses it to tell "not done on purpose" from "not done because something broke".
    kind: ErrorKind


def dead_dependency(plan: Plan, node: PlanNode) -> DeadDependency | None:
    """The first dependency that guarantees ``node`` can never run, if any.

    A skipped *optional* step does not doom dependents that merely wait for it; it does doom those that read
    its output."""
    refs = referenced_nodes(node.args)
    for dep_id in sorted(effective_deps(plan, node.id)):
        dep = plan.nodes.get(dep_id)
        if dep is None:
            continue
        if dep.status in DEAD_STATUSES:
            kind = ErrorKind.PERMISSION if dep.status is NodeStatus.BLOCKED else ErrorKind.PRECONDITION
            return DeadDependency(dep_id, dep.status, kind)
        if dep.status is NodeStatus.SKIPPED and (not dep.optional or dep_id in refs):
            reason = dep.result.error.kind if dep.result and dep.result.error else ErrorKind.PRECONDITION
            kind = ErrorKind.PERMISSION if reason is ErrorKind.PERMISSION else ErrorKind.PRECONDITION
            return DeadDependency(dep_id, dep.status, kind)
    return None


# ---------------------------------------------------------------------------
# Builder / validator
# ---------------------------------------------------------------------------


@dataclass
class Revision:
    revision: int
    goal_id: str
    added: list[str]
    removed: list[str]
    kept: list[str]
    rewired: dict[str, str]
    warnings: list[str] = field(default_factory=list)


class PlanBuilder:
    def __init__(self, spec_of: Callable[[str], ToolSpec | None]) -> None:
        self.spec_of = spec_of

    # -- construction --------------------------------------------------------
    def from_draft(self, draft: DraftPlan) -> tuple[Plan, list[str]]:
        root = PlanNode(id="g0", kind=NodeKind.GOAL, title=draft.goal or draft.title or "Complete the request",
                        success_criteria=draft.success_criteria, rationale=draft.rationale)
        plan = Plan(root_id="g0", revision=1, nodes={"g0": root})
        new_ids, _, warnings = self._materialize(plan, "g0", draft.subgoals, draft.actions, revision=1)
        warnings += self._normalize(plan, new_ids)
        return plan, warnings

    def _materialize(self, plan: Plan, parent_id: str, goals: list[DraftGoal], actions: list[DraftAction],
                     *, revision: int) -> tuple[list[str], dict[str, str], list[str]]:
        """Create nodes under ``parent_id`` with fresh, stable ids; rewrite draft ids inside templates and
        depends_on. Returns (new node ids in order, draft-id → new-id mapping, warnings)."""
        suffix = "" if revision <= 1 else f"r{revision}"
        counters = {"g": _max_index(plan, "g", suffix), "a": _max_index(plan, "a", suffix)}
        mapping: dict[str, str] = {}
        warnings: list[str] = []

        def alloc(prefix: str, draft_id: str) -> str:
            counters[prefix] += 1
            nid = f"{prefix}{counters[prefix]}{suffix}"
            if draft_id:
                if draft_id in mapping:
                    warnings.append(f"duplicate id '{draft_id}' in draft; references use the first one")
                else:
                    mapping[draft_id] = nid
            return nid

        planned: list[tuple[str, str, DraftGoal | DraftAction]] = []  # (new id, parent id, draft)
        for draft_action in actions:
            planned.append((alloc("a", draft_action.id), parent_id, draft_action))
        for goal in goals:
            gid = alloc("g", goal.id)
            planned.append((gid, parent_id, goal))
            for draft_action in goal.actions:
                planned.append((alloc("a", draft_action.id), gid, draft_action))

        new_ids: list[str] = []
        for nid, pid, d in planned:
            deps = [mapping.get(x, x) for x in d.depends_on]
            if isinstance(d, DraftGoal):
                node = PlanNode(id=nid, parent_id=pid, kind=NodeKind.GOAL, title=d.title or "Sub-goal",
                                rationale=d.rationale, success_criteria=d.success_criteria, depends_on=deps,
                                revision=revision)
            else:
                node = PlanNode(id=nid, parent_id=pid, kind=NodeKind.ACTION, title=d.title or d.tool,
                                rationale=d.rationale, tool=d.tool, args=rename_refs(d.args, mapping),
                                depends_on=deps, optional=d.optional, revision=revision)
            plan.nodes[nid] = node
            plan.nodes[pid].children.append(nid)
            new_ids.append(nid)
        return new_ids, mapping, warnings

    def _normalize(self, plan: Plan, node_ids: Iterable[str]) -> list[str]:
        """Auto-fixes that need no model: drop impossible deps, add template deps, coerce literal args."""
        warnings: list[str] = []
        for nid in node_ids:
            node = plan.nodes[nid]
            own_line = {nid, *ancestors(plan, nid)}
            deps: list[str] = []
            for dep in node.depends_on:
                if dep in own_line:
                    warnings.append(f"{nid}: dropped dependency on its own ancestor/self '{dep}'")
                elif dep not in plan.nodes:
                    warnings.append(f"{nid}: dropped dependency on unknown step '{dep}'")
                elif dep not in deps:
                    deps.append(dep)
            node.depends_on = deps
            if node.kind is NodeKind.ACTION:
                covered = effective_deps(plan, nid)
                for ref in sorted(referenced_nodes(node.args)):
                    if ref in plan.nodes and ref not in own_line and ref not in covered:
                        node.depends_on.append(ref)
                spec = self.spec_of(node.tool or "")
                if spec is not None:
                    node.args = argschema.coerce_args(node.args, spec.input_schema)
        return warnings

    # -- validation ----------------------------------------------------------
    def validate(self, plan: Plan, scope: Iterable[str] | None = None) -> list[str]:
        errors: list[str] = []
        if not plan.actions():
            errors.append("the plan has no actions")
        ids = list(scope) if scope is not None else list(plan.nodes)
        for nid in ids:
            node = plan.nodes.get(nid)
            if node is None or node.kind is not NodeKind.ACTION:
                continue
            errors.extend(self.validate_action(plan, node))
        cycle = find_cycle(plan)
        if cycle:
            errors.append("dependency cycle: " + " -> ".join(cycle))
        return errors

    def validate_action(self, plan: Plan, node: PlanNode) -> list[str]:
        errors: list[str] = []
        spec = self.spec_of(node.tool or "") if node.tool else None
        if not node.tool:
            errors.append(f"{node.id}: action has no tool")
        elif spec is None:
            errors.append(f"{node.id}: unknown tool '{node.tool}' (use an exact name from the catalogue)")
        else:
            errors.extend(f"{node.id} ({node.tool}): {p}"
                          for p in argschema.arg_problems(node.args, spec.input_schema, allow_templates=True))
        for ref in sorted(referenced_nodes(node.args)):
            target = plan.nodes.get(ref)
            if ref == node.id:
                errors.append(f"{node.id}: a template references the step's own output")
            elif target is None:
                errors.append(f"{node.id}: template references unknown step '{ref}'")
            elif target.kind is NodeKind.GOAL:
                errors.append(f"{node.id}: template references goal '{ref}', which has no output (use an action id)")
        return errors

    # -- re-planning ---------------------------------------------------------
    def apply_replan(self, plan: Plan, goal_id: str, draft: DraftReplan) -> Revision:
        """Replace the unfinished part of ``goal_id``'s subtree with ``draft`` (mutates ``plan``).

        Kept: succeeded (effects applied) and in-flight actions, verified sub-goals, and the goals that
        contain kept steps. Everything else under the goal is removed. References from outside the subtree to
        removed steps are re-pointed to the draft action that ``replaces`` them, or else to the goal itself
        (so they wait for the new work and their templates get repaired if still dangling).

        Deterministic, so the engine can apply it to a scratch copy for validation and then to the live plan."""
        revision = plan.revision + 1
        sub = descendants(plan, goal_id)
        keep: set[str] = set()
        for nid in sub:
            node = plan.nodes[nid]
            if node.kind is NodeKind.ACTION and node.status in (NodeStatus.SUCCEEDED, NodeStatus.RUNNING):
                keep.add(nid)
            elif node.kind is NodeKind.GOAL and node.status is NodeStatus.SUCCEEDED:
                keep.add(nid)
                keep.update(descendants(plan, nid))
        for nid in list(keep):
            keep.update(a for a in ancestors(plan, nid) if a in sub)
        removed = [nid for nid in sub if nid not in keep]
        for nid in removed:
            plan.nodes.pop(nid, None)
        for node in plan.nodes.values():
            node.children = [c for c in node.children if c in plan.nodes]

        new_ids, mapping, warnings = self._materialize(plan, goal_id, draft.subgoals, draft.actions,
                                                       revision=revision)
        removed_set = set(removed)
        rewired: dict[str, str] = {}
        for d in [*draft.actions, *(a for g in draft.subgoals for a in g.actions)]:
            if d.replaces in removed_set and d.id in mapping and d.replaces not in rewired:
                rewired[d.replaces] = mapping[d.id]

        new_set = set(new_ids)
        for nid, node in plan.nodes.items():
            dangling = [dep for dep in node.depends_on if dep in removed_set and dep not in rewired]
            if nid in new_set:
                node.args = rename_refs(node.args, rewired)
                node.depends_on = [rewired.get(dep, dep) for dep in node.depends_on]
                continue
            if not rewired and not dangling and not (referenced_nodes(node.args) & removed_set):
                continue
            node.args = rename_refs(node.args, rewired)
            deps = [rewired.get(dep, dep) for dep in node.depends_on if dep not in dangling]
            if dangling and goal_id not in ancestors(plan, nid) and goal_id not in deps:
                deps.append(goal_id)
            node.depends_on = deps

        goal = plan.nodes[goal_id]
        if draft.success_criteria:
            goal.success_criteria = draft.success_criteria
        for gid in [goal_id, *ancestors(plan, goal_id)]:
            g = plan.nodes[gid]
            g.status = NodeStatus.RUNNING
            g.verification = None
        plan.revision = revision
        warnings += self._normalize(plan, new_ids)
        return Revision(revision=revision, goal_id=goal_id, added=new_ids, removed=removed, kept=sorted(keep),
                        rewired=rewired, warnings=warnings)


def _max_index(plan: Plan, prefix: str, suffix: str) -> int:
    best = 0
    for nid in plan.nodes:
        if nid.startswith(prefix) and nid.endswith(suffix):
            core = nid[len(prefix): len(nid) - len(suffix) if suffix else None]
            if core.isdigit():
                best = max(best, int(core))
    return best


def plan_outline(plan: Plan) -> list[str]:
    """One line per step, indented by depth: stored as the playbook outline and shown to future planners."""
    lines: list[str] = []

    def visit(nid: str, depth: int) -> None:
        node = plan.nodes.get(nid)
        if node is None:
            return
        if node.kind is NodeKind.ACTION:
            lines.append(f"{'  ' * depth}- {node.title} [{node.tool}]")
        elif nid != plan.root_id:
            lines.append(f"{'  ' * depth}{node.title}")
        for c in node.children:
            visit(c, depth + (0 if nid == plan.root_id else 1))

    visit(plan.root_id, 0)
    return lines


def spec_lookup(specs: Mapping[str, ToolSpec]) -> Callable[[str], ToolSpec | None]:
    return lambda name: specs.get(name)

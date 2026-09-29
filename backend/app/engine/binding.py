"""Binding a step's templated args to concrete values, and the hash approvals are bound to.

Two views of the same call:

* ``actual`` -- what the tool receives (templates resolved against real or simulated outputs).
* ``hash``   -- the *binding hash*: ``args_hash`` over the args with references to other **write** steps kept
  symbolic. Those references are identities of objects this plan creates (a doc id, a page URL); in shadow they
  are simulated values, in commit the real ones. Everything else -- every word of an email body, every
  recipient -- is resolved into the hash. So "what you approved is what gets sent" holds for content, while the
  approval survives the simulated → real id swap. Any content change (a re-run draft, an edit, a repaired arg)
  changes the hash, and the engine re-gates.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..core.models import EffectClass, ErrorKind, NodeKind, NodeStatus, Plan, PlanNode, ToolSpec, args_hash
from . import argschema
from .errors import BindingError
from .plan import SHADOW_OUTPUT_STATUSES
from .templates import TEMPLATE_RE, referenced_nodes, resolve, symbol_table

SpecOf = Callable[[str], ToolSpec | None]


class Mode(str, Enum):
    SHADOW = "shadow"  # reads real, writes simulated
    COMMIT = "commit"  # writes real, through the ledger


@dataclass(frozen=True)
class BoundArgs:
    actual: dict[str, Any]
    hash: str


def is_write(spec: ToolSpec | None) -> bool:
    return spec is not None and spec.effect is not EffectClass.READ


def has_output(node: PlanNode, mode: Mode) -> bool:
    if node.result is None or not node.result.ok:
        return False
    if mode is Mode.COMMIT:
        return node.status is NodeStatus.SUCCEEDED
    return node.status in SHADOW_OUTPUT_STATUSES


def available_outputs(plan: Plan, mode: Mode) -> dict[str, Any]:
    return {nid: (n.result.output or {}) for nid, n in plan.nodes.items()
            if n.kind is NodeKind.ACTION and has_output(n, mode)}


def write_steps(plan: Plan, spec_of: SpecOf) -> set[str]:
    return {n.id for n in plan.actions() if is_write(spec_of(n.tool or ""))}


def bind(plan: Plan, node: PlanNode, spec_of: SpecOf, mode: Mode) -> BoundArgs:
    spec = spec_of(node.tool or "")
    schema = spec.input_schema if spec else {}
    refs = referenced_nodes(node.args)
    missing = sorted(r for r in refs if r not in plan.nodes)
    if missing:
        raise BindingError(ErrorKind.INVALID_ARGS,
                           f"arguments reference step(s) {', '.join(missing)} that no longer exist in the plan")
    outputs = available_outputs(plan, mode)
    resolved = resolve(node.args, outputs)
    empty = [k for k, v in node.args.items()
             if isinstance(v, str) and TEMPLATE_RE.fullmatch(v.strip()) and resolved.get(k) in (None, "", [])]
    if empty:
        # The planner wired data into these args and the upstream step produced nothing. Dropping the arg would
        # silently change the action (an invite with no attendees), so this is an error to repair.
        details = ", ".join(f"'{k}' ({node.args[k]})" for k in empty)
        raise BindingError(ErrorKind.INVALID_ARGS, f"{node.tool}: {details} resolved to an empty value")
    actual = argschema.coerce_args(resolved, schema)
    problems = argschema.arg_problems(actual, schema, allow_templates=False)
    if problems:
        raise BindingError(ErrorKind.INVALID_ARGS, f"{node.tool}: " + "; ".join(problems))
    symbolic = refs & write_steps(plan, spec_of)
    binding = argschema.coerce_args(resolve(node.args, outputs, symbolic=symbolic), schema) if symbolic else actual
    return BoundArgs(actual=actual, hash=args_hash(node.tool or "", binding))


def edit_symbols(plan: Plan, node: PlanNode, spec_of: SpecOf) -> dict[str, str]:
    """Simulated strings from write steps this node references → their templates (for user edits)."""
    symbolic = referenced_nodes(node.args) & write_steps(plan, spec_of)
    return symbol_table(available_outputs(plan, Mode.SHADOW), symbolic)

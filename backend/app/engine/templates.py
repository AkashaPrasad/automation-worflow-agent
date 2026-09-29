"""Template references between plan steps: ``"{{a3.output.text}}"``, ``"{{a5.output.slots[0].start}}"``.

Why templates rather than letting the planner inline content: the plan is written *before* any data is read
(CaMeL-style). The planner can only point at data it will never see. That keeps untrusted content out of the
planner's context, and it gives code exact data-flow edges for dependency order and taint tracking.

Semantics
    * whole-string template → the referenced value with its type (list, object, number...)
    * embedded template     → string interpolation (lists of scalars are joined with ", ")
    * ``[*]`` projects over a list: ``{{a2.output.attendees[*].email}}`` is the list of every attendee's email.
      This lets a plan (or an argument repair) reshape data without an llm.* step, so provenance stays exact.
    * ``.output`` is optional in the reference (``{{a3.text}}`` == ``{{a3.output.text}}``); the canonical form
      written back into plans always includes it.
"""
from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Collection, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from ..core.models import ErrorKind
from .errors import BindingError

TEMPLATE_RE = re.compile(r"\{\{\s*([A-Za-z][\w-]*)((?:\.[\w-]+|\[\s*(?:-?\d+|\*)\s*\])*)\s*\}\}")
_PATH_PART = re.compile(r"\.([\w-]+)|\[\s*(-?\d+|\*)\s*\]")


class _Star:
    """The ``[*]`` path part (a singleton, so it can never collide with a key or an index)."""

    def __repr__(self) -> str:
        return "[*]"


STAR = _Star()
PathPart = str | int | _Star


@dataclass(frozen=True)
class Ref:
    node_id: str
    path: tuple[PathPart, ...]

    @property
    def canonical(self) -> str:
        return "{{" + self.node_id + ".output" + format_path(self.path) + "}}"


def format_path(path: tuple[PathPart, ...]) -> str:
    return "".join("[*]" if p is STAR else f"[{p}]" if isinstance(p, int) else f".{p}" for p in path)


def _part(key: str, idx: str) -> PathPart:
    if idx == "*":
        return STAR
    return int(idx) if idx else key


def _ref(match: re.Match[str]) -> Ref:
    parts: list[PathPart] = [_part(key, idx) for key, idx in _PATH_PART.findall(match.group(2))]
    if parts and parts[0] == "output":
        parts = parts[1:]
    return Ref(match.group(1), tuple(parts))


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------


def iter_refs(value: Any) -> Iterator[Ref]:
    if isinstance(value, str):
        for m in TEMPLATE_RE.finditer(value):
            yield _ref(m)
    elif isinstance(value, dict):
        for v in value.values():
            yield from iter_refs(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from iter_refs(v)


def referenced_nodes(value: Any) -> set[str]:
    return {r.node_id for r in iter_refs(value)}


def has_template(value: Any) -> bool:
    return next(iter_refs(value), None) is not None


def rename_refs(value: Any, mapping: Mapping[str, str]) -> Any:
    """Rewrite node ids inside templates (used when plan ids are assigned or re-pointed by a re-plan).
    Every reference is also normalised to canonical form."""
    if isinstance(value, str):
        def sub(m: re.Match[str]) -> str:
            ref = _ref(m)
            return Ref(mapping.get(ref.node_id, ref.node_id), ref.path).canonical
        return TEMPLATE_RE.sub(sub, value)
    if isinstance(value, dict):
        return {k: rename_refs(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [rename_refs(v, mapping) for v in value]
    return value


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def lookup(outputs: Mapping[str, Any], ref: Ref) -> Any:
    """Follow ``ref.path`` into the referenced step's output, with errors that say what *is* there
    (so Muse can repair a wrong path from the message alone)."""
    if ref.node_id not in outputs:
        raise BindingError(ErrorKind.PRECONDITION,
                           f"{ref.canonical}: step {ref.node_id} has no output (it has not run, failed or was skipped)")
    return _walk(outputs[ref.node_id], ref.path, ref, [])


def _walk(cur: Any, path: tuple[PathPart, ...], ref: Ref, walked: list[PathPart]) -> Any:
    for i, part in enumerate(path):
        where = f"{ref.node_id}.output{format_path(tuple(walked))}"
        if part is STAR:
            if not isinstance(cur, list):
                raise BindingError(ErrorKind.INVALID_ARGS,
                                   f"{ref.canonical}: {where} is {type(cur).__name__}, not a list, so [*] cannot apply")
            return [_walk(item, path[i + 1:], ref, [*walked, j]) for j, item in enumerate(cur)]
        if isinstance(part, int):
            if not isinstance(cur, list):
                raise BindingError(ErrorKind.INVALID_ARGS,
                                   f"{ref.canonical}: {where} is {type(cur).__name__}, not a list")
            if not -len(cur) <= part < len(cur):
                # An empty result (no free slots, no matching email) is a world-state problem, not a typo.
                kind = ErrorKind.PRECONDITION if not cur else ErrorKind.INVALID_ARGS
                raise BindingError(kind, f"{ref.canonical}: {where} has {len(cur)} item(s); index [{part}] is out of range")
            cur = cur[part]
        else:
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            elif isinstance(cur, dict):
                keys = ", ".join(sorted(map(str, cur))[:25])
                raise BindingError(ErrorKind.INVALID_ARGS,
                                   f"{ref.canonical}: key '{part}' not found at {where}; available keys: [{keys}]")
            else:
                raise BindingError(ErrorKind.INVALID_ARGS,
                                   f"{ref.canonical}: {where} is {type(cur).__name__}, cannot read key '{part}'")
        walked.append(part)
    return cur


def stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list) and all(isinstance(v, (str, int, float)) for v in value):
        return ", ".join(stringify(v) for v in value)
    return json.dumps(value, ensure_ascii=False, default=str)


def resolve(value: Any, outputs: Mapping[str, Any], *, symbolic: Collection[str] = ()) -> Any:
    """Substitute templates with values from ``outputs`` ({node_id: output}).

    ``symbolic`` lists node ids whose references stay as canonical template text. The engine uses it to
    compute the *binding* hash of a write: references to other writes' outputs (ids/urls of objects this
    plan creates) are identities, not content, so they must not break an approval when the simulated id
    becomes the real one."""
    if isinstance(value, str):
        whole = TEMPLATE_RE.fullmatch(value.strip())
        if whole:
            ref = _ref(whole)
            return ref.canonical if ref.node_id in symbolic else copy.deepcopy(lookup(outputs, ref))

        def sub(m: re.Match[str]) -> str:
            ref = _ref(m)
            return ref.canonical if ref.node_id in symbolic else stringify(lookup(outputs, ref))

        return TEMPLATE_RE.sub(sub, value)
    if isinstance(value, dict):
        return {k: resolve(v, outputs, symbolic=symbolic) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, outputs, symbolic=symbolic) for v in value]
    return value


# ---------------------------------------------------------------------------
# Reverse mapping (user edits of previews)
# ---------------------------------------------------------------------------


def leaf_strings(value: Any, path: tuple[PathPart, ...] = ()) -> Iterator[tuple[tuple[PathPart, ...], str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from leaf_strings(v, path + (str(k),))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from leaf_strings(v, path + (i,))


def symbol_table(outputs: Mapping[str, Any], node_ids: Collection[str], *, min_len: int = 8) -> dict[str, str]:
    """{concrete simulated string: canonical template} for the given (write) steps' outputs."""
    table: dict[str, str] = {}
    for nid in node_ids:
        if nid not in outputs:
            continue
        for path, text in leaf_strings(outputs[nid]):
            if len(text) >= min_len and text not in table:
                table[text] = Ref(nid, path).canonical
    return table


def resymbolize(value: Any, symbols: Mapping[str, str]) -> Any:
    """Map concrete values that came from *simulated* write outputs back to their templates.

    A user editing an email body in the approval panel edits the preview, which contains e.g. the preview URL
    of a Notion page that does not exist yet. Without this, the edit would freeze the fake URL into the email."""
    if not symbols:
        return value
    if isinstance(value, str):
        for concrete in sorted(symbols, key=len, reverse=True):
            if concrete in value:
                value = value.replace(concrete, symbols[concrete])
        return value
    if isinstance(value, dict):
        return {k: resymbolize(v, symbols) for k, v in value.items()}
    if isinstance(value, list):
        return [resymbolize(v, symbols) for v in value]
    return value


# ---------------------------------------------------------------------------
# Per-argument provenance
# ---------------------------------------------------------------------------


def argument_taint(args: dict[str, Any], outputs: Mapping[str, Any], *, tainted: Callable[[str], bool],
                   endorsed: Callable[[str], bool]) -> list[str]:
    """Names of top-level args whose resolved value still carries untrusted data after endorsement.

    ``tainted(node_id)`` says whether a step's output derives from untrusted content (transitively through
    llm.* steps). Literals written by the planner are trusted: the planner never saw external data. A string
    that exactly equals a trusted value (the user, a known contact, an internal address, an existing Slack
    channel) is *endorsed*: an email address lifted from an inbound email is fine as a recipient when it is
    one the user already corresponds with, and suspicious when it is not. Lists are endorsed element-wise;
    the arg stays tainted if any element does. Structured values (objects, numbers) are never endorsed."""
    return [key for key, value in args.items() if _value_tainted(value, outputs, tainted, endorsed)]


def _value_tainted(value: Any, outputs: Mapping[str, Any], tainted: Callable[[str], bool],
                   endorsed: Callable[[str], bool]) -> bool:
    if isinstance(value, str):
        refs = [_ref(m) for m in TEMPLATE_RE.finditer(value)]
        if not any(tainted(r.node_id) for r in refs):
            return False
        try:
            resolved = resolve(value, outputs)
        except BindingError:
            return True  # cannot inspect the value: stay conservative
        return _leaf_tainted(resolved, endorsed)
    if isinstance(value, dict):
        return any(_value_tainted(v, outputs, tainted, endorsed) for v in value.values())
    if isinstance(value, list):
        return any(_value_tainted(v, outputs, tainted, endorsed) for v in value)
    return False


def _leaf_tainted(resolved: Any, endorsed: Callable[[str], bool]) -> bool:
    if resolved is None:
        return False
    if isinstance(resolved, str):
        return not endorsed(resolved)
    if isinstance(resolved, list):
        return any(_leaf_tainted(x, endorsed) for x in resolved)
    if isinstance(resolved, dict):
        return bool(resolved)
    return True

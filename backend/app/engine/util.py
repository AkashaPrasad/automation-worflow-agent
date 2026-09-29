"""Small helpers shared by engine modules (trimming data for prompts/events, JSON-safety, text similarity)."""
from __future__ import annotations

import json
import re
from enum import Enum
from typing import Any

from pydantic import BaseModel


def jsonable(value: Any) -> Any:
    """Deep, JSON-safe snapshot of ``value`` (pydantic models, enums and sets included).

    Events are snapshots: the plan keeps mutating after an event is queued, so we copy at emit time."""

    def _default(obj: Any) -> Any:
        if isinstance(obj, BaseModel):
            return obj.model_dump(mode="json")
        if isinstance(obj, Enum):
            return obj.value
        if isinstance(obj, (set, frozenset, tuple)):
            return list(obj)
        return str(obj)

    return json.loads(json.dumps(value, default=_default, ensure_ascii=False))


def to_json(value: Any, limit: int | None = None) -> str:
    text = json.dumps(jsonable(value), ensure_ascii=False, separators=(",", ":"))
    return truncate(text, limit) if limit else text


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def compact(value: Any, *, max_str: int = 600, max_list: int = 12, max_depth: int = 6, _depth: int = 0) -> Any:
    """Trim nested data for prompts, judgments and events: long strings are cut, long lists sampled and deep
    nesting summarised. Keeps the *shape* visible, which is what planners and verifiers need."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, str):
        return truncate(value, max_str)
    if isinstance(value, dict):
        if _depth >= max_depth:
            return f"<object with {len(value)} keys>"
        return {str(k): compact(v, max_str=max_str, max_list=max_list, max_depth=max_depth, _depth=_depth + 1)
                for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        seq = list(value)
        if _depth >= max_depth:
            return f"<list of {len(seq)}>"
        items = [compact(v, max_str=max_str, max_list=max_list, max_depth=max_depth, _depth=_depth + 1)
                 for v in seq[:max_list]]
        if len(seq) > max_list:
            items.append(f"… {len(seq) - max_list} more")
        return items
    return value


def compact_to_budget(value: Any, budget_chars: int) -> Any:
    """``compact`` with progressively tighter limits until the JSON fits ``budget_chars``."""
    for max_str, max_list in ((800, 15), (400, 10), (200, 6), (100, 4), (60, 3)):
        out = compact(value, max_str=max_str, max_list=max_list)
        if len(to_json(out)) <= budget_chars:
            return out
    return compact(value, max_str=40, max_list=2, max_depth=3)


_WORD = re.compile(r"[a-z0-9@._-]+")


def words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def similarity(a: str, b: str) -> float:
    """Jaccard similarity of word sets: a cheap, deterministic duplicate check for memories."""
    wa, wb = words(a), words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def shape(value: Any, *, max_depth: int = 5, _depth: int = 0) -> Any:
    """The structure of a value without its text: what Muse may see of untrusted data.

    Keys, types, list lengths, numbers, booleans and nulls survive (enough to fix a template path or notice
    that an extraction came back empty); strings do not (so injected instructions cannot reach the planner)."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, str):
        return f"<text: {len(value)} chars>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if _depth >= max_depth:
        return "<nested>"
    if isinstance(value, dict):
        return {str(k): shape(v, max_depth=max_depth, _depth=_depth + 1) for k, v in list(value.items())[:30]}
    if isinstance(value, (list, tuple)):
        if not value:
            return []
        head = shape(value[0], max_depth=max_depth, _depth=_depth + 1)
        return [head] if len(value) == 1 else [head, f"<... {len(value) - 1} more of the same shape>"]
    return f"<{type(value).__name__}>"


_QUOTED = re.compile(r"""(['"])(?:(?!\1).){4,}?\1""")


def redact_quoted(text: str) -> str:
    """Tool errors often echo the offending value ("'Dana <d@x.com>' is not a valid address"). When that value
    came from untrusted content, the planner lane gets the message with the value blanked out."""
    return _QUOTED.sub("<untrusted value>", text)

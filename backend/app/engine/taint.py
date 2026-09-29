"""Provenance and taint tracking (CaMeL-style).

A step's *output* is tainted when the tool itself returns external content (``output_trust == untrusted``) or
when any template input is tainted. ``llm.*`` steps are never sources: they only re-emit what they were given,
so they inherit taint from their inputs (SPEC §5). Data flows only through templates; plain ordering
dependencies carry no data, so they carry no taint.

Write steps are sinks, not conduits: their tainted inputs were already gated (and approved when needed), and
their outputs are the identities of objects Adjutant itself created (a doc id, a page URL). Posting the link to
a doc we just wrote is therefore not "control-tainted", even if the doc's content came from an email.

Beyond the boolean we keep *which* untrusted outputs flowed into a step, so the gate can show Jev the exact
external texts behind a write (``untrusted_context``), split per item (one text per email), which lets a
single hostile email be attributed precisely instead of tainting the whole inbox equally.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from ..core.models import EffectClass, Plan, ToolSpec, Trust
from .templates import Ref, iter_refs, referenced_nodes
from .util import truncate

SpecOf = Callable[[str], ToolSpec | None]


def is_untrusted_source(spec: ToolSpec | None) -> bool:
    if spec is None:
        return False
    if spec.app == "llm" or spec.name.startswith("llm."):
        return False
    return spec.output_trust is Trust.UNTRUSTED and spec.effect is EffectClass.READ


class Provenance:
    """Memoised source sets over the current plan (cheap to rebuild; plans are small)."""

    def __init__(self, plan: Plan, spec_of: SpecOf) -> None:
        self.plan = plan
        self.spec_of = spec_of
        self._out: dict[str, frozenset[str]] = {}

    def arg_sources(self, node_id: str) -> frozenset[str]:
        """Untrusted source steps whose content reaches ``node_id``'s arguments."""
        node = self.plan.nodes.get(node_id)
        if node is None:
            return frozenset()
        acc: set[str] = set()
        for ref in referenced_nodes(node.args):
            acc |= self.output_sources(ref, _stack=frozenset({node_id}))
        return frozenset(acc)

    def output_sources(self, node_id: str, _stack: frozenset[str] = frozenset()) -> frozenset[str]:
        if node_id in self._out:
            return self._out[node_id]
        node = self.plan.nodes.get(node_id)
        if node is None or node_id in _stack:
            return frozenset()
        spec = self.spec_of(node.tool or "")
        acc: set[str] = set()
        if spec is None or spec.effect is EffectClass.READ:
            for ref in referenced_nodes(node.args):
                acc |= self.output_sources(ref, _stack | {node_id})
        if is_untrusted_source(spec):
            acc.add(node_id)
        result = frozenset(acc)
        self._out[node_id] = result
        return result

    def args_tainted(self, node_id: str) -> bool:
        return bool(self.arg_sources(node_id))

    def arg_texts(self, node_id: str, _stack: frozenset[str] = frozenset()) -> frozenset[str]:
        """Untrusted *text ids* (one per email/event/hit) that reach ``node_id``'s arguments.

        Precise where the template is: ``{{a1.output.messages[0]}}`` contributes only ``a1#0``, so a reply
        built from one email is not judged against every other email of the same search. An llm.* step mixes
        all of its inputs, so it forwards everything it received."""
        node = self.plan.nodes.get(node_id)
        if node is None or node_id in _stack:
            return frozenset()
        acc: set[str] = set()
        for ref in iter_refs(node.args):
            src = self.plan.nodes.get(ref.node_id)
            src_spec = self.spec_of(src.tool or "") if src is not None else None
            if src is None or (src_spec is not None and src_spec.effect is not EffectClass.READ):
                continue  # writes are sinks
            if is_untrusted_source(src_spec):
                acc |= self._texts_for_ref(ref)
            else:
                acc |= self.arg_texts(ref.node_id, _stack | {node_id})
        return frozenset(acc)

    def _texts_for_ref(self, ref: Ref) -> set[str]:
        src = self.plan.nodes[ref.node_id]
        output = src.result.output if src.result is not None and src.result.ok else None
        texts = set(untrusted_texts(ref.node_id, output)) if output is not None else {ref.node_id}
        if len(ref.path) >= 2 and isinstance(ref.path[0], str) and isinstance(ref.path[1], int) \
                and isinstance(output, dict) and isinstance(output.get(ref.path[0]), list):
            items = output[ref.path[0]]
            index = ref.path[1] % len(items) if items else 0
            precise = f"{ref.node_id}#{index}"
            if precise in texts:
                return {precise}
        return texts

    def output_tainted(self, node_id: str) -> bool:
        return bool(self.output_sources(node_id))


def render_text(value: Any, limit: int) -> str:
    """Flatten one output item to text for scanning: string fields as "key: value" lines."""
    if isinstance(value, str):
        return truncate(value, limit)
    if isinstance(value, dict):
        lines = []
        for k, v in value.items():
            if isinstance(v, str):
                lines.append(f"{k}: {v}")
            elif isinstance(v, (list, dict)):
                inner = render_text(v, limit)
                if inner:
                    lines.append(f"{k}: {inner}")
        return truncate("\n".join(lines), limit)
    if isinstance(value, list):
        return truncate("\n".join(render_text(v, limit) for v in value), limit)
    return ""


def untrusted_texts(node_id: str, output: Any, *, limit: int = 2000, max_items: int = 25) -> dict[str, str]:
    """Split an untrusted output into scan units: a list of records (emails, events, search hits) becomes
    one text per record (``"a1#3"``); anything else is one text (``"a1"``)."""
    if isinstance(output, dict):
        for key, val in output.items():
            if isinstance(val, list) and val and all(isinstance(x, dict) for x in val):
                texts = {f"{node_id}#{i}": render_text(item, limit) for i, item in enumerate(val[:max_items])}
                rest = render_text({k: v for k, v in output.items() if k != key}, limit)
                if rest.strip():
                    texts[f"{node_id}#rest"] = rest
                return {k: v for k, v in texts.items() if v.strip()}
    text = render_text(output, limit)
    return {node_id: text} if text.strip() else {}


_DISPLAY_ADDRESS = re.compile(r'^\s*"?[^<>"]*"?\s*<\s*([^<>\s]+@[^<>\s]+)\s*>\s*$')


def bare_address(value: str) -> str:
    """``Dana Reyes <dana@northwind.com>`` → ``dana@northwind.com`` (anything else unchanged)."""
    m = _DISPLAY_ADDRESS.match(value)
    return m.group(1) if m else value


class Endorser:
    """Trusted values that endorse an otherwise-tainted string (see ``templates.argument_taint``)."""

    def __init__(self, profile: dict[str, Any], channels: list[str]) -> None:
        values = [profile.get("user_email"), *(profile.get("known_contacts") or [])]
        self.exact = {str(v).strip().lower() for v in values if isinstance(v, str) and v.strip()}
        for ch in channels:
            name = str(ch).strip().lower().lstrip("#")
            if name:
                self.exact |= {name, f"#{name}"}
        domain = str(profile.get("internal_domain") or "").strip().lower().lstrip("@")
        self.domain_suffix = f"@{domain}" if domain else None

    def __call__(self, value: str) -> bool:
        v = bare_address(value).strip().lower()
        if v in self.exact:
            return True
        return bool(self.domain_suffix and v.endswith(self.domain_suffix) and v.count("@") == 1
                    and " " not in v and len(v) > len(self.domain_suffix))


def slack_channels(snapshot: dict[str, Any]) -> list[str]:
    """Channel names from a workspace snapshot ({"slack": {channel: [...]}} or a list of channel records)."""
    slack = snapshot.get("slack") if isinstance(snapshot, dict) else None
    if isinstance(slack, dict):
        inner = slack.get("channels")
        if isinstance(inner, (dict, list)):
            slack = inner
    if isinstance(slack, dict):
        return [str(k) for k in slack]
    if isinstance(slack, list):
        return [str(c.get("name") or c.get("channel") or c.get("id")) for c in slack if isinstance(c, dict)]
    return []

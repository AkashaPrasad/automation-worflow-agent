"""Light JSON-Schema checks for tool arguments.

Tools validate their own input; the engine checks the part it can check *before* acting, so bad plans are
repaired early (at plan time for literals, at bind time for resolved templates) instead of failing in commit.
Only safe, meaning-preserving coercions are applied (``"a@x.com"`` → ``["a@x.com"]`` for an array field).
"""
from __future__ import annotations

import json
from typing import Any

from .templates import has_template

_PY_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def properties(schema: dict[str, Any] | None) -> dict[str, Any]:
    return dict((schema or {}).get("properties") or {})


def required(schema: dict[str, Any] | None) -> list[str]:
    return list((schema or {}).get("required") or [])


def expected_types(prop: dict[str, Any]) -> list[str]:
    t = prop.get("type")
    if isinstance(t, str):
        return [t]
    if isinstance(t, list):
        return [x for x in t if isinstance(x, str)]
    return []


def matches(value: Any, types: list[str]) -> bool:
    if not types:
        return True
    for t in types:
        if t == "null" and value is None:
            return True
        py = _PY_TYPES.get(t)
        if py is None:
            return True  # unknown type keyword: do not second-guess
        if isinstance(value, bool) and t in ("integer", "number"):
            continue
        if isinstance(value, py):
            return True
    return False


def coerce_value(value: Any, prop: dict[str, Any]) -> Any:
    types = expected_types(prop)
    if not types or matches(value, types) or has_template(value):
        return value
    if "array" in types and isinstance(value, (str, int, float, dict)) and value != "":
        return [value]
    if "integer" in types and isinstance(value, float) and value.is_integer():
        return int(value)
    if "integer" in types and isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    if "number" in types and isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return value
    if "string" in types and isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if "string" in types and isinstance(value, (dict, list)):
        # Structured data bound into a text field (an email object into llm.* "text"): serialise it rather
        # than fail -- the content is preserved and the model downstream reads JSON fine.
        return json.dumps(value, ensure_ascii=False, default=str)
    return value


ADDRESS_ARGS = frozenset({"to", "cc", "bcc", "attendees", "recipients", "email", "emails", "reply_to"})


def _is_address_arg(key: str, prop: dict[str, Any]) -> bool:
    items = prop.get("items") if isinstance(prop.get("items"), dict) else {}
    return key.lower() in ADDRESS_ARGS or prop.get("format") == "email" or items.get("format") == "email"


def _bare_addresses(value: Any) -> Any:
    from .taint import bare_address  # local import: taint imports templates, which imports us
    if isinstance(value, str):
        return bare_address(value)
    if isinstance(value, list):
        return [bare_address(v) if isinstance(v, str) else v for v in value]
    return value


def coerce_args(args: dict[str, Any], schema: dict[str, Any] | None) -> dict[str, Any]:
    props = properties(schema)
    out: dict[str, Any] = {}
    for key, value in args.items():
        if value is None and key not in required(schema):
            continue  # an explicit null for an optional argument means "not given"
        value = coerce_value(value, props[key]) if key in props else value
        if _is_address_arg(key, props.get(key, {})) and not has_template(value):
            value = _bare_addresses(value)  # "Dana Reyes <dana@x.com>" → "dana@x.com": same recipient
        out[key] = value
    return out


def arg_problems(args: dict[str, Any], schema: dict[str, Any] | None, *, allow_templates: bool) -> list[str]:
    """Human-readable problems: missing required arguments and literal type mismatches."""
    problems: list[str] = []
    props = properties(schema)
    for key in required(schema):
        value = args.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            problems.append(f"missing required argument '{key}'")
    for key, value in args.items():
        prop = props.get(key)
        if prop is None or (allow_templates and has_template(value)):
            continue
        types = expected_types(prop)
        if not matches(value, types):
            problems.append(f"argument '{key}' should be {'/'.join(types)}, got {type(value).__name__}")
    return problems


def compact_signature(schema: dict[str, Any] | None) -> str:
    """``{to: string[], cc?: string[], subject: string}`` -- short, LLM-friendly, "?" marks optional."""
    props = properties(schema)
    req = set(required(schema))
    parts = []
    for name, prop in props.items():
        parts.append(f"{name}{'' if name in req else '?'}: {_type_label(prop)}")
    return "{" + ", ".join(parts) + "}"


def _type_label(prop: dict[str, Any]) -> str:
    if "enum" in prop and isinstance(prop["enum"], list):
        return "|".join(repr(v) if not isinstance(v, str) else f'"{v}"' for v in prop["enum"][:8])
    types = expected_types(prop) or ["any"]
    labels = []
    for t in types:
        if t == "array":
            items = prop.get("items") if isinstance(prop.get("items"), dict) else {}
            inner = _type_label(items) if items else "any"
            labels.append(f"{inner}[]" if "|" not in inner else f"({inner})[]")
        elif t == "object" and isinstance(prop.get("properties"), dict):
            labels.append(compact_signature(prop))
        else:
            labels.append(t)
    return "|".join(labels)

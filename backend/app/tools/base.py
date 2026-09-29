"""BaseTool: shared plumbing for every Adjutant tool.

* JSON-schema argument validation (required keys, basic types, enums) that yields precise
  INVALID_ARGS messages the repair loop can feed back to Muse.
* Idempotency: a non-read tool called twice with the same ctx.idempotency_key returns the
  ORIGINAL result and does not act again.
* Errors are always returned as ToolResult(ok=False, error=...), never raised.
* latency_ms is measured here.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from app.core.models import (
    EffectClass,
    EffectRecord,
    ErrorKind,
    ToolContext,
    ToolError,
    ToolResult,
    ToolSpec,
    args_hash,
    now_ms,
)


class ToolFail(Exception):
    """Raised inside tool implementations; converted to ToolResult(ok=False) by BaseTool."""

    def __init__(self, kind: ErrorKind, message: str, retryable: bool | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retryable = (kind == ErrorKind.TRANSIENT) if retryable is None else retryable


def fail(kind: ErrorKind, message: str, retryable: bool | None = None) -> ToolResult:
    return ToolResult(
        ok=False,
        error=ToolError(kind=kind, message=message, retryable=(kind == ErrorKind.TRANSIENT) if retryable is None else retryable),
    )


def ok(output: dict[str, Any] | None = None, **kw: Any) -> ToolResult:
    return ToolResult(ok=True, output=output if output is not None else {}, **kw)


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------

_JSON_NAMES = {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "object", type(None): "null"}


def _tname(v: Any) -> str:
    return _JSON_NAMES.get(type(v), type(v).__name__)


def _coerce(value: Any, sch: dict[str, Any], path: str, errors: list[str]) -> Any:
    t = sch.get("type")
    if t is None:
        return value
    if t == "string":
        if not isinstance(value, str):
            errors.append(f"'{path}': expected string, got {_tname(value)}")
            return value
        if len(value) < sch.get("minLength", 0):
            errors.append(f"'{path}': must not be empty" if sch.get("minLength") == 1 else f"'{path}': shorter than {sch['minLength']} chars")
    elif t in ("integer", "number"):
        v = value
        if isinstance(v, str) and re.fullmatch(r"-?\d+(\.\d+)?", v.strip()):
            v = float(v) if "." in v else int(v)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            errors.append(f"'{path}': expected {t}, got {_tname(value)}")
            return value
        if t == "integer":
            if isinstance(v, float):
                if not v.is_integer():
                    errors.append(f"'{path}': expected integer, got {v}")
                    return value
                v = int(v)
        if "minimum" in sch and v < sch["minimum"]:
            errors.append(f"'{path}': must be >= {sch['minimum']}, got {v}")
        if "maximum" in sch and v > sch["maximum"]:
            errors.append(f"'{path}': must be <= {sch['maximum']}, got {v}")
        value = v
    elif t == "boolean":
        if isinstance(value, str) and value.lower() in ("true", "false"):
            value = value.lower() == "true"
        if not isinstance(value, bool):
            errors.append(f"'{path}': expected boolean, got {_tname(value)}")
    elif t == "array":
        if isinstance(value, str) and (sch.get("items") or {}).get("type") == "string":
            value = [value]  # tolerate a single string where a list of strings is expected
        if not isinstance(value, list):
            errors.append(f"'{path}': expected array, got {_tname(value)}")
            return value
        if len(value) < sch.get("minItems", 0):
            errors.append(f"'{path}': needs at least {sch['minItems']} item(s)")
        items = sch.get("items")
        if items:
            value = [_coerce(v, items, f"{path}[{i}]", errors) for i, v in enumerate(value)]
    elif t == "object":
        if not isinstance(value, dict):
            errors.append(f"'{path}': expected object, got {_tname(value)}")
            return value
        props = sch.get("properties")
        if props or sch.get("required"):
            value = _coerce_object(value, sch, path, errors)
    if "enum" in sch and value not in sch["enum"]:
        errors.append(f"'{path}': must be one of {sch['enum']}, got {value!r}")
    return value


def _coerce_object(obj: dict[str, Any], sch: dict[str, Any], path: str, errors: list[str]) -> dict[str, Any]:
    props = sch.get("properties", {})
    out = {k: v for k, v in obj.items() if v is not None}
    for req in sch.get("required", []):
        if req not in out:
            errors.append(f"missing required argument '{req}'" if not path else f"'{path}.{req}' is required")
    for k, psch in props.items():
        if k in out:
            out[k] = _coerce(out[k], psch, f"{path}.{k}" if path else k, errors)
        elif "default" in psch:
            out[k] = psch["default"]
    return out


def validate_args(schema: dict[str, Any], args: Any) -> tuple[dict[str, Any], str | None]:
    """Returns (normalized_args, error_message). Numeric strings are coerced to numbers and a bare
    string is accepted where a list of strings is expected; everything else must match the schema."""
    if not isinstance(args, dict):
        return {}, f"arguments must be a JSON object, got {_tname(args)}"
    errors: list[str] = []
    out = _coerce_object(args, schema, "", errors)
    if errors:
        allowed = ", ".join(schema.get("properties", {}))
        return out, "; ".join(errors) + f". Allowed arguments: {allowed}."
    return out, None


# ---------------------------------------------------------------------------
# BaseTool
# ---------------------------------------------------------------------------


class BaseTool:
    spec: ToolSpec

    def __init__(self) -> None:
        self._idem_cache: dict[str, ToolResult] = {}
        self._inflight: dict[str, asyncio.Lock] = {}

    # ---- hooks for subclasses -------------------------------------------------
    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:  # pragma: no cover - abstract
        raise NotImplementedError

    async def dry_run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if self.spec.effect == EffectClass.READ:
            return await self.execute(args, ctx)
        raise NotImplementedError(f"{self.spec.name} must implement dry_run")

    async def before(self, args: dict[str, Any], ctx: ToolContext, phase: str) -> None:
        """Called after validation, before acting (fault injection lives here). May raise ToolFail."""

    async def idem_get(self, ctx: ToolContext) -> ToolResult | None:
        return self._idem_cache.get(f"{ctx.workspace_id}:{ctx.idempotency_key}")

    async def idem_put(self, ctx: ToolContext, result: ToolResult) -> None:
        self._idem_cache[f"{ctx.workspace_id}:{ctx.idempotency_key}"] = result.model_copy(deep=True)

    # ---- Tool protocol --------------------------------------------------------
    def _uses_idempotency(self, ctx: ToolContext) -> bool:
        return self.spec.effect != EffectClass.READ and bool(ctx.idempotency_key)

    async def _guard(self, coro) -> ToolResult:
        try:
            return await coro
        except ToolFail as e:
            return fail(e.kind, e.message, e.retryable)
        except NotImplementedError as e:
            return fail(ErrorKind.UNKNOWN, str(e) or "not implemented")
        except Exception as e:  # never raise out of a tool
            return fail(ErrorKind.UNKNOWN, f"{type(e).__name__}: {e}")

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        t0 = time.perf_counter()
        norm, err = validate_args(self.spec.input_schema, args)
        if err:
            res = fail(ErrorKind.INVALID_ARGS, f"Invalid arguments for {self.spec.name}: {err}")
        elif self._uses_idempotency(ctx):
            lk_key = f"{ctx.workspace_id}:{ctx.idempotency_key}"
            lock = self._inflight.setdefault(lk_key, asyncio.Lock())
            async with lock:
                cached = await self.idem_get(ctx)
                if cached is not None:
                    res = cached.model_copy(deep=True)
                else:
                    res = await self._guard(self._act(norm, ctx, "run"))
                    if res.ok:
                        await self.idem_put(ctx, res)
            self._inflight.pop(lk_key, None)
        else:
            res = await self._guard(self._act(norm, ctx, "run"))
        res.latency_ms = int((time.perf_counter() - t0) * 1000)
        return res

    async def simulate(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        t0 = time.perf_counter()
        norm, err = validate_args(self.spec.input_schema, args)
        if err:
            res = fail(ErrorKind.INVALID_ARGS, f"Invalid arguments for {self.spec.name}: {err}")
        else:
            res = await self._guard(self._act(norm, ctx, "simulate"))
        res.latency_ms = int((time.perf_counter() - t0) * 1000)
        return res

    async def _act(self, args: dict[str, Any], ctx: ToolContext, phase: str) -> ToolResult:
        await self.before(args, ctx, phase)
        if phase == "simulate":
            return await self.dry_run(args, ctx)
        return await self.execute(args, ctx)

    async def compensate(self, effect: EffectRecord, ctx: ToolContext) -> ToolResult:
        return fail(ErrorKind.PRECONDITION, f"{self.spec.name} effects cannot be compensated automatically")

    async def reconcile(self, effect: EffectRecord, ctx: ToolContext) -> bool | None:
        return None

    # ---- helpers --------------------------------------------------------------
    def make_effect(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
        *,
        summary: str,
        target: dict[str, Any],
        preview: dict[str, Any],
        compensation: dict[str, Any] | None,
        simulated: bool,
    ) -> EffectRecord:
        return EffectRecord(
            run_id=ctx.run_id,
            node_id=ctx.node_id,
            tool=self.spec.name,
            app=self.spec.app,
            effect=self.spec.effect,
            idempotency_key=ctx.idempotency_key,
            args_hash=args_hash(self.spec.name, args),
            summary=summary,
            target=target,
            preview=preview,
            compensation=compensation if self.spec.compensable else None,
            status="simulated" if simulated else "applied",
            simulated=simulated,
            applied_at=None if simulated else now_ms(),
        )

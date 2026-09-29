"""Small local base for live connectors (independent of the sandbox's base.py).

A connector tool implements `_execute(args, ctx)` (real call) and, for writes,
`_preview(args, ctx)` (validation + read-only preconditions; never mutates).
This base handles: JSON-Schema arg validation, ToolError mapping, timing, and the
simulate/run/compensate/reconcile plumbing of the `Tool` Protocol.
"""
from __future__ import annotations

import time
from typing import Any

import jsonschema

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


class ConnectorError(Exception):
    """Raised inside a tool; converted to a ToolResult(ok=False) by the base class."""

    def __init__(self, kind: ErrorKind, message: str, retryable: bool = False):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retryable = retryable

    def to_error(self) -> ToolError:
        return ToolError(kind=self.kind, message=self.message, retryable=self.retryable)


def error_from_status(status: int, message: str = "") -> ConnectorError:
    """Map an HTTP status to a ConnectorError (401/403 AUTH/PERMISSION, 404, 409/412, 400/422, 429/5xx)."""
    msg = f"HTTP {status}: {message}".strip()[:500]
    if status == 401:
        return ConnectorError(ErrorKind.AUTH, msg)
    if status == 403:
        return ConnectorError(ErrorKind.PERMISSION, msg)
    if status == 404:
        return ConnectorError(ErrorKind.NOT_FOUND, msg)
    if status in (409, 412):
        return ConnectorError(ErrorKind.PRECONDITION, msg)
    if status in (400, 422):
        return ConnectorError(ErrorKind.INVALID_ARGS, msg)
    if status == 429 or status >= 500:
        return ConnectorError(ErrorKind.TRANSIENT, msg, retryable=True)
    return ConnectorError(ErrorKind.UNKNOWN, msg)


def map_exception(exc: Exception) -> ConnectorError:
    """Best-effort mapping for library exceptions (googleapiclient, httpx, timeouts)."""
    if isinstance(exc, ConnectorError):
        return exc
    status = None
    resp = getattr(exc, "resp", None)  # googleapiclient.errors.HttpError
    if resp is not None and getattr(resp, "status", None):
        status = int(resp.status)
    elif isinstance(getattr(exc, "status_code", None), int):
        status = exc.status_code  # type: ignore[attr-defined]
    elif isinstance(getattr(exc, "status", None), int):
        status = exc.status  # type: ignore[attr-defined]
    if status is not None:
        # Google returns 403 for rate limits ("rateLimitExceeded"/"userRateLimitExceeded")
        text = str(exc)
        if status == 403 and ("rateLimitExceeded" in text or "RateLimit" in text):
            return ConnectorError(ErrorKind.TRANSIENT, "rate limited", retryable=True)
        return error_from_status(status, _short(exc))
    name = type(exc).__name__
    if "Timeout" in name or "Connect" in name or "Network" in name or isinstance(exc, (TimeoutError, ConnectionError)):
        return ConnectorError(ErrorKind.TRANSIENT, f"{name}: {_short(exc)}", retryable=True)
    if "RefreshError" in name:
        return ConnectorError(ErrorKind.AUTH, "credentials could not be refreshed; reconnect the account")
    return ConnectorError(ErrorKind.UNKNOWN, f"{name}: {_short(exc)}")


def _short(exc: Exception) -> str:
    return str(exc).replace("\n", " ")[:300]


def obj_schema(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


STR = {"type": "string"}
STR_LIST = {"type": "array", "items": {"type": "string"}}


class ConnectorTool:
    spec: ToolSpec

    # -- to implement ------------------------------------------------------
    async def _execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raise NotImplementedError

    async def _preview(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Writes: validate + read-only precondition checks + predicted effect. Default: args-only preview."""
        return self.simulated_result({}, self.summarize(args), preview=self.preview_from_args(args), args=args)

    def summarize(self, args: dict[str, Any]) -> str:
        return f"{self.spec.name} {sorted(args)}"

    def preview_from_args(self, args: dict[str, Any]) -> dict[str, Any]:
        return dict(args)

    # -- helpers -----------------------------------------------------------
    @property
    def is_write(self) -> bool:
        return self.spec.effect != EffectClass.READ

    def validate(self, args: dict[str, Any]) -> None:
        try:
            jsonschema.validate(args, self.spec.input_schema)
        except jsonschema.ValidationError as e:
            path = ".".join(str(p) for p in e.absolute_path)
            raise ConnectorError(ErrorKind.INVALID_ARGS, f"{path or 'args'}: {e.message}"[:300]) from None

    def effect(self, args: dict[str, Any], ctx: ToolContext, *, summary: str, target: dict[str, Any] | None = None,
               preview: dict[str, Any] | None = None, compensation: dict[str, Any] | None = None,
               status: str = "applied") -> EffectRecord:
        return EffectRecord(
            run_id=ctx.run_id, node_id=ctx.node_id, tool=self.spec.name, app=self.spec.app,
            effect=self.spec.effect, idempotency_key=ctx.idempotency_key, args_hash=args_hash(self.spec.name, args),
            summary=summary, target=target or {}, preview=preview if preview is not None else dict(args),
            compensation=compensation, status=status,  # type: ignore[arg-type]
            simulated=status == "simulated", applied_at=now_ms() if status == "applied" else None,
        )

    def applied_result(self, output: dict[str, Any], args: dict[str, Any], ctx: ToolContext, *, summary: str,
                       target: dict[str, Any] | None = None, compensation: dict[str, Any] | None = None,
                       preview: dict[str, Any] | None = None) -> ToolResult:
        fx = self.effect(args, ctx, summary=summary, target=target, compensation=compensation, preview=preview)
        return ToolResult(ok=True, output=output, effect=fx)

    def simulated_result(self, output: dict[str, Any], summary: str, *, preview: dict[str, Any], args: dict[str, Any],
                         ctx: ToolContext | None = None, target: dict[str, Any] | None = None) -> ToolResult:
        ctx = ctx or ToolContext(run_id="", node_id="", workspace_id="", idempotency_key="")
        fx = self.effect(args, ctx, summary=summary, target=target, preview=preview, status="simulated")
        return ToolResult(ok=True, output={**output, "simulated": True}, simulated=True, effect=fx)

    def fail(self, exc: Exception) -> ToolResult:
        return ToolResult(ok=False, error=map_exception(exc).to_error())

    # -- Tool Protocol -----------------------------------------------------
    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        t0 = time.monotonic()
        try:
            self.validate(args)
            res = await self._execute(args, ctx)
        except Exception as exc:  # noqa: BLE001 - boundary: every failure becomes a typed ToolResult
            res = self.fail(exc)
        res.latency_ms = int((time.monotonic() - t0) * 1000)
        return res

    async def simulate(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if not self.is_write:
            return await self.run(args, ctx)
        t0 = time.monotonic()
        try:
            self.validate(args)
            res = await self._preview(args, ctx)
            if res.effect is not None:
                res.effect.run_id, res.effect.node_id = ctx.run_id, ctx.node_id
                res.effect.idempotency_key = ctx.idempotency_key
        except Exception as exc:  # noqa: BLE001
            res = self.fail(exc)
        res.latency_ms = int((time.monotonic() - t0) * 1000)
        return res

    async def compensate(self, effect: EffectRecord, ctx: ToolContext) -> ToolResult:
        try:
            if not self.spec.compensable:
                raise ConnectorError(ErrorKind.PRECONDITION, f"{self.spec.name} has no compensation")
            return await self._compensate(effect, ctx)
        except Exception as exc:  # noqa: BLE001
            return self.fail(exc)

    async def _compensate(self, effect: EffectRecord, ctx: ToolContext) -> ToolResult:
        raise ConnectorError(ErrorKind.PRECONDITION, "no compensation")

    async def reconcile(self, effect: EffectRecord, ctx: ToolContext) -> bool | None:
        return None

"""FastAPI application factory. Run with `uvicorn app.main:app`."""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.deps import Components
from app.api.ratelimit import SlidingWindowLimiter
from app.api.routes import VERSION, router
from app.config import get_settings

log = logging.getLogger("adjutant")


def _try(comp: Components, name: str, fn):
    try:
        setattr(comp, name, fn())
    except Exception as e:  # noqa: BLE001 - boot must survive a missing component
        comp.errors[name] = f"{type(e).__name__}: {e}"
        log.error("component %r unavailable: %s", name, comp.errors[name])


def build_components() -> Components:
    """Wire real components via lazy imports. Anything missing is logged and left as None."""
    s = get_settings()
    comp = Components(limiter=SlidingWindowLimiter(s.runs_per_hour_per_workspace))

    def _store():
        from app.store import get_store
        return get_store()

    def _bus():
        from app.store import get_bus
        return get_bus()

    def _llm():
        from app.llm.muse import get_llm
        return get_llm()

    def _judge():
        from app.judgment import get_judge
        return get_judge()

    def _registry():
        from app.tools import get_registry
        return get_registry()

    def _workspaces():
        from app.tools import get_workspace_store
        return get_workspace_store()

    for name, fn in (("store", _store), ("bus", _bus), ("llm", _llm), ("judge", _judge),
                     ("registry", _registry), ("workspaces", _workspaces)):
        _try(comp, name, fn)

    def _orch():
        from app.engine import build_orchestrator
        needed = ("store", "bus", "llm", "judge", "registry", "workspaces")
        missing = [n for n in needed if getattr(comp, n) is None]
        if missing:
            raise RuntimeError(f"missing dependencies: {', '.join(missing)}")
        return build_orchestrator(comp.store, comp.bus, comp.llm, comp.judge, comp.registry, comp.workspaces)

    _try(comp, "orchestrator", _orch)
    return comp


class _RequestLogMiddleware:
    """Pure ASGI (does not buffer SSE): request logging with timing + JSON 500s without stack traces."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        t0 = time.perf_counter()
        status = {"code": 0}
        started = {"v": False}

        async def send_wrap(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                started["v"] = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrap)
        except Exception:  # noqa: BLE001
            log.exception("unhandled error on %s %s", scope["method"], scope["path"])
            status["code"] = 500
            if not started["v"]:
                resp = JSONResponse({"detail": "internal server error"}, status_code=500)
                await resp(scope, receive, send)
        finally:
            log.info("%s %s -> %s (%.0f ms)", scope["method"], scope["path"], status["code"],
                     (time.perf_counter() - t0) * 1000)


def create_app(components: Components | None = None) -> FastAPI:
    """`components`: inject fakes (tests). When omitted, real components are built in the lifespan."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if components is None:
            comp = build_components()
            app.state.components = comp
            try:
                from app.tools.mcp_adapter import load_mcp_servers
                if comp.judge is not None:
                    await load_mcp_servers(comp.judge)
            except Exception as e:  # noqa: BLE001
                log.warning("MCP servers not loaded: %s", e)
            if comp.orchestrator is not None:
                try:
                    await comp.orchestrator.recover_unfinished()
                except Exception:  # noqa: BLE001
                    log.exception("recover_unfinished failed")
        yield

    app = FastAPI(title="Adjutant", version=VERSION, lifespan=lifespan)
    app.state.components = components or Components(limiter=SlidingWindowLimiter(get_settings().runs_per_hour_per_workspace))
    if components is not None and components.limiter is None:
        components.limiter = SlidingWindowLimiter(get_settings().runs_per_hour_per_workspace)

    app.add_middleware(_RequestLogMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=get_settings().cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["Content-Type", "X-Workspace-Id", "Last-Event-ID", "Authorization", "Accept"],
    )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.exception("unhandled error: %s", type(exc).__name__)
        return JSONResponse({"detail": "internal server error"}, status_code=500)

    app.include_router(router)
    try:
        from app.api.oauth import router as oauth_router
        app.include_router(oauth_router)
    except ImportError as e:
        log.info("oauth router not available: %s", e)
    except Exception as e:  # noqa: BLE001
        log.warning("oauth router failed to load: %s", e)
    return app


app = create_app()

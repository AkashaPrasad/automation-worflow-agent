"""SandboxWorkspaceStore: one JSON world per workspace_id, persisted in SQLite.

Table `sandbox_worlds(workspace_id TEXT PRIMARY KEY, data TEXT, updated_at INTEGER)` in the shared
database file (other components own their own tables there). Mutations are load-modify-save inside a
per-workspace re-entrant lock via `transaction()`.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import get_settings

from .seed import USER_EMAIL, USER_NAME, USER_TITLE, INTERNAL_DOMAIN, build_world
from .timeutil import TIMEZONE, workspace_tz

# Keys of the world that are internal bookkeeping (not shown in snapshots).
_INTERNAL_KEYS = {"faults", "idem", "audit", "doc_history", "busy", "known_contacts", "people", "seeded_at"}


class SandboxWorkspaceStore:
    def __init__(self, db_path: str | None = None, clock: Callable[[], datetime] | None = None):
        path = db_path or get_settings().database_path
        if path != ":memory:":
            Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._clock = clock
        self._db_lock = threading.RLock()
        self._ws_locks: dict[str, threading.RLock] = defaultdict(threading.RLock)
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=10, isolation_level=None)
        with self._db_lock:
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
            self._conn.execute("PRAGMA busy_timeout=8000")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS sandbox_worlds("
                "workspace_id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at INTEGER NOT NULL)"
            )

    # ---- clock ----------------------------------------------------------------
    def now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(workspace_tz())

    # ---- persistence ----------------------------------------------------------
    def _ws_lock(self, ws: str) -> threading.RLock:
        with self._db_lock:
            return self._ws_locks[ws]

    def _load(self, ws: str) -> dict[str, Any]:
        with self._db_lock:
            row = self._conn.execute("SELECT data FROM sandbox_worlds WHERE workspace_id=?", (ws,)).fetchone()
        if row:
            return json.loads(row[0])
        world = build_world(self.now())
        self._save(ws, world)
        return world

    def _save(self, ws: str, world: dict[str, Any]) -> None:
        blob = json.dumps(world, ensure_ascii=False, default=str)
        with self._db_lock:
            self._conn.execute(
                "INSERT INTO sandbox_worlds(workspace_id, data, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(workspace_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                (ws, blob, int(time.time())),
            )

    def read(self, ws: str) -> dict[str, Any]:
        """A private copy of the world (lazily seeded)."""
        with self._ws_lock(ws):
            return self._load(ws)

    @contextmanager
    def transaction(self, ws: str) -> Iterator[dict[str, Any]]:
        """Load-modify-save under the workspace lock. Nothing is saved if the body raises."""
        with self._ws_lock(ws):
            world = self._load(ws)
            yield world
            self._save(ws, world)

    # ---- WorkspaceStore protocol ---------------------------------------------
    def snapshot(self, workspace_id: str) -> dict[str, Any]:
        w = self.read(workspace_id)
        inbox = sorted(w["mail"], key=lambda m: m["date"], reverse=True)
        docs = [{k: v for k, v in d.items()} for d in w["docs"] if not d.get("trashed")]
        return {
            "profile": self.profile(workspace_id),
            "mail": inbox,
            "sent": sorted(w["sent"], key=lambda m: m["date"], reverse=True),
            "drafts": w["drafts"],
            "calendar": sorted(w["calendar"], key=lambda e: e["start"]),
            "docs": docs,
            "sheets": w["sheets"],
            "notion": [p for p in w["notion"] if not p.get("archived")],
            "slack": w["slack"],
            "meetings": sorted(w["meetings"], key=lambda m: m["date"], reverse=True),
            "outbox": sorted(w["outbox"], key=lambda o: o["at"], reverse=True),
        }

    def reset(self, workspace_id: str) -> dict[str, Any]:
        with self._ws_lock(workspace_id):
            self._save(workspace_id, build_world(self.now()))
        return self.snapshot(workspace_id)

    def profile(self, workspace_id: str) -> dict[str, Any]:
        w = self.read(workspace_id)
        return {
            "user_email": USER_EMAIL,
            "user_name": USER_NAME,
            "title": USER_TITLE,
            "internal_domain": INTERNAL_DOMAIN,
            "known_contacts": sorted(set(w["known_contacts"])),
            "timezone": TIMEZONE,
            "now_iso": self.now().isoformat(timespec="seconds"),
        }

    # ---- fault injection ------------------------------------------------------
    def inject_fault(
        self, workspace_id: str, tool: str, kind: str = "transient", message: str = "503 Service Unavailable",
        times: int = 1, phase: str = "any",
    ) -> None:
        """The next `times` calls of `tool` (phase: any | run | simulate) fail with ToolError(kind, message)."""
        with self.transaction(workspace_id) as w:
            w.setdefault("faults", {}).setdefault(tool, []).append(
                {"kind": kind, "message": message, "times": int(times), "phase": phase}
            )

    def clear_faults(self, workspace_id: str) -> None:
        with self.transaction(workspace_id) as w:
            w["faults"] = {}


def inject_fault(
    workspace_id: str, tool: str, kind: str = "transient", message: str = "503 Service Unavailable",
    times: int = 1, phase: str = "any", store: SandboxWorkspaceStore | None = None,
) -> None:
    """Deterministic fault injection for tests/demos: make `tool` fail `times` times, then behave."""
    if store is None:
        from app.tools import get_workspace_store

        store = get_workspace_store()  # type: ignore[assignment]
    store.inject_fault(workspace_id, tool, kind, message, times, phase)  # type: ignore[union-attr]

"""SQLite implementation of RunStore (stdlib sqlite3, WAL, one lock)."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from app.config import get_settings
from app.core.models import (
    TERMINAL_STATUSES,
    Approval,
    EffectRecord,
    MemoryItem,
    Run,
    RunEvent,
    RunSummary,
    now_ms,
)

_TERMINAL = tuple(s.value for s in TERMINAL_STATUSES)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, status TEXT NOT NULL,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_runs_ws ON runs(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE TABLE IF NOT EXISTS events (
  run_id TEXT NOT NULL, seq INTEGER NOT NULL, ts INTEGER NOT NULL, type TEXT NOT NULL, data TEXT NOT NULL,
  PRIMARY KEY (run_id, seq));
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_approvals_run ON approvals(run_id, created_at);
CREATE TABLE IF NOT EXISTS effects (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, idempotency_key TEXT UNIQUE, created_at INTEGER NOT NULL,
  data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_effects_run ON effects(run_id, created_at);
CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, created_at INTEGER NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_memories_ws ON memories(workspace_id, created_at);
"""


class SqliteRunStore:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or get_settings().database_path
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=30, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute("PRAGMA busy_timeout=30000")
            self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- runs ---------------------------------------------------------------
    def _write_run(self, run: Run) -> None:
        self._db.execute(
            "INSERT INTO runs (id, workspace_id, status, created_at, updated_at, data) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at, "
            "data=excluded.data",
            (run.id, run.workspace_id, run.status.value, run.created_at, run.updated_at, run.model_dump_json()),
        )

    def create_run(self, run: Run) -> Run:
        with self._lock:
            self._write_run(run)
        return run

    def save_run(self, run: Run) -> None:
        run.updated_at = now_ms()
        with self._lock:
            self._write_run(run)

    def get_run(self, run_id: str) -> Run | None:
        with self._lock:
            row = self._db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
        return Run.model_validate_json(row["data"]) if row else None

    def list_runs(self, workspace_id: str, limit: int = 50) -> list[RunSummary]:
        with self._lock:
            rows = self._db.execute(
                "SELECT data FROM runs WHERE workspace_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (workspace_id, limit),
            ).fetchall()
        out = []
        for r in rows:
            run = Run.model_validate_json(r["data"])
            out.append(RunSummary(
                id=run.id, title=run.title, request=run.request, status=run.status, autonomy=run.autonomy,
                created_at=run.created_at, updated_at=run.updated_at, metrics=run.metrics,
            ))
        return out

    def unfinished_runs(self) -> list[Run]:
        marks = ",".join("?" * len(_TERMINAL))
        with self._lock:
            rows = self._db.execute(
                f"SELECT data FROM runs WHERE status NOT IN ({marks}) ORDER BY created_at", _TERMINAL
            ).fetchall()
        return [Run.model_validate_json(r["data"]) for r in rows]

    # -- events -------------------------------------------------------------
    def append_event(self, event: RunEvent) -> RunEvent:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT COALESCE(MAX(seq),0)+1 AS n FROM events WHERE run_id=?",
                                       (event.run_id,)).fetchone()
                event.seq = int(row["n"])
                self._db.execute(
                    "INSERT INTO events (run_id, seq, ts, type, data) VALUES (?,?,?,?,?)",
                    (event.run_id, event.seq, event.ts, event.type, event.model_dump_json()),
                )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return event

    def events(self, run_id: str, after_seq: int = 0) -> list[RunEvent]:
        with self._lock:
            rows = self._db.execute(
                "SELECT data FROM events WHERE run_id=? AND seq>? ORDER BY seq", (run_id, after_seq)
            ).fetchall()
        return [RunEvent.model_validate_json(r["data"]) for r in rows]

    # -- approvals ----------------------------------------------------------
    def save_approval(self, approval: Approval) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO approvals (id, run_id, status, created_at, data) VALUES (?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET status=excluded.status, data=excluded.data",
                (approval.id, approval.run_id, approval.status, approval.created_at, approval.model_dump_json()),
            )

    def get_approval(self, approval_id: str) -> Approval | None:
        with self._lock:
            row = self._db.execute("SELECT data FROM approvals WHERE id=?", (approval_id,)).fetchone()
        return Approval.model_validate_json(row["data"]) if row else None

    def pending_approval(self, run_id: str) -> Approval | None:
        with self._lock:
            row = self._db.execute(
                "SELECT data FROM approvals WHERE run_id=? AND status='pending' ORDER BY created_at DESC, rowid DESC",
                (run_id,),
            ).fetchone()
        return Approval.model_validate_json(row["data"]) if row else None

    def latest_approval(self, run_id: str) -> Approval | None:
        """Extra (not in the RunStore protocol): pending one if any, else the most recent."""
        pending = self.pending_approval(run_id)
        if pending:
            return pending
        with self._lock:
            row = self._db.execute(
                "SELECT data FROM approvals WHERE run_id=? ORDER BY created_at DESC, rowid DESC", (run_id,)
            ).fetchone()
        return Approval.model_validate_json(row["data"]) if row else None

    # -- effects ------------------------------------------------------------
    def upsert_effect(self, effect: EffectRecord) -> None:
        key = effect.idempotency_key or None
        sql = (
            "INSERT INTO effects (id, run_id, idempotency_key, created_at, data) VALUES (?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET run_id=excluded.run_id, idempotency_key=excluded.idempotency_key, "
            "data=excluded.data"
        )
        args = (effect.id, effect.run_id, key, effect.created_at, effect.model_dump_json())
        with self._lock:
            try:
                self._db.execute(sql, args)
            except sqlite3.IntegrityError:
                # same idempotency key under a different id: the newer record supersedes the old row
                self._db.execute("DELETE FROM effects WHERE idempotency_key=? AND id<>?", (key, effect.id))
                self._db.execute(sql, args)

    def effects(self, run_id: str) -> list[EffectRecord]:
        with self._lock:
            rows = self._db.execute(
                "SELECT data FROM effects WHERE run_id=? ORDER BY created_at, rowid", (run_id,)
            ).fetchall()
        return [EffectRecord.model_validate_json(r["data"]) for r in rows]

    def effect_by_key(self, idempotency_key: str) -> EffectRecord | None:
        if not idempotency_key:
            return None
        with self._lock:
            row = self._db.execute("SELECT data FROM effects WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        return EffectRecord.model_validate_json(row["data"]) if row else None

    # -- memories -----------------------------------------------------------
    def add_memory(self, item: MemoryItem) -> MemoryItem:
        with self._lock:
            self._db.execute(
                "INSERT INTO memories (id, workspace_id, created_at, data) VALUES (?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (item.id, item.workspace_id, item.created_at, item.model_dump_json()),
            )
        return item

    def memories(self, workspace_id: str) -> list[MemoryItem]:
        with self._lock:
            rows = self._db.execute(
                "SELECT data FROM memories WHERE workspace_id=? ORDER BY created_at, rowid", (workspace_id,)
            ).fetchall()
        return [MemoryItem.model_validate_json(r["data"]) for r in rows]

    def delete_memory(self, memory_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM memories WHERE id=?", (memory_id,))

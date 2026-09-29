"""Per-workspace OAuth token storage (SQLite, WAL). Encrypted with Fernet when OAUTH_ENC_KEY is set."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import time
from typing import Any

from app.config import get_settings

try:  # optional
    from cryptography.fernet import Fernet, InvalidToken
except Exception:  # pragma: no cover
    Fernet = None  # type: ignore[assignment,misc]
    InvalidToken = Exception  # type: ignore[assignment,misc]


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(get_settings().database_path, timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute(
        "CREATE TABLE IF NOT EXISTS oauth_tokens (workspace_id TEXT NOT NULL, provider TEXT NOT NULL, "
        "data TEXT NOT NULL, updated_at INTEGER NOT NULL, PRIMARY KEY (workspace_id, provider))"
    )
    return c


def _fernet() -> Any | None:
    secret = os.getenv("OAUTH_ENC_KEY", "")
    if not secret or Fernet is None:
        return None
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def encryption_enabled() -> bool:
    return _fernet() is not None


def save_token(workspace_id: str, provider: str, data: dict[str, Any]) -> None:
    blob = json.dumps(data)
    f = _fernet()
    stored = "fernet:" + f.encrypt(blob.encode()).decode() if f else "plain:" + blob
    c = _conn()
    try:
        with c:
            c.execute(
                "INSERT INTO oauth_tokens(workspace_id, provider, data, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(workspace_id, provider) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                (workspace_id, provider, stored, int(time.time() * 1000)),
            )
    finally:
        c.close()


def load_token(workspace_id: str, provider: str) -> dict[str, Any] | None:
    c = _conn()
    try:
        row = c.execute(
            "SELECT data FROM oauth_tokens WHERE workspace_id=? AND provider=?", (workspace_id, provider)
        ).fetchone()
    finally:
        c.close()
    if not row:
        return None
    raw: str = row[0]
    try:
        if raw.startswith("fernet:"):
            f = _fernet()
            if f is None:
                return None  # encrypted but no key available
            return json.loads(f.decrypt(raw[7:].encode()).decode())
        return json.loads(raw.removeprefix("plain:"))
    except (InvalidToken, ValueError):
        return None


def delete_token(workspace_id: str, provider: str) -> None:
    c = _conn()
    try:
        with c:
            c.execute("DELETE FROM oauth_tokens WHERE workspace_id=? AND provider=?", (workspace_id, provider))
    finally:
        c.close()

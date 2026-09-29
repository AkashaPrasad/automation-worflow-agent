"""Persistence + event bus singletons."""
from __future__ import annotations

from functools import lru_cache

from .bus import LocalEventBus
from .sqlite import SqliteRunStore


@lru_cache
def get_store() -> SqliteRunStore:
    return SqliteRunStore()


@lru_cache
def get_bus() -> LocalEventBus:
    return LocalEventBus(get_store())


__all__ = ["SqliteRunStore", "LocalEventBus", "get_store", "get_bus"]

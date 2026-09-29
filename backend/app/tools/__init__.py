"""Tool layer: sandbox workspace ("Acme Robotics"), llm.* and web tools, registry."""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .registry import SandboxFirstRegistry
    from .sandbox.world import SandboxWorkspaceStore

_lock = threading.Lock()
_store: "SandboxWorkspaceStore | None" = None
_registry: "SandboxFirstRegistry | None" = None


def get_workspace_store() -> "SandboxWorkspaceStore":
    global _store
    with _lock:
        if _store is None:
            from .sandbox.world import SandboxWorkspaceStore

            _store = SandboxWorkspaceStore()
        return _store


def get_registry() -> "SandboxFirstRegistry":
    global _registry
    store = get_workspace_store()
    with _lock:
        if _registry is None:
            from .registry import SandboxFirstRegistry

            _registry = SandboxFirstRegistry(store)
        return _registry

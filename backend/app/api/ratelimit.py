"""In-memory sliding-window rate limiter (per workspace)."""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_s: float = 3600.0, clock=time.monotonic) -> None:
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> float:
        """Record a hit. Returns 0 if allowed, else seconds until a slot frees up."""
        now = self._clock()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] >= self.window_s:
                q.popleft()
            if len(q) >= self.limit:
                return max(1.0, self.window_s - (now - q[0]))
            q.append(now)
            return 0.0

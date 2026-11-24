"""Small in-process fixed-window limiter for unauthenticated webhook endpoints.

The server is documented as a single replica; this is deliberately not a
distributed rate limiter. It stops a single client from hammering signature
verification / body buffering. Put a real WAF/proxy in front for stronger
guarantees.
"""
from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    def __init__(self, limit: int, window: float, *, clock=time.monotonic):
        self.limit = max(1, int(limit))
        self.window = max(1.0, float(window))
        self._clock = clock
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self._clock()
        cutoff = now - self.window
        with self._lock:
            bucket = self._events.get(key)
            if bucket is None:
                bucket = deque()
                self._events[key] = bucket
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.limit:
                return False
            bucket.append(now)
            if len(self._events) > 10_000:
                empty = [name for name, events in self._events.items() if not events]
                for name in empty:
                    del self._events[name]
            return True


__all__ = ["RateLimiter"]

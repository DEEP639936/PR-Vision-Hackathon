"""Tiny in-process TTL cache (spec #18: caching where useful).

Used for read-heavy, mildly-stale-tolerant endpoints (dashboard summary,
platform listing). Values are stored per-key with an expiry; `bump()`
invalidates a namespace when ingestion refreshes data so caches stay honest.
"""
from __future__ import annotations

import threading
import time
from typing import Any


class TTLCache:
    def __init__(self, default_ttl: float = 20.0, max_entries: int = 512) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._ttl = default_ttl
        self._max = max_entries
        self._lock = threading.Lock()
        self._epoch: dict[str, int] = {}

    def get(self, key: str) -> Any | None:
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            expires, value, epoch = entry
            if epoch != self._epoch.get(key.split("::", 1)[0], 0) or expires < now:
                self._data.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        with self._lock:
            if len(self._data) >= self._max:
                # drop expired first, then oldest insert
                now = time.monotonic()
                for k in [k for k, (exp, _, _) in self._data.items() if exp < now]:
                    self._data.pop(k, None)
                if len(self._data) >= self._max:
                    oldest = min(self._data, key=lambda k: self._data[k][0])
                    self._data.pop(oldest, None)
            ns = key.split("::", 1)[0]
            self._data[key] = (time.monotonic() + (ttl or self._ttl), value, self._epoch.get(ns, 0))

    def bump(self, namespace: str) -> None:
        """Invalidate every key in a namespace (e.g. after an ingestion cycle)."""
        with self._lock:
            self._epoch[namespace] = self._epoch.get(namespace, 0) + 1
            for k in [k for k in self._data if k.split("::", 1)[0] == namespace]:
                self._data.pop(k, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._epoch.clear()


cache = TTLCache(default_ttl=20.0)

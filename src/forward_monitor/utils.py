"""Miscellaneous helpers."""

from __future__ import annotations

import asyncio
import time


class RateLimiter:
    """Simple rate limiter using sleep between events."""

    def __init__(self, rate_per_second: float):
        self.update_rate(rate_per_second)
        self._lock = asyncio.Lock()
        self._next_time = 0.0

    def update_rate(self, rate_per_second: float) -> None:
        self._interval = 0.0 if rate_per_second <= 0 else 1.0 / rate_per_second

    async def wait(self) -> None:
        async with self._lock:
            if self._interval <= 0:
                return
            now = time.perf_counter()
            if now < self._next_time:
                await asyncio.sleep(self._next_time - now)
            self._next_time = time.perf_counter() + self._interval

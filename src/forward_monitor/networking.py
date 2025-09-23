from __future__ import annotations

import asyncio
import random
from collections import deque
from typing import Iterable, Optional

import aiohttp

from .config import ProxyPoolSettings, RateLimitSettings, UserAgentSettings
from .structured_logging import log_event

__all__ = [
    "ProxyPool",
    "SoftRateLimiter",
    "UserAgentProvider",
]


class SoftRateLimiter:
    """Co-operative rate limiter with soft concurrency and jitter."""

    __slots__ = (
        "_settings",
        "_semaphore",
        "_lock",
        "_next_slot",
        "_recent",
        "_cooldown_until",
        "name",
    )

    def __init__(self, settings: RateLimitSettings, *, name: str) -> None:
        self._settings = settings
        self._semaphore = asyncio.Semaphore(max(1, settings.concurrency))
        self._lock = asyncio.Lock()
        self._next_slot = 0.0
        self._recent: deque[float] = deque()
        self._cooldown_until = 0.0
        self.name = name

    async def __aenter__(self) -> "SoftRateLimiter":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        self.release()

    async def acquire(self) -> None:
        await self._semaphore.acquire()
        loop = asyncio.get_running_loop()
        jitter_min = max(0.0, self._settings.jitter_min_ms / 1000.0)
        jitter_max = max(jitter_min, self._settings.jitter_max_ms / 1000.0)

        while True:
            async with self._lock:
                now = loop.time()
                waits: list[float] = []

                if self._settings.per_second:
                    waits.append(self._next_slot - now)

                if self._settings.per_minute:
                    cutoff = now - 60.0
                    while self._recent and self._recent[0] < cutoff:
                        self._recent.popleft()
                    if len(self._recent) >= self._settings.per_minute:
                        waits.append((self._recent[0] + 60.0) - now)

                if self._cooldown_until > now:
                    waits.append(self._cooldown_until - now)

                wait_time = max(waits) if waits else 0.0
                if wait_time <= 0:
                    if self._settings.per_second:
                        self._next_slot = now + 1.0 / self._settings.per_second
                    if self._settings.per_minute:
                        self._recent.append(now)
                    break

            await asyncio.sleep(wait_time)

        if jitter_max > 0:
            jitter = random.uniform(jitter_min, jitter_max)
            if jitter > 0:
                await asyncio.sleep(jitter)

    def release(self) -> None:
        self._semaphore.release()

    async def impose_cooldown(self, seconds: float) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            self._cooldown_until = max(self._cooldown_until, loop.time() + max(0.0, seconds))

    @property
    def settings(self) -> RateLimitSettings:
        return self._settings


class UserAgentProvider:
    """Randomises user agent selection for outbound requests."""

    __slots__ = ("_desktop", "_mobile", "_mobile_ratio", "_random")

    def __init__(self, settings: UserAgentSettings) -> None:
        desktop = list(settings.desktop)
        mobile = list(settings.mobile)
        if not desktop:
            raise ValueError("Desktop user-agent pool cannot be empty")
        if not mobile:
            raise ValueError("Mobile user-agent pool cannot be empty")
        self._desktop = desktop
        self._mobile = mobile
        self._mobile_ratio = min(max(float(settings.mobile_ratio), 0.0), 1.0)
        self._random = random.Random()

    def pick(self, *, prefer_mobile: Optional[bool] = None) -> str:
        if prefer_mobile is None:
            prefer_mobile = self._random.random() < self._mobile_ratio
        pool = self._mobile if prefer_mobile else self._desktop
        return self._random.choice(pool)


class ProxyPool:
    """Rotates proxies and performs health checks where configured."""

    __slots__ = (
        "_settings",
        "_lock",
        "_unhealthy",
        "_verified",
        "_name",
        "_random",
    )

    def __init__(self, settings: ProxyPoolSettings, *, name: str) -> None:
        self._settings = settings.normalised()
        self._lock = asyncio.Lock()
        self._unhealthy: dict[str, float] = {}
        self._verified: set[str] = set()
        self._name = name
        self._random = random.Random()

    def has_proxies(self) -> bool:
        return bool(self._settings.endpoints)

    async def get_proxy(self) -> str | None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            candidates = [
                proxy
                for proxy in self._settings.endpoints
                if self._unhealthy.get(proxy, 0.0) <= now
            ]
            if not candidates:
                return None
            return self._random.choice(candidates)

    async def ensure_healthy(
        self, proxy: str | None, session: aiohttp.ClientSession
    ) -> bool:
        if proxy is None:
            return True
        if not self._settings.health_check_url:
            return True
        async with self._lock:
            if proxy in self._verified and proxy not in self._unhealthy:
                return True
        url = self._settings.health_check_url
        try:
            async with session.get(
                url,
                proxy=proxy,
                timeout=self._settings.health_check_timeout,
            ) as response:
                if response.status >= 400:
                    raise aiohttp.ClientError(f"status={response.status}")
        except Exception as exc:  # pragma: no cover - network failure paths
            await self.mark_bad(proxy, reason=f"health_check_failed:{exc}")
            return False
        async with self._lock:
            self._verified.add(proxy)
        log_event(
            "proxy_checked",
            level=20,
            discord_channel_id=None,
            discord_message_id=None,
            telegram_chat_id=None,
            attempt=None,
            outcome="healthy",
            latency_ms=None,
            extra={"service": self._name, "proxy": proxy},
        )
        return True

    async def mark_bad(self, proxy: str, *, reason: str) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            self._unhealthy[proxy] = loop.time() + self._settings.recovery_seconds
            self._verified.discard(proxy)
        log_event(
            "proxy_marked_unhealthy",
            level=30,
            discord_channel_id=None,
            discord_message_id=None,
            telegram_chat_id=None,
            attempt=None,
            outcome="cooldown",
            latency_ms=None,
            extra={"service": self._name, "proxy": proxy, "reason": reason},
        )

    async def mark_success(self, proxy: str | None) -> None:
        if proxy is None:
            return
        async with self._lock:
            self._unhealthy.pop(proxy, None)
            self._verified.add(proxy)

    def endpoints(self) -> Iterable[str]:
        return tuple(self._settings.endpoints)

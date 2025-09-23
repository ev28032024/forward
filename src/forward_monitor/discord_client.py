from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Mapping
from time import perf_counter
from typing import Any, Literal

import aiohttp

from .networking import ProxyPool, SoftRateLimiter, UserAgentProvider
from .structured_logging import log_event
from .types import DiscordMessage

DISCORD_API_BASE = "https://discord.com/api/v10"
_DEFAULT_HEADERS: Mapping[str, str] = {
    "Accept": "application/json",
}
_RATE_LIMIT_MAX_RETRIES = 5
_NETWORK_MAX_RETRIES = 3
_SUSPICIOUS_KEYWORDS = (
    "captcha",
    "verify",
    "unusual",
    "temporary",
    "suspicious",
)

TokenType = Literal["auto", "bot", "user", "bearer"]


def _normalize_authorization_header(
    token: str, token_type: TokenType = "auto"
) -> str:
    """Return a Discord-ready Authorization header value.

    The ``token_type`` argument allows callers to explicitly control how the
    header should be formed.  When set to ``"auto"`` (the default) the function
    keeps backwards compatibility with historical behaviour: only values that do
    not resemble user or bearer tokens receive the ``"Bot "`` prefix.
    """

    normalized = token.strip()

    if token_type == "bot":
        if normalized.startswith("Bot "):
            return normalized
        return f"Bot {normalized}"

    if token_type == "bearer":
        if normalized.startswith("Bearer "):
            return normalized
        return f"Bearer {normalized}"

    if token_type == "user":
        return normalized

    if token_type != "auto":
        raise ValueError(f"Unknown Discord token type: {token_type}")

    # User tokens (not recommended) start either with "mfa." or three segments
    # separated by dots.  Bearer tokens already contain whitespace.  In those
    # cases we should not force the "Bot " prefix.
    if normalized.startswith("Bot ") or normalized.startswith("Bearer "):
        return normalized

    if normalized.startswith("mfa."):
        return normalized

    if normalized.count(".") == 2:
        return normalized

    return f"Bot {normalized}"


class DiscordAPIError(RuntimeError):
    """Error raised when the Discord API returns a non-success response."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Discord API request failed with status {status}: {body}")
        self.status = status
        self.body = body


__all__ = ["DiscordAPIError", "DiscordClient", "TokenType"]


class DiscordClient:
    """HTTP client for the Discord REST API with adaptive protections."""

    __slots__ = (
        "_session",
        "_headers",
        "_rate_limiter",
        "_proxy_pool",
        "_user_agents",
        "_current_proxy",
        "_suspicion_count",
    )

    def __init__(
        self,
        token: str,
        session: aiohttp.ClientSession,
        *,
        rate_limiter: SoftRateLimiter,
        proxy_pool: ProxyPool,
        user_agents: UserAgentProvider,
        token_type: TokenType = "auto",
    ) -> None:
        self._session = session
        self._headers = dict(_DEFAULT_HEADERS)
        self._headers["Authorization"] = _normalize_authorization_header(
            token, token_type
        )
        self._rate_limiter = rate_limiter
        self._proxy_pool = proxy_pool
        self._user_agents = user_agents
        self._current_proxy: str | None = None
        self._suspicion_count = 0

    async def fetch_messages(
        self,
        channel_id: int,
        *,
        after: str | None = None,
        limit: int = 100,
    ) -> list[DiscordMessage]:
        params: dict[str, str] = {"limit": str(max(1, min(limit, 100)))}
        if after:
            params["after"] = after

        data = await self._request_json(
            "GET",
            f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
            params=params,
            discord_channel_id=channel_id,
        )
        if not isinstance(data, list):
            raise RuntimeError("Unexpected Discord response shape when fetching messages")

        return sorted(data, key=lambda item: int(item["id"]))

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        max_rate_limit_retries: int = _RATE_LIMIT_MAX_RETRIES,
        max_network_retries: int = _NETWORK_MAX_RETRIES,
        discord_channel_id: int | None = None,
    ) -> Any:
        attempts = 0
        network_attempts = 0
        while True:
            proxy = await self._proxy_pool.get_proxy()
            if not await self._proxy_pool.ensure_healthy(proxy, self._session):
                await asyncio.sleep(0.5)
                continue

            if proxy != self._current_proxy:
                log_event(
                    "discord_proxy_switch",
                    level=logging.INFO,
                    discord_channel_id=discord_channel_id,
                    discord_message_id=None,
                    telegram_chat_id=None,
                    attempt=None,
                    outcome="switch",
                    latency_ms=None,
                    extra={"proxy": proxy or "direct"},
                )
                self._current_proxy = proxy

            headers = dict(self._headers)
            headers["User-Agent"] = self._user_agents.pick()

            start = perf_counter()
            try:
                async with self._rate_limiter:
                    async with self._session.request(
                        method,
                        url,
                        headers=headers,
                        params=params,
                        proxy=proxy,
                    ) as response:
                        elapsed_ms = (perf_counter() - start) * 1000
                        network_attempts = 0

                        if response.status == 429 or response.status >= 500:
                            attempts += 1
                            if attempts > max_rate_limit_retries:
                                text = await response.text()
                                log_event(
                                    "discord_rate_limit_exhausted",
                                    level=logging.ERROR,
                                    discord_channel_id=discord_channel_id,
                                    discord_message_id=None,
                                    telegram_chat_id=None,
                                    attempt=attempts,
                                    outcome="failure",
                                    latency_ms=elapsed_ms,
                                    extra={"url": url, "status": response.status, "proxy": proxy},
                                )
                                raise DiscordAPIError(response.status, text)

                            retry_after = await _retry_after_seconds(response)
                            backoff = min(2 ** attempts, 60.0)
                            delay = max(retry_after, random.uniform(0.5, backoff))
                            await self._rate_limiter.impose_cooldown(
                                self._rate_limiter.settings.cooldown_seconds
                            )
                            if proxy:
                                await self._proxy_pool.mark_bad(
                                    proxy, reason=f"status_{response.status}"
                                )
                            log_event(
                                "discord_rate_limited",
                                level=logging.WARNING,
                                discord_channel_id=discord_channel_id,
                                discord_message_id=None,
                                telegram_chat_id=None,
                                attempt=attempts,
                                outcome="retry",
                                latency_ms=elapsed_ms,
                                extra={
                                    "retry_after": delay,
                                    "status": response.status,
                                    "url": url,
                                    "proxy": proxy,
                                },
                            )
                            await asyncio.sleep(delay)
                            continue

                        if response.status >= 400:
                            text = await response.text()
                            if response.status in {401, 403} or _looks_suspicious(text):
                                self._suspicion_count += 1
                                cooldown = (
                                    self._rate_limiter.settings.cooldown_seconds
                                    * min(self._suspicion_count, 3)
                                )
                                await self._rate_limiter.impose_cooldown(cooldown)
                                if proxy:
                                    await self._proxy_pool.mark_bad(
                                        proxy, reason=f"suspicious_{response.status}"
                                    )
                                log_event(
                                    "discord_suspicious_response",
                                    level=logging.WARNING,
                                    discord_channel_id=discord_channel_id,
                                    discord_message_id=None,
                                    telegram_chat_id=None,
                                    attempt=attempts + 1,
                                    outcome="cooldown",
                                    latency_ms=elapsed_ms,
                                    extra={"status": response.status, "url": url, "proxy": proxy},
                                )
                            else:
                                self._suspicion_count = 0
                            log_event(
                                "discord_http_error",
                                level=logging.ERROR,
                                discord_channel_id=discord_channel_id,
                                discord_message_id=None,
                                telegram_chat_id=None,
                                attempt=attempts + 1,
                                outcome="failure",
                                latency_ms=elapsed_ms,
                                extra={"status": response.status, "url": url, "proxy": proxy},
                            )
                            raise DiscordAPIError(response.status, text)

                        content_type = response.headers.get("Content-Type", "")
                        if "application/json" not in content_type:
                            text = await response.text()
                            log_event(
                                "discord_unexpected_content_type",
                                level=logging.ERROR,
                                discord_channel_id=discord_channel_id,
                                discord_message_id=None,
                                telegram_chat_id=None,
                                attempt=attempts + 1,
                                outcome="failure",
                                latency_ms=elapsed_ms,
                                extra={"content_type": content_type, "url": url, "proxy": proxy},
                            )
                            raise RuntimeError(
                                "Unexpected content type "
                                f"'{content_type}' from Discord API: {text[:200]}"
                            )

                        payload = await response.json()
                        self._suspicion_count = 0
                        await self._proxy_pool.mark_success(proxy)
                        log_event(
                            "discord_request_succeeded",
                            level=logging.DEBUG,
                            discord_channel_id=discord_channel_id,
                            discord_message_id=None,
                            telegram_chat_id=None,
                            attempt=attempts + 1,
                            outcome="success",
                            latency_ms=elapsed_ms,
                            extra={"url": url, "proxy": proxy},
                        )
                        return payload
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                network_attempts += 1
                elapsed_ms = (perf_counter() - start) * 1000
                if proxy:
                    await self._proxy_pool.mark_bad(proxy, reason=type(exc).__name__)
                if network_attempts > max_network_retries:
                    log_event(
                        "discord_network_failure",
                        level=logging.ERROR,
                        discord_channel_id=discord_channel_id,
                        discord_message_id=None,
                        telegram_chat_id=None,
                        attempt=network_attempts,
                        outcome="failure",
                        latency_ms=elapsed_ms,
                        extra={"url": url, "error": type(exc).__name__, "proxy": proxy},
                    )
                    raise
                backoff = min(2 ** network_attempts, 30.0)
                delay = random.uniform(0.5, backoff)
                log_event(
                    "discord_network_retry",
                    level=logging.WARNING,
                    discord_channel_id=discord_channel_id,
                    discord_message_id=None,
                    telegram_chat_id=None,
                    attempt=network_attempts,
                    outcome="retry",
                    latency_ms=elapsed_ms,
                    extra={"url": url, "backoff_seconds": delay, "proxy": proxy},
                )
                await asyncio.sleep(delay)


async def _retry_after_seconds(response: aiohttp.ClientResponse) -> float:
    try:
        payload = await response.json()
    except aiohttp.ContentTypeError:
        return 1.0

    retry_after = payload.get("retry_after")
    try:
        return max(float(retry_after), 0.0)
    except (TypeError, ValueError):
        return 1.0


def _looks_suspicious(body: str) -> bool:
    lower = body.casefold()
    return any(keyword in lower for keyword in _SUSPICIOUS_KEYWORDS)

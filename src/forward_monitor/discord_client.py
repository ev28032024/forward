from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from time import perf_counter
from typing import Any, Literal, Optional

import aiohttp

from .structured_logging import log_event
from .types import DiscordMessage

DISCORD_API_BASE = "https://discord.com/api/v10"
_DEFAULT_HEADERS: Mapping[str, str] = {
    "User-Agent": "forward-monitor/0.1",
}
_RATE_LIMIT_MAX_RETRIES = 5
_NETWORK_MAX_RETRIES = 3
_DEFAULT_MAX_CONCURRENCY = 8

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
    """Simple HTTP client for the Discord REST API."""

    __slots__ = (
        "_session",
        "_headers",
        "_semaphore",
        "_rate_limit_lock",
        "_next_request_time",
    )

    def __init__(
        self,
        token: str,
        session: aiohttp.ClientSession,
        *,
        max_concurrency: int = _DEFAULT_MAX_CONCURRENCY,
        token_type: TokenType = "auto",
    ) -> None:
        self._session = session
        self._headers = dict(_DEFAULT_HEADERS)
        self._headers["Authorization"] = _normalize_authorization_header(
            token, token_type
        )
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self._rate_limit_lock = asyncio.Lock()
        self._next_request_time = 0.0

    async def fetch_messages(
        self,
        channel_id: int,
        *,
        after: Optional[str] = None,
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
        params: Optional[Mapping[str, str]] = None,
        max_rate_limit_retries: int = _RATE_LIMIT_MAX_RETRIES,
        max_network_retries: int = _NETWORK_MAX_RETRIES,
        discord_channel_id: int | None = None,
    ) -> Any:
        attempts = 0
        network_attempts = 0
        while True:
            await self._respect_rate_limit()
            start = perf_counter()
            try:
                async with self._semaphore:
                    async with self._session.request(
                        method, url, headers=self._headers, params=params
                    ) as response:
                        elapsed_ms = (perf_counter() - start) * 1000
                        network_attempts = 0
                        if response.status == 429:
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
                                    extra={"url": url, "status": response.status},
                                )
                                raise DiscordAPIError(response.status, text)

                            retry_after = await _rate_limit_delay_seconds(response)
                            expected_until = await self._register_rate_limit_delay(
                                retry_after
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
                                extra={"retry_after": retry_after, "url": url},
                            )
                            await asyncio.sleep(retry_after)
                            await self._mark_rate_limit_consumed(expected_until)
                            continue

                        if response.status >= 400:
                            text = await response.text()
                            log_event(
                                "discord_http_error",
                                level=logging.ERROR,
                                discord_channel_id=discord_channel_id,
                                discord_message_id=None,
                                telegram_chat_id=None,
                                attempt=attempts + 1,
                                outcome="failure",
                                latency_ms=elapsed_ms,
                                extra={"status": response.status, "url": url},
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
                                extra={"content_type": content_type, "url": url},
                            )
                            raise RuntimeError(
                                "Unexpected content type "
                                f"'{content_type}' from Discord API: {text[:200]}"
                            )
                        payload = await response.json()
                        log_event(
                            "discord_request_succeeded",
                            level=logging.DEBUG,
                            discord_channel_id=discord_channel_id,
                            discord_message_id=None,
                            telegram_chat_id=None,
                            attempt=attempts + 1,
                            outcome="success",
                            latency_ms=elapsed_ms,
                            extra={"url": url},
                        )
                        return payload
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                network_attempts += 1
                elapsed_ms = (perf_counter() - start) * 1000
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
                        extra={"url": url, "error": type(exc).__name__},
                    )
                    raise
                backoff = min(2 ** (network_attempts - 1), 30)
                log_event(
                    "discord_network_retry",
                    level=logging.WARNING,
                    discord_channel_id=discord_channel_id,
                    discord_message_id=None,
                    telegram_chat_id=None,
                    attempt=network_attempts,
                    outcome="retry",
                    latency_ms=elapsed_ms,
                    extra={"url": url, "backoff_seconds": backoff},
                )
                await asyncio.sleep(backoff)

    async def _respect_rate_limit(self) -> None:
        loop = asyncio.get_running_loop()
        async with self._rate_limit_lock:
            now = loop.time()
            target_time = self._next_request_time
            delay = target_time - now
            if delay <= 0:
                self._next_request_time = now
                return

        await asyncio.sleep(delay)

        async with self._rate_limit_lock:
            now = loop.time()
            if self._next_request_time == target_time:
                self._next_request_time = now

    async def _register_rate_limit_delay(self, retry_after: float) -> float:
        loop = asyncio.get_running_loop()
        async with self._rate_limit_lock:
            now = loop.time()
            proposed = now + max(retry_after, 0.0)
            if proposed > self._next_request_time:
                self._next_request_time = proposed
            return self._next_request_time

    async def _mark_rate_limit_consumed(self, expected_time: float) -> None:
        loop = asyncio.get_running_loop()
        async with self._rate_limit_lock:
            now = loop.time()
            if self._next_request_time == expected_time or self._next_request_time < now:
                self._next_request_time = now


async def _rate_limit_delay_seconds(response: aiohttp.ClientResponse) -> float:
    try:
        payload = await response.json()
    except aiohttp.ContentTypeError:
        return 1.0

    retry_after = payload.get("retry_after")
    try:
        return max(float(retry_after), 0.0)
    except (TypeError, ValueError):
        return 1.0

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Iterable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Literal, cast

import aiohttp

from .networking import ProxyPool, SoftRateLimiter, UserAgentProvider
from .structured_logging import log_event
from .types import TelegramAPIPayload, TelegramAPIResponse, TelegramResponseParameters

__all__ = ["TelegramClient"]


class TelegramClient:
    """Telegram Bot API client hardened for rate limits and proxy usage."""

    RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
    _SUSPICIOUS_KEYWORDS = ("too many requests", "flood", "retry later", "blocked")
    __slots__ = (
        "_token",
        "_session",
        "_rate_limiter",
        "_proxy_pool",
        "_user_agents",
        "_default_disable_preview",
        "_default_parse_mode",
        "_current_proxy",
    )

    _current_proxy: str | None

    def __init__(
        self,
        token: str,
        session: aiohttp.ClientSession,
        *,
        rate_limiter: SoftRateLimiter,
        proxy_pool: ProxyPool,
        user_agents: UserAgentProvider,
        default_disable_preview: bool = True,
        default_parse_mode: str | None = "HTML",
    ):
        self._token = token
        self._session = session
        self._rate_limiter = rate_limiter
        self._proxy_pool = proxy_pool
        self._user_agents = user_agents
        self._default_disable_preview = default_disable_preview
        self._default_parse_mode = default_parse_mode
        self._current_proxy: str | None = None

    async def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        disable_web_page_preview: bool | None = None,
        parse_mode: str | None = None,
        retry_attempts: int = 5,
        retry_statuses: Iterable[int] | None = None,
    ) -> TelegramAPIResponse:
        disable_preview = (
            self._default_disable_preview
            if disable_web_page_preview is None
            else bool(disable_web_page_preview)
        )
        parse_mode_value = parse_mode or self._default_parse_mode
        payload: TelegramAPIPayload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_preview,
        }
        if parse_mode_value is not None:
            payload["parse_mode"] = parse_mode_value
        return await self._post(
            "sendMessage",
            payload,
            retry_attempts=retry_attempts,
            retry_statuses=retry_statuses,
        )

    async def send_photo(
        self,
        chat_id: str,
        photo: str,
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
        retry_attempts: int = 5,
        retry_statuses: Iterable[int] | None = None,
    ) -> TelegramAPIResponse:
        return await self._send_media(
            method="sendPhoto",
            chat_id=chat_id,
            media_field="photo",
            media_value=photo,
            caption=caption,
            parse_mode=parse_mode,
            retry_attempts=retry_attempts,
            retry_statuses=retry_statuses,
        )

    async def send_video(
        self,
        chat_id: str,
        video: str,
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
        retry_attempts: int = 5,
        retry_statuses: Iterable[int] | None = None,
    ) -> TelegramAPIResponse:
        return await self._send_media(
            method="sendVideo",
            chat_id=chat_id,
            media_field="video",
            media_value=video,
            caption=caption,
            parse_mode=parse_mode,
            retry_attempts=retry_attempts,
            retry_statuses=retry_statuses,
        )

    async def send_audio(
        self,
        chat_id: str,
        audio: str,
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
        retry_attempts: int = 5,
        retry_statuses: Iterable[int] | None = None,
    ) -> TelegramAPIResponse:
        return await self._send_media(
            method="sendAudio",
            chat_id=chat_id,
            media_field="audio",
            media_value=audio,
            caption=caption,
            parse_mode=parse_mode,
            retry_attempts=retry_attempts,
            retry_statuses=retry_statuses,
        )

    async def send_document(
        self,
        chat_id: str,
        document: str,
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
        retry_attempts: int = 5,
        retry_statuses: Iterable[int] | None = None,
    ) -> TelegramAPIResponse:
        return await self._send_media(
            method="sendDocument",
            chat_id=chat_id,
            media_field="document",
            media_value=document,
            caption=caption,
            parse_mode=parse_mode,
            retry_attempts=retry_attempts,
            retry_statuses=retry_statuses,
        )

    async def _send_media(
        self,
        *,
        method: str,
        chat_id: str,
        media_field: Literal["photo", "video", "audio", "document"],
        media_value: str,
        caption: str | None,
        parse_mode: str | None,
        retry_attempts: int,
        retry_statuses: Iterable[int] | None,
    ) -> TelegramAPIResponse:
        payload: TelegramAPIPayload = {"chat_id": chat_id}
        payload[media_field] = media_value
        if caption is not None:
            payload["caption"] = caption
        parse_mode_value = parse_mode or self._default_parse_mode
        if parse_mode_value is not None:
            payload["parse_mode"] = parse_mode_value
        return await self._post(
            method,
            payload,
            retry_attempts=retry_attempts,
            retry_statuses=retry_statuses,
        )

    async def _post(
        self,
        method: str,
        request_payload: TelegramAPIPayload,
        *,
        retry_attempts: int = 5,
        retry_statuses: Iterable[int] | None = None,
    ) -> TelegramAPIResponse:
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        statuses = (
            self.RETRYABLE_STATUSES
            if retry_statuses is None
            else _normalise_retry_statuses(retry_statuses)
        )
        chat_id = request_payload.get("chat_id")
        attempt = 0
        network_attempts = 0
        while True:
            attempt += 1
            try:
                proxy = await self._proxy_pool.get_proxy()
                if not await self._proxy_pool.ensure_healthy(proxy, self._session):
                    await asyncio.sleep(0.5)
                    continue

                if proxy != self._current_proxy:
                    log_event(
                        "telegram_proxy_switch",
                        level=logging.INFO,
                        discord_channel_id=None,
                        discord_message_id=None,
                        telegram_chat_id=chat_id,
                        attempt=None,
                        outcome="switch",
                        latency_ms=None,
                        extra={"proxy": proxy or "direct"},
                    )
                    self._current_proxy = proxy

                headers = {"User-Agent": self._user_agents.pick()}

                async with self._rate_limiter:
                    async with self._session.post(
                        url,
                        json=request_payload,
                        proxy=proxy,
                        proxy_auth=self._proxy_pool.auth,
                        headers=headers,
                    ) as response:
                        if response.status in statuses and attempt <= retry_attempts:
                            retry_after = await _retry_after_seconds(response)
                            backoff = min(2 ** attempt, 30.0)
                            delay = max(retry_after, random.uniform(0.5, backoff))
                            await self._rate_limiter.impose_cooldown(
                                self._rate_limiter.settings.cooldown_seconds
                            )
                            if proxy:
                                await self._proxy_pool.mark_bad(
                                    proxy,
                                    reason=f"status_{response.status}",
                                    session=self._session,
                                )
                            log_event(
                                "telegram_rate_limited",
                                level=logging.WARNING,
                                discord_channel_id=None,
                                discord_message_id=None,
                                telegram_chat_id=chat_id,
                                attempt=attempt,
                                outcome="retry",
                                latency_ms=None,
                                extra={
                                    "status": response.status,
                                    "proxy": proxy,
                                    "method": method,
                                    "retry_after": delay,
                                },
                            )
                            await asyncio.sleep(delay)
                            continue

                        if response.status >= 400:
                            detail = await response.text()
                            if (
                                response.status in {401, 403}
                                or _looks_suspicious(detail, self._SUSPICIOUS_KEYWORDS)
                            ):
                                await self._rate_limiter.impose_cooldown(
                                    self._rate_limiter.settings.cooldown_seconds
                                )
                                if proxy:
                                    await self._proxy_pool.mark_bad(
                                        proxy,
                                        reason=f"suspicious_{response.status}",
                                        session=self._session,
                                    )
                                log_event(
                                    "telegram_suspicious_response",
                                    level=logging.WARNING,
                                    discord_channel_id=None,
                                    discord_message_id=None,
                                    telegram_chat_id=chat_id,
                                    attempt=attempt,
                                    outcome="cooldown",
                                    latency_ms=None,
                                    extra={"status": response.status, "proxy": proxy},
                                )
                            raise RuntimeError(
                                "Telegram API request failed with status "
                                f"{response.status}: {detail}"
                            )

                        response_payload = cast(
                            TelegramAPIResponse, await response.json()
                        )
                        if response_payload.get("ok") is False:
                            description = response_payload.get("description")
                            error_code = response_payload.get("error_code")
                            extra_details = []
                            if description:
                                extra_details.append(str(description))
                            if error_code is not None:
                                extra_details.append(f"error_code={error_code}")
                            detail = "; ".join(extra_details) if extra_details else "no details"
                            raise RuntimeError(
                                f"Telegram API method '{method}' reported failure: {detail}"
                            )
                        await self._proxy_pool.mark_success(proxy)
                        return response_payload
            except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
                network_attempts += 1
                if network_attempts <= retry_attempts:
                    backoff = min(2 ** network_attempts, 20.0)
                    delay = random.uniform(0.5, backoff)
                    log_event(
                        "telegram_network_retry",
                        level=logging.WARNING,
                        discord_channel_id=None,
                        discord_message_id=None,
                        telegram_chat_id=chat_id,
                        attempt=network_attempts,
                        outcome="retry",
                        latency_ms=None,
                        extra={"error": type(exc).__name__, "delay": delay},
                    )
                    await asyncio.sleep(delay)
                    continue
                raise RuntimeError("Telegram API request failed due to a network error") from exc


def _normalise_retry_statuses(statuses: Iterable[int | str]) -> set[int]:
    normalised: set[int] = set()
    for status in statuses:
        try:
            normalised.add(int(status))
        except (TypeError, ValueError) as exc:  # pragma: no cover - defensive programming
            raise ValueError(f"Retry status codes must be integers; got {status!r}") from exc
    return normalised


async def _retry_after_seconds(
    response: aiohttp.ClientResponse, *, now: datetime | None = None
) -> float:
    reference_time = now or datetime.now(UTC)
    retry_header = response.headers.get("Retry-After")
    if retry_header:
        try:
            return float(retry_header)
        except ValueError:  # pragma: no cover - defensive parsing
            try:
                retry_at = parsedate_to_datetime(retry_header)
            except (TypeError, ValueError, IndexError):
                retry_at = None
            if retry_at is not None:
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                delay = (retry_at - reference_time).total_seconds()
                if delay > 0:
                    return delay
                return 1.0

    try:
        payload = cast(TelegramAPIResponse, await response.json())
    except aiohttp.ContentTypeError:  # pragma: no cover - fallback for non-JSON responses
        return 1.0

    parameters: TelegramResponseParameters = payload.get("parameters", {})
    retry_after = parameters.get("retry_after")
    if retry_after is None:
        retry_after = payload.get("retry_after")
    if retry_after is not None:
        try:
            return float(retry_after)
        except (TypeError, ValueError):  # pragma: no cover - fallback when parsing fails
            return 1.0
    return 1.0


def _looks_suspicious(body: str, keywords: Iterable[str]) -> bool:
    lowered = body.casefold()
    return any(keyword in lowered for keyword in keywords)

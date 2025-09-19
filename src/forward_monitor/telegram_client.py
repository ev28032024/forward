from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterable, Set

import aiohttp


__all__ = ["TelegramClient"]


class TelegramClient:
    """Minimal Telegram Bot API client."""

    RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
    __slots__ = ("_token", "_session")

    def __init__(self, token: str, session: aiohttp.ClientSession):
        self._token = token
        self._session = session

    async def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        disable_web_page_preview: bool = True,
        retry_attempts: int = 5,
        retry_statuses: Iterable[int] | None = None,
    ) -> Dict[str, Any]:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_web_page_preview,
        }
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
        retry_attempts: int = 5,
        retry_statuses: Iterable[int] | None = None,
    ) -> Dict[str, Any]:
        return await self._send_media(
            method="sendPhoto",
            chat_id=chat_id,
            media_field="photo",
            media_value=photo,
            caption=caption,
            retry_attempts=retry_attempts,
            retry_statuses=retry_statuses,
        )

    async def send_video(
        self,
        chat_id: str,
        video: str,
        *,
        caption: str | None = None,
        retry_attempts: int = 5,
        retry_statuses: Iterable[int] | None = None,
    ) -> Dict[str, Any]:
        return await self._send_media(
            method="sendVideo",
            chat_id=chat_id,
            media_field="video",
            media_value=video,
            caption=caption,
            retry_attempts=retry_attempts,
            retry_statuses=retry_statuses,
        )

    async def send_audio(
        self,
        chat_id: str,
        audio: str,
        *,
        caption: str | None = None,
        retry_attempts: int = 5,
        retry_statuses: Iterable[int] | None = None,
    ) -> Dict[str, Any]:
        return await self._send_media(
            method="sendAudio",
            chat_id=chat_id,
            media_field="audio",
            media_value=audio,
            caption=caption,
            retry_attempts=retry_attempts,
            retry_statuses=retry_statuses,
        )

    async def send_document(
        self,
        chat_id: str,
        document: str,
        *,
        caption: str | None = None,
        retry_attempts: int = 5,
        retry_statuses: Iterable[int] | None = None,
    ) -> Dict[str, Any]:
        return await self._send_media(
            method="sendDocument",
            chat_id=chat_id,
            media_field="document",
            media_value=document,
            caption=caption,
            retry_attempts=retry_attempts,
            retry_statuses=retry_statuses,
        )

    async def _send_media(
        self,
        *,
        method: str,
        chat_id: str,
        media_field: str,
        media_value: str,
        caption: str | None,
        retry_attempts: int,
        retry_statuses: Iterable[int] | None,
    ) -> Dict[str, Any]:
        payload = {"chat_id": chat_id, media_field: media_value}
        if caption is not None:
            payload["caption"] = caption
        return await self._post(
            method,
            payload,
            retry_attempts=retry_attempts,
            retry_statuses=retry_statuses,
        )

    async def _post(
        self,
        method: str,
        payload: Dict[str, Any],
        *,
        retry_attempts: int = 5,
        retry_statuses: Iterable[int] | None = None,
    ) -> Dict[str, Any]:
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        statuses = (
            self.RETRYABLE_STATUSES
            if retry_statuses is None
            else _normalise_retry_statuses(retry_statuses)
        )
        attempt = 0
        backoff = 1.0
        while True:
            attempt += 1
            try:
                async with self._session.post(url, json=payload) as response:
                    if response.status in statuses and attempt <= retry_attempts:
                        retry_after = await _retry_after_seconds(response)
                        await asyncio.sleep(max(retry_after, backoff))
                        backoff = min(backoff * 2, 30)
                        continue

                    if response.status >= 400:
                        detail = await response.text()
                        raise RuntimeError(
                            f"Telegram API request failed with status {response.status}: {detail}"
                        )

                    payload = await response.json()
                    if isinstance(payload, dict) and payload.get("ok") is False:
                        description = payload.get("description")
                        error_code = payload.get("error_code")
                        extra_details = []
                        if description:
                            extra_details.append(str(description))
                        if error_code is not None:
                            extra_details.append(f"error_code={error_code}")
                        detail = "; ".join(extra_details) if extra_details else "no details"
                        raise RuntimeError(
                            f"Telegram API method '{method}' reported failure: {detail}"
                        )
                    return payload
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt <= retry_attempts:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)
                    continue
                raise RuntimeError("Telegram API request failed due to a network error") from exc


def _normalise_retry_statuses(statuses: Iterable[int | str]) -> Set[int]:
    normalised: Set[int] = set()
    for status in statuses:
        try:
            normalised.add(int(status))
        except (TypeError, ValueError) as exc:  # pragma: no cover - defensive programming
            raise ValueError(
                f"Retry status codes must be integers; got {status!r}"
            ) from exc
    return normalised


async def _retry_after_seconds(
    response: aiohttp.ClientResponse, *, now: datetime | None = None
) -> float:
    reference_time = now or datetime.now(timezone.utc)
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
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                delay = (retry_at - reference_time).total_seconds()
                if delay > 0:
                    return delay
                return 1.0

    try:
        payload = await response.json()
    except aiohttp.ContentTypeError:  # pragma: no cover - fallback for non-JSON responses
        return 1.0

    retry_after = payload.get("parameters", {}).get("retry_after")
    if retry_after is None:
        retry_after = payload.get("retry_after")
    try:
        return float(retry_after)
    except (TypeError, ValueError):  # pragma: no cover - fallback when parsing fails
        return 1.0

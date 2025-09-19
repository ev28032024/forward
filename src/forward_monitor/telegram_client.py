from __future__ import annotations

import asyncio
from typing import Any, Dict, Iterable

import aiohttp


class TelegramClient:
    """Minimal Telegram Bot API client."""

    RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

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
        payload = {"chat_id": chat_id, "photo": photo}
        if caption:
            payload["caption"] = caption
        return await self._post(
            "sendPhoto",
            payload,
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
        payload = {"chat_id": chat_id, "video": video}
        if caption:
            payload["caption"] = caption
        return await self._post(
            "sendVideo",
            payload,
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
        payload = {"chat_id": chat_id, "audio": audio}
        if caption:
            payload["caption"] = caption
        return await self._post(
            "sendAudio",
            payload,
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
        payload = {"chat_id": chat_id, "document": document}
        if caption:
            payload["caption"] = caption
        return await self._post(
            "sendDocument",
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
        statuses = set(retry_statuses or self.RETRYABLE_STATUSES)
        attempt = 0
        backoff = 1.0
        while True:
            attempt += 1
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
                return await response.json()


async def _retry_after_seconds(response: aiohttp.ClientResponse) -> float:
    retry_header = response.headers.get("Retry-After")
    if retry_header:
        try:
            return float(retry_header)
        except ValueError:  # pragma: no cover - defensive parsing
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

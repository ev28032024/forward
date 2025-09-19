from __future__ import annotations

import asyncio
from collections.abc import Mapping, MutableMapping
from typing import Any, Optional

import aiohttp

DISCORD_API_BASE = "https://discord.com/api/v10"
_DEFAULT_HEADERS: Mapping[str, str] = {
    "User-Agent": "forward-monitor/0.1",
}
_RATE_LIMIT_MAX_RETRIES = 5
_NETWORK_MAX_RETRIES = 3


class DiscordAPIError(RuntimeError):
    """Error raised when the Discord API returns a non-success response."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Discord API request failed with status {status}: {body}")
        self.status = status
        self.body = body


__all__ = ["DiscordAPIError", "DiscordClient"]


class DiscordClient:
    """Simple HTTP client for the Discord REST API."""

    __slots__ = ("_session", "_headers")

    def __init__(self, token: str, session: aiohttp.ClientSession):
        self._session = session
        self._headers = dict(_DEFAULT_HEADERS)
        self._headers["Authorization"] = token

    async def fetch_messages(
        self,
        channel_id: int,
        *,
        after: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params: MutableMapping[str, str] = {"limit": str(max(1, min(limit, 100)))}
        if after:
            params["after"] = after

        data = await self._request_json(
            "GET",
            f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
            params=params,
        )
        if not isinstance(data, list):
            raise RuntimeError("Unexpected Discord response shape when fetching messages")

        # Discord returns messages in reverse chronological order.
        return sorted(data, key=lambda item: int(item["id"]))

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Mapping[str, str]] = None,
        max_rate_limit_retries: int = _RATE_LIMIT_MAX_RETRIES,
        max_network_retries: int = _NETWORK_MAX_RETRIES,
    ) -> Any:
        attempts = 0
        network_attempts = 0
        while True:
            try:
                async with self._session.request(
                    method, url, headers=self._headers, params=params
                ) as response:
                    network_attempts = 0
                    if response.status == 429:
                        attempts += 1
                        if attempts > max_rate_limit_retries:
                            text = await response.text()
                            raise DiscordAPIError(response.status, text)

                        retry_after = await _rate_limit_delay_seconds(response)
                        await asyncio.sleep(retry_after)
                        continue

                    if response.status >= 400:
                        text = await response.text()
                        raise DiscordAPIError(response.status, text)

                    content_type = response.headers.get("Content-Type", "")
                    if "application/json" not in content_type:
                        text = await response.text()
                        raise RuntimeError(
                            "Unexpected content type "
                            f"'{content_type}' from Discord API: {text[:200]}",
                        )
                    return await response.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                network_attempts += 1
                if network_attempts > max_network_retries:
                    raise exc
                await asyncio.sleep(2 ** (network_attempts - 1))


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

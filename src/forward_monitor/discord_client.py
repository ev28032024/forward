from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import aiohttp


DISCORD_API_BASE = "https://discord.com/api/v10"


class DiscordAPIError(RuntimeError):
    """Error raised when the Discord API returns a non-success response."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Discord API request failed with status {status}: {body}")
        self.status = status
        self.body = body


class DiscordClient:
    """Simple HTTP client for the Discord REST API."""

    def __init__(self, token: str, session: aiohttp.ClientSession):
        self._token = token
        self._session = session

    async def fetch_messages(self, channel_id: int, *, after: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        params = {"limit": str(max(1, min(limit, 100)))}
        if after:
            params["after"] = after

        data = await self._request_json(
            "GET", f"{DISCORD_API_BASE}/channels/{channel_id}/messages", params=params
        )
        if not isinstance(data, list):
            raise RuntimeError("Unexpected Discord response shape when fetching messages")

        # Discord returns messages in reverse chronological order.
        return sorted(data, key=lambda item: int(item["id"]))

    async def fetch_pins(self, channel_id: int) -> List[Dict[str, Any]]:
        data = await self._request_json("GET", f"{DISCORD_API_BASE}/channels/{channel_id}/pins")
        if not isinstance(data, list):
            raise RuntimeError("Unexpected Discord response shape when fetching pinned messages")
        return data

    async def _request_json(
        self, method: str, url: str, *, params: Optional[Dict[str, str]] = None
    ) -> Any:
        headers = {
            "Authorization": self._token,
            "User-Agent": "forward-monitor/0.1",
        }
        while True:
            async with self._session.request(method, url, headers=headers, params=params) as response:
                if response.status == 429:
                    payload = await response.json()
                    retry_after = float(payload.get("retry_after", 1))
                    await asyncio.sleep(retry_after)
                    continue

                if response.status >= 400:
                    text = await response.text()
                    raise DiscordAPIError(response.status, text)

                content_type = response.headers.get("Content-Type", "")
                if "application/json" not in content_type:
                    text = await response.text()
                    raise RuntimeError(
                        f"Unexpected content type '{content_type}' from Discord API: {text[:200]}"
                    )
                return await response.json()

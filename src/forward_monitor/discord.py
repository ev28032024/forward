"""Discord API client."""

from __future__ import annotations

import asyncio
from typing import Any, Mapping, Sequence

import aiohttp

from .models import DiscordMessage, NetworkOptions

_API_BASE = "https://discord.com/api/v10"
_DEFAULT_USER_AGENT = "DiscordBot (https://github.com, 1.0)"


class DiscordClient:
    """Thin asynchronous wrapper around the Discord REST API."""

    def __init__(self, session: aiohttp.ClientSession):
        self._session = session
        self._token: str | None = None
        self._network = NetworkOptions()
        self._lock = asyncio.Lock()

    def set_token(self, token: str | None) -> None:
        self._token = token.strip() if token else None

    def set_network_options(self, options: NetworkOptions) -> None:
        self._network = options

    async def fetch_messages(
        self,
        channel_id: str,
        *,
        limit: int = 50,
        after: str | None = None,
    ) -> Sequence[DiscordMessage]:
        if not self._token:
            return []

        params = {"limit": str(max(1, min(limit, 100)))}
        if after:
            params["after"] = after

        headers = {
            "Authorization": self._token,
            "User-Agent": self._choose_user_agent(),
            "Accept": "application/json",
        }

        url = f"{_API_BASE}/channels/{channel_id}/messages"
        proxy = self._network.discord_proxy_url
        proxy_auth = self._build_proxy_auth()

        async with self._lock:
            try:
                timeout_cfg = aiohttp.ClientTimeout(total=15)
                async with self._session.get(
                    url,
                    headers=headers,
                    params=params,
                    proxy=proxy,
                    timeout=timeout_cfg,
                    proxy_auth=proxy_auth,
                ) as resp:
                    if resp.status >= 400:
                        return []
                    data = await resp.json()
            except aiohttp.ClientError:
                return []
        return tuple(
            _parse_message(payload, channel_id) for payload in data if isinstance(payload, Mapping)
        )

    def _choose_user_agent(self) -> str:
        return self._network.discord_user_agent or _DEFAULT_USER_AGENT

    def _build_proxy_auth(self) -> aiohttp.BasicAuth | None:
        login = self._network.discord_proxy_login
        password = self._network.discord_proxy_password
        if login:
            return aiohttp.BasicAuth(login, password or "")
        return None


def _parse_message(payload: Mapping[str, Any], channel_id: str) -> DiscordMessage:
    message_id = str(payload.get("id") or "0")
    author = payload.get("author") or {}
    author_id = str(author.get("id") or "0")
    author_name = (
        str(author.get("global_name") or "") or str(author.get("username") or "") or "Unknown"
    )
    content = str(payload.get("content") or "")
    attachments_raw = payload.get("attachments") or []
    embeds_raw = payload.get("embeds") or []
    attachments = tuple(item for item in attachments_raw if isinstance(item, Mapping))
    embeds = tuple(item for item in embeds_raw if isinstance(item, Mapping))

    return DiscordMessage(
        id=message_id,
        channel_id=str(payload.get("channel_id") or channel_id),
        author_id=author_id,
        author_name=author_name,
        content=content,
        attachments=attachments,
        embeds=embeds,
        timestamp=payload.get("timestamp"),
        edited_timestamp=payload.get("edited_timestamp"),
    )

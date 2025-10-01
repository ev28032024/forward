"""Discord API client."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import aiohttp

from .models import DiscordMessage, NetworkOptions

_API_BASE = "https://discord.com/api/v10"
_DEFAULT_USER_AGENT = "DiscordBot (https://github.com, 1.0)"


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TokenCheckResult:
    """Outcome of a Discord token validation attempt."""

    ok: bool
    display_name: str | None = None
    error: str | None = None
    status: int | None = None


@dataclass(slots=True)
class ProxyCheckResult:
    """Outcome of a proxy health-check attempt."""

    ok: bool
    error: str | None = None
    status: int | None = None


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
                        logger.warning(
                            "Discord ответил статусом %s при получении сообщений канала %s",
                            resp.status,
                            channel_id,
                        )
                        return []
                    data = await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning(
                    "Не удалось получить сообщения из Discord канала %s: %s",
                    channel_id,
                    exc,
                )
                return []
        return tuple(
            _parse_message(payload, channel_id) for payload in data if isinstance(payload, Mapping)
        )

    async def fetch_pinned_messages(self, channel_id: str) -> Sequence[DiscordMessage]:
        if not self._token:
            return []

        headers = {
            "Authorization": self._token,
            "User-Agent": self._choose_user_agent(),
            "Accept": "application/json",
        }

        url = f"{_API_BASE}/channels/{channel_id}/pins"
        proxy = self._network.discord_proxy_url
        proxy_auth = self._build_proxy_auth()

        async with self._lock:
            try:
                timeout_cfg = aiohttp.ClientTimeout(total=15)
                async with self._session.get(
                    url,
                    headers=headers,
                    proxy=proxy,
                    timeout=timeout_cfg,
                    proxy_auth=proxy_auth,
                ) as resp:
                    if resp.status >= 400:
                        logger.warning(
                            "Discord ответил статусом %s при получении закреплённых сообщений %s",
                            resp.status,
                            channel_id,
                        )
                        return []
                    data = await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning(
                    "Не удалось получить закреплённые сообщения Discord канала %s: %s",
                    channel_id,
                    exc,
                )
                return []
        return tuple(
            _parse_message(payload, channel_id) for payload in data if isinstance(payload, Mapping)
        )

    def _choose_user_agent(self) -> str:
        return self._network.discord_user_agent or _DEFAULT_USER_AGENT

    def _build_proxy_auth(
        self, options: NetworkOptions | None = None
    ) -> aiohttp.BasicAuth | None:
        opts = options or self._network
        login = opts.discord_proxy_login
        password = opts.discord_proxy_password
        if login:
            return aiohttp.BasicAuth(login, password or "")
        return None

    async def verify_token(
        self,
        token: str,
        *,
        network: NetworkOptions | None = None,
    ) -> TokenCheckResult:
        if not token:
            return TokenCheckResult(ok=False, error="Токен не задан")

        options = network or self._network
        headers = {
            "Authorization": token,
            "User-Agent": options.discord_user_agent or self._choose_user_agent(),
            "Accept": "application/json",
        }
        url = f"{_API_BASE}/users/@me"
        proxy = options.discord_proxy_url
        proxy_auth = self._build_proxy_auth(options)

        async with self._lock:
            try:
                timeout_cfg = aiohttp.ClientTimeout(total=15)
                async with self._session.get(
                    url,
                    headers=headers,
                    proxy=proxy,
                    timeout=timeout_cfg,
                    proxy_auth=proxy_auth,
                ) as resp:
                    status = resp.status
                    if status == 200:
                        payload = await resp.json()
                        username = str(payload.get("global_name") or payload.get("username") or "")
                        display = username or ""
                        if not display:
                            display = str(payload.get("id") or "user")
                        return TokenCheckResult(ok=True, display_name=display, status=status)
                    if status == 401:
                        return TokenCheckResult(
                            ok=False,
                            error="Discord отклонил токен (401). Проверьте правильность значения.",
                            status=status,
                        )
                    return TokenCheckResult(
                        ok=False,
                        error=f"Discord ответил статусом {status}. Попробуйте позже.",
                        status=status,
                    )
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning(
                    "Не удалось проверить Discord токен: %s",
                    exc,
                )
                return TokenCheckResult(
                    ok=False,
                    error="Не удалось обратиться к Discord. Проверьте сеть или прокси.",
                )

    async def check_proxy(
        self,
        network: NetworkOptions,
    ) -> ProxyCheckResult:
        if not network.discord_proxy_url:
            return ProxyCheckResult(ok=True)

        proxy_auth = self._build_proxy_auth(network)
        url = f"{_API_BASE}/gateway"
        headers = {
            "User-Agent": network.discord_user_agent or self._choose_user_agent(),
            "Accept": "application/json",
        }

        async with self._lock:
            try:
                timeout_cfg = aiohttp.ClientTimeout(total=10)
                async with self._session.get(
                    url,
                    headers=headers,
                    proxy=network.discord_proxy_url,
                    proxy_auth=proxy_auth,
                    timeout=timeout_cfg,
                ) as resp:
                    status = resp.status
                    if status == 200:
                        await resp.read()
                        return ProxyCheckResult(ok=True, status=status)
                    if status in {401, 407}:
                        return ProxyCheckResult(
                            ok=False,
                            error="Прокси отклоняет подключение. Проверьте логин и пароль.",
                            status=status,
                        )
                    return ProxyCheckResult(
                        ok=False,
                        error=f"Прокси вернул статус {status}.",
                        status=status,
                    )
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning(
                    "Не удалось проверить прокси для Discord: %s",
                    exc,
                )
                return ProxyCheckResult(
                    ok=False,
                    error="Не удалось подключиться к прокси. Проверьте адрес и доступность.",
                )


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
    stickers_raw = (
        payload.get("sticker_items")
        or payload.get("stickers")
        or []
    )
    member = payload.get("member") or {}
    roles_raw = member.get("roles") or []
    role_ids = {str(role_id) for role_id in roles_raw if str(role_id)}
    attachments = tuple(item for item in attachments_raw if isinstance(item, Mapping))
    embeds = tuple(item for item in embeds_raw if isinstance(item, Mapping))
    stickers = tuple(item for item in stickers_raw if isinstance(item, Mapping))

    return DiscordMessage(
        id=message_id,
        channel_id=str(payload.get("channel_id") or channel_id),
        author_id=author_id,
        author_name=author_name,
        content=content,
        attachments=attachments,
        embeds=embeds,
        stickers=stickers,
        role_ids=role_ids,
        timestamp=payload.get("timestamp"),
        edited_timestamp=payload.get("edited_timestamp"),
    )

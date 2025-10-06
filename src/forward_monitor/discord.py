"""Discord API client."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import aiohttp

from .models import DiscordMessage, NetworkOptions

_API_BASE = "https://discord.com/api/v10"
_DEFAULT_USER_AGENT = "DiscordBot (https://github.com, 1.0)"
_ROLE_CACHE_TTL = 3600.0


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TokenCheckResult:
    """Outcome of a Discord token validation attempt."""

    ok: bool
    display_name: str | None = None
    error: str | None = None
    status: int | None = None
    normalized_token: str | None = None


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
        self._role_cache: dict[str, tuple[float, dict[str, str]]] = {}

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
        before: str | None = None,
    ) -> Sequence[DiscordMessage]:
        if not self._token:
            return []

        params = {"limit": str(max(1, min(limit, 100)))}
        if after:
            params["after"] = after
        if before:
            params["before"] = before

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
        payloads = [payload for payload in data if isinstance(payload, Mapping)]
        return await self._prepare_messages(payloads, channel_id)

    async def check_channel_exists(self, channel_id: str) -> bool:
        if not self._token:
            return False

        headers = {
            "Authorization": self._token,
            "User-Agent": self._choose_user_agent(),
            "Accept": "application/json",
        }

        url = f"{_API_BASE}/channels/{channel_id}"
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
                    status = resp.status
                    if status == 200:
                        await resp.read()
                        return True
                    if status in {401, 403, 404}:
                        logger.info(
                            "Discord ответил статусом %s при проверке канала %s", status, channel_id
                        )
                        return False
                    if status >= 400:
                        logger.warning(
                            "Discord ответил статусом %s при проверке канала %s", status, channel_id
                        )
                        return False
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning(
                    "Не удалось проверить канал Discord %s: %s",
                    channel_id,
                    exc,
                )
                return False
        return False

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
        payloads = [payload for payload in data if isinstance(payload, Mapping)]
        return await self._prepare_messages(payloads, channel_id)

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

    async def _prepare_messages(
        self, payloads: Sequence[Mapping[str, Any]], channel_id: str
    ) -> Sequence[DiscordMessage]:
        if not payloads:
            return ()

        roles_to_resolve: dict[str, set[str]] = {}
        for payload in payloads:
            guild_id_raw = payload.get("guild_id")
            if not guild_id_raw:
                continue
            guild_id = str(guild_id_raw)
            mention_roles = payload.get("mention_roles") or []
            role_ids = {
                str(role_id)
                for role_id in mention_roles
                if isinstance(role_id, (str, int)) and str(role_id)
            }
            if role_ids:
                roles_to_resolve.setdefault(guild_id, set()).update(role_ids)

        role_name_map: dict[str, dict[str, str]] = {}
        for guild_id, role_ids in roles_to_resolve.items():
            resolved = await self._resolve_role_names(guild_id, role_ids)
            if resolved:
                role_name_map[guild_id] = resolved

        messages: list[DiscordMessage] = []
        for payload in payloads:
            guild_id_raw = payload.get("guild_id")
            guild_id = str(guild_id_raw) if guild_id_raw else ""
            role_names = role_name_map.get(guild_id, {})
            messages.append(_parse_message(payload, channel_id, role_names))
        return tuple(messages)

    async def _resolve_role_names(
        self, guild_id: str, role_ids: set[str]
    ) -> dict[str, str]:
        if not guild_id or not role_ids or not self._token:
            return {}

        cached = self._role_cache.get(guild_id)
        now = time.monotonic()
        cache_data = cached[1] if cached else {}
        needs_refresh = cached is None or cached[0] <= now
        missing = {role_id for role_id in role_ids if role_id not in cache_data}

        if needs_refresh or missing:
            fetched = await self._fetch_roles(guild_id)
            if fetched:
                cache_data = {**cache_data, **fetched}
                self._role_cache[guild_id] = (time.monotonic() + _ROLE_CACHE_TTL, cache_data)
            elif cached and cached[0] <= now:
                self._role_cache[guild_id] = (time.monotonic() + 300.0, cache_data)

        return {role_id: cache_data.get(role_id, role_id) for role_id in role_ids}

    async def _fetch_roles(self, guild_id: str) -> dict[str, str]:
        if not self._token:
            return {}

        headers = {
            "Authorization": self._token,
            "User-Agent": self._choose_user_agent(),
            "Accept": "application/json",
        }

        url = f"{_API_BASE}/guilds/{guild_id}/roles"
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
                    if resp.status != 200:
                        logger.debug(
                            "Не удалось получить роли гильдии %s: статус %s",
                            guild_id,
                            resp.status,
                        )
                        await resp.read()
                        return {}
                    payload = await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.debug(
                    "Ошибка при получении ролей гильдии %s: %s",
                    guild_id,
                    exc,
                )
                return {}

        roles: dict[str, str] = {}
        if isinstance(payload, Sequence):
            for item in payload:
                if not isinstance(item, Mapping):
                    continue
                role_id = str(item.get("id") or "")
                name = str(item.get("name") or "").strip()
                if role_id and name:
                    roles[role_id] = name
        return roles

    async def verify_token(
        self,
        token: str,
        *,
        network: NetworkOptions | None = None,
    ) -> TokenCheckResult:
        candidate_token = (token or "").strip()
        if not candidate_token:
            return TokenCheckResult(ok=False, error="Токен не задан")

        options = network or self._network
        user_agent = options.discord_user_agent or self._choose_user_agent()
        url = f"{_API_BASE}/users/@me"
        proxy = options.discord_proxy_url
        proxy_auth = self._build_proxy_auth(options)

        lowered = candidate_token.lower()
        if lowered.startswith("bot ") or lowered.startswith("bearer "):
            candidates = [candidate_token]
        else:
            candidates = [candidate_token, f"Bot {candidate_token}"]

        last_status: int | None = None
        last_error: str | None = None

        async with self._lock:
            for attempt, auth_token in enumerate(candidates, start=1):
                headers = {
                    "Authorization": auth_token,
                    "User-Agent": user_agent,
                    "Accept": "application/json",
                }
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
                        last_status = status
                        if status == 200:
                            payload = await resp.json()
                            username = str(
                                payload.get("global_name")
                                or payload.get("username")
                                or ""
                            )
                            display = username or str(payload.get("id") or "user")
                            normalized = auth_token
                            is_bot = bool(payload.get("bot"))
                            auth_lower = auth_token.lower()
                            if is_bot and not auth_lower.startswith("bot "):
                                normalized = f"Bot {candidate_token}"
                            elif not is_bot and auth_lower.startswith("bot "):
                                normalized = candidate_token
                            return TokenCheckResult(
                                ok=True,
                                display_name=display,
                                status=status,
                                normalized_token=normalized,
                            )
                        if status == 401:
                            await resp.read()
                            last_error = (
                                "Discord отклонил токен (401). Проверьте правильность значения."
                            )
                            if attempt < len(candidates):
                                continue
                            return TokenCheckResult(
                                ok=False,
                                error=last_error,
                                status=status,
                            )
                        await resp.read()
                        last_error = f"Discord ответил статусом {status}. Попробуйте позже."
                        if attempt < len(candidates):
                            continue
                        return TokenCheckResult(
                            ok=False,
                            error=last_error,
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

        return TokenCheckResult(
            ok=False,
            error=last_error or "Discord отклонил токен. Попробуйте позже.",
            status=last_status,
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


def _parse_message(
    payload: Mapping[str, Any], channel_id: str, role_names: Mapping[str, str]
) -> DiscordMessage:
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

    mention_users: dict[str, str] = {}
    for entry in payload.get("mentions") or []:
        if not isinstance(entry, Mapping):
            continue
        user_id = str(entry.get("id") or "")
        if not user_id:
            continue
        display = (
            str(entry.get("global_name") or "")
            or str(entry.get("username") or "")
            or str(entry.get("nick") or "")
            or str(entry.get("name") or "")
        )
        if not display and isinstance(entry.get("member"), Mapping):
            display = str(entry["member"].get("nick") or "")
        if display:
            mention_users[user_id] = display

    mention_channels: dict[str, str] = {}
    for entry in payload.get("mention_channels") or []:
        if not isinstance(entry, Mapping):
            continue
        channel_ref = str(entry.get("id") or "")
        if not channel_ref:
            continue
        name = str(entry.get("name") or "").strip()
        if name:
            mention_channels[channel_ref] = name

    mention_roles: dict[str, str] = {}
    for role_id in payload.get("mention_roles") or []:
        if not isinstance(role_id, (str, int)):
            continue
        key = str(role_id)
        if not key:
            continue
        mention_roles[key] = role_names.get(key, key)

    message_type_raw = payload.get("type")
    try:
        message_type = int(str(message_type_raw))
    except (TypeError, ValueError):
        message_type = 0

    return DiscordMessage(
        id=message_id,
        channel_id=str(payload.get("channel_id") or channel_id),
        guild_id=str(payload.get("guild_id")) if payload.get("guild_id") else None,
        author_id=author_id,
        author_name=author_name,
        content=content,
        attachments=attachments,
        embeds=embeds,
        stickers=stickers,
        role_ids=role_ids,
        mention_users=mention_users,
        mention_roles=mention_roles,
        mention_channels=mention_channels,
        timestamp=payload.get("timestamp"),
        edited_timestamp=payload.get("edited_timestamp"),
        message_type=message_type,
    )

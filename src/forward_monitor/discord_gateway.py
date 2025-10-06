"""Low-level helpers that communicate with Discord similarly to the web client."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, Mapping, MutableMapping, Sequence

import aiohttp

from .models import DiscordMessage, NetworkOptions

logger = logging.getLogger(__name__)

_GATEWAY_URL = "wss://gateway.discord.gg/?encoding=json&v=10"
_API_BASE = "https://discord.com/api/v10"
_DEFAULT_SUPER_PROPERTIES = {
    "os": "Windows",
    "browser": "Chrome",
    "device": "",
    "system_locale": "ru",
    "browser_user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "browser_version": "122.0.0.0",
    "os_version": "10",
    "referrer": "",
    "referring_domain": "",
    "referrer_current": "",
    "referring_domain_current": "",
    "release_channel": "stable",
    "client_build_number": 9999,
    "client_event_source": None,
}


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


class DiscordGateway:
    """Manage both HTTP and websocket traffic that mimics browser behaviour."""

    def __init__(self, session: aiohttp.ClientSession):
        self._session = session
        self._token: str | None = None
        self._connection: _GatewayConnection | None = None
        self._connection_lock = asyncio.Lock()

    def set_token(self, token: str | None) -> None:
        self._token = token.strip() if token else None
        if self._connection:
            self._connection.request_stop()
            self._connection = None

    def set_network_options(self, options: NetworkOptions) -> None:
        if self._connection:
            self._connection.request_stop()
            self._connection = None

    async def fetch_messages(
        self,
        channel_id: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str],
        proxy: str | None,
        proxy_auth: aiohttp.BasicAuth | None,
    ) -> Sequence[DiscordMessage]:
        connection = await self._ensure_connection(headers, proxy, proxy_auth)
        if connection is None:
            return []
        if not params.get("after"):
            await connection.bootstrap_channel(channel_id, headers, params, proxy, proxy_auth)
        return connection.get_messages(channel_id, params.get("after"), params.get("limit"))

    async def check_channel_exists(
        self,
        channel_id: str,
        *,
        headers: Mapping[str, str],
        proxy: str | None,
        proxy_auth: aiohttp.BasicAuth | None,
    ) -> bool:
        connection = await self._ensure_connection(headers, proxy, proxy_auth)
        if connection is None:
            return False
        return await connection.check_channel(headers, channel_id, proxy, proxy_auth)

    async def fetch_pinned_messages(
        self,
        channel_id: str,
        *,
        headers: Mapping[str, str],
        proxy: str | None,
        proxy_auth: aiohttp.BasicAuth | None,
    ) -> Sequence[DiscordMessage]:
        connection = await self._ensure_connection(headers, proxy, proxy_auth)
        if connection is None:
            return []
        return await connection.fetch_pins(headers, channel_id, proxy, proxy_auth)

    async def verify_token(
        self,
        token: str,
        *,
        headers: Mapping[str, str],
        proxy: str | None,
        proxy_auth: aiohttp.BasicAuth | None,
    ) -> TokenCheckResult:
        probe = _GatewayProbe(self._session, token, headers, proxy, proxy_auth)
        return await probe.run()

    async def check_proxy(
        self,
        network: NetworkOptions,
        *,
        headers: Mapping[str, str],
        proxy: str | None,
        proxy_auth: aiohttp.BasicAuth | None,
    ) -> ProxyCheckResult:
        probe = _GatewayProbe(self._session, None, headers, proxy, proxy_auth)
        return await probe.check_proxy()

    async def _ensure_connection(
        self,
        headers: Mapping[str, str],
        proxy: str | None,
        proxy_auth: aiohttp.BasicAuth | None,
    ) -> "_GatewayConnection | None":
        if not self._token:
            return None
        async with self._connection_lock:
            if self._connection and not self._connection.is_closing:
                return self._connection
            connection = _GatewayConnection(
                self._session,
                token=self._token,
                headers=headers,
                proxy=proxy,
                proxy_auth=proxy_auth,
            )
            self._connection = connection
        try:
            await connection.start()
        except Exception:
            logger.exception("Не удалось установить websocket соединение с Discord")
            self._connection = None
            return None
        return connection


class _GatewayConnection:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        token: str,
        headers: Mapping[str, str],
        proxy: str | None,
        proxy_auth: aiohttp.BasicAuth | None,
    ) -> None:
        self._session = session
        self._token = token
        self._headers = dict(headers)
        self._proxy = proxy
        self._proxy_auth = proxy_auth
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ws_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._heartbeat_interval: float = 0.0
        self._seq: int | None = None
        self._session_id: str | None = None
        self._ready_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._channel_buffers: Dict[str, Deque[DiscordMessage]] = defaultdict(lambda: deque(maxlen=500))
        self._lock = asyncio.Lock()

    @property
    def is_closing(self) -> bool:
        return self._stop_event.is_set()

    def request_stop(self) -> None:
        self._stop_event.set()
        if self._ws_task:
            self._ws_task.cancel()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()

    async def start(self) -> None:
        await self._connect()
        await self._ready_event.wait()

    async def _connect(self) -> None:
        ws_headers = dict(self._headers)
        ws_headers.setdefault("Origin", "https://discord.com")
        ws_headers.setdefault("Pragma", "no-cache")
        ws_headers.setdefault("Cache-Control", "no-cache")
        ws_headers.setdefault("Sec-WebSocket-Version", "13")
        ws_headers.setdefault("Sec-WebSocket-Extensions", "permessage-deflate; client_max_window_bits")
        ws_headers.setdefault("Upgrade", "websocket")

        self._ws = await self._session.ws_connect(
            _GATEWAY_URL,
            headers=ws_headers,
            proxy=self._proxy,
            proxy_auth=self._proxy_auth,
        )
        self._ws_task = asyncio.create_task(self._runner(), name="discord-gateway-listener")

    async def _runner(self) -> None:
        assert self._ws is not None
        ws = self._ws
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_ws_message(msg.data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка при обработке websocket от Discord")
        finally:
            self._stop_event.set()
            self._ready_event.set()
            if self._heartbeat_task:
                self._heartbeat_task.cancel()

    async def _handle_ws_message(self, payload: str) -> None:
        data = json.loads(payload)
        op = data.get("op")
        d = data.get("d")
        t = data.get("t")
        s = data.get("s")
        if isinstance(s, int):
            self._seq = s
        if op == 10:  # HELLO
            interval_ms = d.get("heartbeat_interval", 45000)
            self._heartbeat_interval = float(interval_ms) / 1000.0
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="discord-gateway-heartbeat")
            await self._send_identify()
        elif op == 11:
            return
        elif op == 0 and t == "READY":
            self._session_id = d.get("session_id")
            self._ready_event.set()
        elif op == 0 and t == "MESSAGE_CREATE":
            message = _parse_message(d, str(d.get("channel_id") or "0"))
            async with self._lock:
                self._channel_buffers[message.channel_id].append(message)
        elif op == 7:
            await self._reconnect()
        elif op == 9:
            await self._reidentify()

    async def _heartbeat_loop(self) -> None:
        assert self._ws is not None
        try:
            while not self._stop_event.is_set():
                await asyncio.sleep(max(self._heartbeat_interval, 1.0))
                payload = json.dumps({"op": 1, "d": self._seq})
                await self._ws.send_str(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Не удалось отправить heartbeat Discord", exc_info=True)

    async def _send_identify(self) -> None:
        assert self._ws is not None
        properties = dict(_DEFAULT_SUPER_PROPERTIES)
        properties["browser_user_agent"] = self._headers.get("User-Agent", properties["browser_user_agent"])
        identify = {
            "op": 2,
            "d": {
                "token": self._token,
                "capabilities": 509,
                "properties": properties,
                "compress": False,
                "presence": {
                    "status": "invisible",
                    "since": 0,
                    "activities": [],
                    "afk": False,
                },
                "client_state": {
                    "guild_versions": {},
                    "highest_last_message_id": "0",
                    "read_state_version": 0,
                    "user_guild_settings_version": -1,
                    "user_settings_version": -1,
                    "private_channels_version": "0",
                    "api_code_version": 0,
                },
            },
        }
        await self._ws.send_str(json.dumps(identify))

    async def _reconnect(self) -> None:
        if self._ws:
            await self._ws.close(code=4000)
        self.request_stop()

    async def _reidentify(self) -> None:
        if self._ws and self._session_id:
            payload = {
                "op": 6,
                "d": {
                    "token": self._token,
                    "session_id": self._session_id,
                    "seq": self._seq,
                },
            }
            await self._ws.send_str(json.dumps(payload))

    async def bootstrap_channel(
        self,
        channel_id: str,
        headers: Mapping[str, str],
        params: Mapping[str, str],
        proxy: str | None,
        proxy_auth: aiohttp.BasicAuth | None,
    ) -> None:
        url = f"{_API_BASE}/channels/{channel_id}/messages"
        timeout_cfg = aiohttp.ClientTimeout(total=15)
        try:
            async with self._session.get(
                url,
                headers=headers,
                params=params,
                proxy=proxy,
                proxy_auth=proxy_auth,
                timeout=timeout_cfg,
            ) as resp:
                if resp.status >= 400:
                    logger.warning(
                        "Discord ответил статусом %s при получении истории канала %s",
                        resp.status,
                        channel_id,
                    )
                    return
                data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("Не удалось получить историю канала Discord %s: %s", channel_id, exc)
            return
        if not isinstance(data, Sequence):
            return
        async with self._lock:
            buffer = self._channel_buffers[channel_id]
            buffer.clear()
            for payload in data:
                if isinstance(payload, Mapping):
                    buffer.append(_parse_message(payload, channel_id))

    def get_messages(
        self,
        channel_id: str,
        after: str | None,
        limit: str | None,
    ) -> Sequence[DiscordMessage]:
        try:
            limit_int = int(limit) if limit else 50
        except ValueError:
            limit_int = 50
        async_buffer = self._channel_buffers.get(channel_id)
        if not async_buffer:
            return []
        def _is_after(message: DiscordMessage) -> bool:
            if not after:
                return True
            try:
                return int(message.id) > int(after)
            except ValueError:
                return message.id > after
        filtered = [msg for msg in list(async_buffer) if _is_after(msg)]
        return filtered[-limit_int:]

    async def check_channel(
        self,
        headers: Mapping[str, str],
        channel_id: str,
        proxy: str | None,
        proxy_auth: aiohttp.BasicAuth | None,
    ) -> bool:
        url = f"{_API_BASE}/channels/{channel_id}"
        timeout_cfg = aiohttp.ClientTimeout(total=15)
        try:
            async with self._session.get(
                url,
                headers=headers,
                proxy=proxy,
                proxy_auth=proxy_auth,
                timeout=timeout_cfg,
            ) as resp:
                if resp.status == 200:
                    await resp.read()
                    return True
                if resp.status in {401, 403, 404}:
                    logger.warning(
                        "Discord ответил статусом %s при проверке канала %s",
                        resp.status,
                        channel_id,
                    )
                    return False
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("Не удалось проверить канал Discord %s: %s", channel_id, exc)
            return False
        return False

    async def fetch_pins(
        self,
        headers: Mapping[str, str],
        channel_id: str,
        proxy: str | None,
        proxy_auth: aiohttp.BasicAuth | None,
    ) -> Sequence[DiscordMessage]:
        url = f"{_API_BASE}/channels/{channel_id}/pins"
        timeout_cfg = aiohttp.ClientTimeout(total=15)
        try:
            async with self._session.get(
                url,
                headers=headers,
                proxy=proxy,
                proxy_auth=proxy_auth,
                timeout=timeout_cfg,
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
            logger.warning("Не удалось получить закреплённые сообщения Discord канала %s: %s", channel_id, exc)
            return []
        if not isinstance(data, Sequence):
            return []
        return [
            _parse_message(payload, channel_id) for payload in data if isinstance(payload, Mapping)
        ]


class _GatewayProbe:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        token: str | None,
        headers: Mapping[str, str],
        proxy: str | None,
        proxy_auth: aiohttp.BasicAuth | None,
    ) -> None:
        self._session = session
        self._token = token
        self._headers = dict(headers)
        self._proxy = proxy
        self._proxy_auth = proxy_auth

    async def run(self) -> TokenCheckResult:
        if not self._token:
            return TokenCheckResult(ok=False, error="Токен не задан")
        try:
            async with self._session.ws_connect(
                _GATEWAY_URL,
                headers=self._build_ws_headers(),
                proxy=self._proxy,
                proxy_auth=self._proxy_auth,
            ) as ws:
                ready_name: str | None = None
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    data = json.loads(msg.data)
                    op = data.get("op")
                    if op == 10:
                        await ws.send_str(json.dumps(self._identify_payload()))
                    elif op == 0 and data.get("t") == "READY":
                        payload = data.get("d") or {}
                        user = payload.get("user") or {}
                        ready_name = str(
                            user.get("global_name")
                            or user.get("username")
                            or user.get("id")
                            or "user"
                        )
                        return TokenCheckResult(ok=True, display_name=ready_name, status=101)
                    elif op == 9:
                        return TokenCheckResult(
                            ok=False,
                            error="Discord отклонил данные авторизации.",
                            status=400,
                        )
        except aiohttp.ClientProxyConnectionError:
            return TokenCheckResult(
                ok=False,
                error="Прокси отклоняет подключение. Проверьте настройки.",
                status=407,
            )
        except aiohttp.ClientResponseError as exc:
            if exc.status == 401:
                return TokenCheckResult(
                    ok=False,
                    error="Discord отклонил токен (401). Проверьте правильность значения.",
                    status=exc.status,
                )
            return TokenCheckResult(
                ok=False,
                error=f"Discord вернул статус {exc.status} при подключении к шлюзу.",
                status=exc.status,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("Не удалось подключиться к Discord через websocket: %s", exc)
            return TokenCheckResult(
                ok=False,
                error="Не удалось подключиться к Discord. Проверьте сеть или прокси.",
            )
        return TokenCheckResult(
            ok=False,
            error="Не удалось получить ответ от Discord. Попробуйте позже.",
        )

    async def check_proxy(self) -> ProxyCheckResult:
        try:
            async with self._session.ws_connect(
                _GATEWAY_URL,
                headers=self._build_ws_headers(),
                proxy=self._proxy,
                proxy_auth=self._proxy_auth,
            ) as ws:
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    data = json.loads(msg.data)
                    if data.get("op") == 10:
                        return ProxyCheckResult(ok=True, status=101)
        except aiohttp.ClientProxyConnectionError:
            return ProxyCheckResult(
                ok=False,
                error="Прокси отклоняет подключение. Проверьте логин и пароль.",
                status=407,
            )
        except aiohttp.ClientResponseError as exc:
            return ProxyCheckResult(
                ok=False,
                error=f"Discord вернул статус {exc.status} при подключении.",
                status=exc.status,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("Не удалось проверить прокси через websocket: %s", exc)
            return ProxyCheckResult(
                ok=False,
                error="Не удалось подключиться к прокси. Проверьте адрес и доступность.",
            )
        return ProxyCheckResult(ok=False, error="Не удалось установить websocket соединение.")

    def _build_ws_headers(self) -> MutableMapping[str, str]:
        headers = dict(self._headers)
        headers.setdefault("Origin", "https://discord.com")
        headers.setdefault("Pragma", "no-cache")
        headers.setdefault("Cache-Control", "no-cache")
        headers.setdefault("Sec-WebSocket-Version", "13")
        headers.setdefault(
            "Sec-WebSocket-Extensions",
            "permessage-deflate; client_max_window_bits",
        )
        headers.setdefault("Upgrade", "websocket")
        if self._token:
            headers.setdefault("Authorization", self._token)
        return headers

    def _identify_payload(self) -> Mapping[str, Any]:
        properties = dict(_DEFAULT_SUPER_PROPERTIES)
        properties["browser_user_agent"] = self._headers.get("User-Agent", properties["browser_user_agent"])
        return {
            "op": 2,
            "d": {
                "token": self._token,
                "capabilities": 509,
                "properties": properties,
                "compress": False,
                "presence": {
                    "status": "invisible",
                    "since": 0,
                    "activities": [],
                    "afk": False,
                },
                "client_state": {
                    "guild_versions": {},
                    "highest_last_message_id": "0",
                    "read_state_version": 0,
                    "user_guild_settings_version": -1,
                    "user_settings_version": -1,
                    "private_channels_version": "0",
                    "api_code_version": 0,
                },
            },
        }


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
        timestamp=payload.get("timestamp"),
        edited_timestamp=payload.get("edited_timestamp"),
        message_type=message_type,
    )

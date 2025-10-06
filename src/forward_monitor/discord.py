"""Discord client that mimics how the web application talks to Discord."""

from __future__ import annotations

import logging
from typing import Mapping, Sequence

import aiohttp

from .discord_gateway import (
    DiscordGateway,
    ProxyCheckResult,
    TokenCheckResult,
)

from .models import DiscordMessage, NetworkOptions

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


logger = logging.getLogger(__name__)
class DiscordClient:
    """High level helper that proxies calls to the browser-style gateway."""

    def __init__(self, session: aiohttp.ClientSession):
        self._session = session
        self._token: str | None = None
        self._network = NetworkOptions()
        self._gateway = DiscordGateway(session)

    def set_token(self, token: str | None) -> None:
        self._token = token.strip() if token else None
        self._gateway.set_token(self._token)

    def set_network_options(self, options: NetworkOptions) -> None:
        self._network = options
        self._gateway.set_network_options(options)

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

        messages = await self._gateway.fetch_messages(
            channel_id,
            headers=self._browser_headers(),
            params=params,
            proxy=self._network.discord_proxy_url,
            proxy_auth=self._build_proxy_auth(),
        )
        return tuple(messages)

    async def check_channel_exists(self, channel_id: str) -> bool:
        if not self._token:
            return False

        return await self._gateway.check_channel_exists(
            channel_id,
            headers=self._browser_headers(),
            proxy=self._network.discord_proxy_url,
            proxy_auth=self._build_proxy_auth(),
        )

    async def fetch_pinned_messages(self, channel_id: str) -> Sequence[DiscordMessage]:
        if not self._token:
            return []

        messages = await self._gateway.fetch_pinned_messages(
            channel_id,
            headers=self._browser_headers(),
            proxy=self._network.discord_proxy_url,
            proxy_auth=self._build_proxy_auth(),
        )
        return tuple(messages)

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

    def _browser_headers(
        self,
        *,
        token_override: str | None = None,
        network: NetworkOptions | None = None,
        skip_auth: bool = False,
    ) -> Mapping[str, str]:
        token = token_override or self._token
        ua = (network.discord_user_agent if network else None) or self._choose_user_agent()
        headers: dict[str, str] = {
            "User-Agent": ua,
            "Accept": "*/*",
            "Accept-Language": "ru,en;q=0.9",
            "Connection": "keep-alive",
            "Referer": "https://discord.com/channels/@me",
            "X-Discord-Locale": "ru",
            "X-Discord-Timezone": "Europe/Moscow",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        if not skip_auth and token:
            headers["Authorization"] = token
        return headers

    async def verify_token(
        self,
        token: str,
        *,
        network: NetworkOptions | None = None,
    ) -> TokenCheckResult:
        if not token:
            return TokenCheckResult(ok=False, error="Токен не задан")

        options = network or self._network
        return await self._gateway.verify_token(
            token,
            headers=self._browser_headers(token_override=token, network=options),
            proxy=options.discord_proxy_url,
            proxy_auth=self._build_proxy_auth(options),
        )

    async def check_proxy(
        self,
        network: NetworkOptions,
    ) -> ProxyCheckResult:
        if not network.discord_proxy_url:
            return ProxyCheckResult(ok=True)

        return await self._gateway.check_proxy(
            network,
            headers=self._browser_headers(network=network, skip_auth=True),
            proxy=network.discord_proxy_url,
            proxy_auth=self._build_proxy_auth(network),
        )

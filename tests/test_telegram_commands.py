from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Iterable, cast

from forward_monitor.config_store import ConfigStore
from forward_monitor.discord import DiscordClient, ProxyCheckResult, TokenCheckResult
from forward_monitor.models import NetworkOptions
from forward_monitor.telegram import CommandContext, TelegramController


class DummyAPI:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.commands: list[tuple[str, str]] = []

    def set_proxy(self, proxy: str | None) -> None:
        return None

    async def get_updates(
        self,
        offset: int | None = None,
        timeout: int = 30,
    ) -> list[dict[str, object]]:
        await asyncio.sleep(0)
        return []

    async def set_my_commands(self, commands: Iterable[tuple[str, str]]) -> None:
        self.commands = list(commands)

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        parse_mode: str | None = None,
        disable_preview: bool = True,
        message_thread_id: int | None = None,
    ) -> None:
        self.messages.append(text)

    async def answer_callback_query(self, callback_id: str, text: str) -> None:
        return None


class DummyDiscordClient:
    def __init__(self) -> None:
        self.tokens: list[str] = []
        self.proxies: list[str | None] = []

    async def verify_token(
        self, token: str, *, network: NetworkOptions | None = None
    ) -> TokenCheckResult:
        self.tokens.append(token)
        return TokenCheckResult(ok=True, display_name="tester")

    async def check_proxy(self, network: NetworkOptions) -> ProxyCheckResult:
        self.proxies.append(getattr(network, "discord_proxy_url", None))
        return ProxyCheckResult(ok=True)


def test_controller_adds_channel_and_updates_formatting(tmp_path: Path) -> None:
    async def runner() -> None:
        store = ConfigStore(tmp_path / "db.sqlite")
        api = DummyAPI()

        controller = TelegramController(
            api,
            store,
            discord_client=cast(DiscordClient, DummyDiscordClient()),
            on_change=lambda: None,
        )
        admin = CommandContext(
            chat_id=1,
            user_id=1,
            username="admin",
            handle="admin",
            args="",
            message={},
        )

        await controller._dispatch("claim", admin)
        admin.args = "123 456:789 Label"
        await controller._dispatch("add_channel", admin)
        record = store.get_channel("123")
        assert record is not None
        assert record.telegram_thread_id == 789

        admin.args = "123 clear"
        await controller._dispatch("set_thread", admin)
        record = store.get_channel("123")
        assert record is not None
        assert record.telegram_thread_id is None

        admin.args = "123 on"
        await controller._dispatch("set_disable_preview", admin)
        record = store.get_channel("123")
        assert record is not None
        options = dict(store.iter_channel_options(record.id))
        assert options["formatting.disable_preview"] == "true"

        admin.args = "all links"
        await controller._dispatch("set_attachments", admin)
        assert store.get_setting("formatting.attachments_style") == "links"

    import asyncio

    asyncio.run(runner())

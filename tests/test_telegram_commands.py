from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Iterable, cast

from forward_monitor.config_store import ConfigStore
from forward_monitor.discord import DiscordClient, ProxyCheckResult, TokenCheckResult
from forward_monitor.models import DiscordMessage, NetworkOptions
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

    async def send_photo(
        self,
        chat_id: int | str,
        photo: str,
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
        message_thread_id: int | None = None,
    ) -> None:
        self.messages.append(f"PHOTO:{photo}")


class DummyDiscordClient:
    def __init__(self) -> None:
        self.tokens: list[str] = []
        self.proxies: list[str | None] = []
        self.fetch_calls: list[tuple[str, int, str | None]] = []
        self.messages: list[DiscordMessage] = []
        self.checked_channels: list[str] = []
        self.existing_channels: set[str] | None = None

    def set_token(self, token: str | None) -> None:
        self.tokens.append(token or "")

    def set_network_options(self, options: NetworkOptions) -> None:
        self.proxies.append(options.discord_proxy_url)

    async def verify_token(
        self, token: str, *, network: NetworkOptions | None = None
    ) -> TokenCheckResult:
        self.tokens.append(token)
        return TokenCheckResult(ok=True, display_name="tester")

    async def check_proxy(self, network: NetworkOptions) -> ProxyCheckResult:
        self.proxies.append(getattr(network, "discord_proxy_url", None))
        return ProxyCheckResult(ok=True)

    async def fetch_messages(
        self,
        channel_id: str,
        *,
        limit: int = 50,
        after: str | None = None,
    ) -> list[DiscordMessage]:
        self.fetch_calls.append((channel_id, limit, after))
        return list(self.messages)

    async def fetch_pinned_messages(self, channel_id: str) -> list[DiscordMessage]:
        self.fetch_calls.append((channel_id, 0, None))
        return list(self.messages)

    async def check_channel_exists(self, channel_id: str) -> bool:
        self.checked_channels.append(channel_id)
        if self.existing_channels is None:
            return True
        return channel_id in self.existing_channels


def test_controller_adds_channel_and_updates_formatting(tmp_path: Path) -> None:
    async def runner() -> None:
        store = ConfigStore(tmp_path / "db.sqlite")
        store.set_setting("discord.token", "token")
        api = DummyAPI()

        dummy_client = DummyDiscordClient()
        dummy_client.messages = [
            DiscordMessage(
                id="100",
                channel_id="123",
                guild_id="guild",
                author_id="1",
                author_name="tester",
                content="",
                attachments=(),
                embeds=(),
                stickers=(),
                role_ids=set(),
            )
        ]

        controller = TelegramController(
            api,
            store,
            discord_client=cast(DiscordClient, dummy_client),
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
        assert "123" in dummy_client.checked_channels
        record = store.get_channel("123")
        assert record is not None
        assert record.telegram_thread_id == 789
        assert record.last_message_id == "100"

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

        admin.args = "all on"
        await controller._dispatch("set_discord_link", admin)
        assert store.get_setting("formatting.show_discord_link") == "true"

        admin.args = "123 pinned"
        await controller._dispatch("set_monitoring", admin)
        configs = store.load_channel_configurations()
        assert configs[0].pinned_only is True
        assert configs[0].pinned_synced is True

        admin.args = "123 messages"
        await controller._dispatch("set_monitoring", admin)
        configs = store.load_channel_configurations()
        assert configs[0].pinned_only is False
        assert configs[0].pinned_synced is False

        admin.args = "999 111 Label pinned"
        dummy_client.messages = []
        await controller._dispatch("add_channel", admin)
        assert "999" in dummy_client.checked_channels
        configs = [cfg for cfg in store.load_channel_configurations() if cfg.discord_id == "999"]
        assert configs and configs[0].pinned_only is True
        assert configs[0].pinned_synced is True

    import asyncio

    asyncio.run(runner())


def test_send_recent_forwards_messages(tmp_path: Path) -> None:
    async def runner() -> None:
        store = ConfigStore(tmp_path / "db.sqlite")
        store.set_setting("discord.token", "token")
        api = DummyAPI()
        dummy_client = DummyDiscordClient()
        dummy_client.existing_channels = {"123"}
        dummy_client.messages = [
            DiscordMessage(
                id="100",
                channel_id="123",
                guild_id="guild",
                author_id="1",
                author_name="tester",
                content="",
                attachments=(),
                embeds=(),
                stickers=(),
                role_ids=set(),
            )
        ]

        controller = TelegramController(
            api,
            store,
            discord_client=cast(DiscordClient, dummy_client),
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
        admin.args = "123 456 Label"
        await controller._dispatch("add_channel", admin)

        dummy_client.messages = [
            DiscordMessage(
                id="101",
                channel_id="123",
                guild_id="guild",
                author_id="2",
                author_name="Alice",
                content="**Bold text**",
                attachments=(),
                embeds=(),
                stickers=(),
                role_ids=set(),
            ),
            DiscordMessage(
                id="102",
                channel_id="123",
                guild_id="guild",
                author_id="3",
                author_name="Bob",
                content="",
                attachments=(
                    {"url": "https://cdn.example.com/image.png", "filename": "image.png"},
                ),
                embeds=(),
                stickers=(),
                role_ids=set(),
            ),
        ]
        api.messages.clear()

        admin.args = "2 123"
        await controller._dispatch("send_recent", admin)

        record = store.get_channel("123")
        assert record is not None
        assert record.last_message_id == "102"
        assert any(message.startswith("PHOTO:") for message in api.messages)
        assert any("<b>Bold text</b>" in message for message in api.messages)
        assert any("Всего переслано: 2" in message for message in api.messages)
        assert ("123", 2, None) in dummy_client.fetch_calls

    import asyncio

    asyncio.run(runner())

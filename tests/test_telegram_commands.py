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
        self.fetch_calls: list[tuple[str, int, str | None, str | None]] = []
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
        before: str | None = None,
    ) -> list[DiscordMessage]:
        self.fetch_calls.append((channel_id, limit, after, before))
        def _key(value: str) -> tuple[int, str]:
            return (int(value), value) if value.isdigit() else (0, value)

        filtered: list[DiscordMessage] = []
        for message in self.messages:
            if after is not None and not (_key(message.id) > _key(after)):
                continue
            if before is not None and not (_key(message.id) < _key(before)):
                continue
            filtered.append(message)
        filtered.sort(key=lambda msg: _key(msg.id), reverse=True)
        return list(filtered[:limit])

    async def fetch_pinned_messages(self, channel_id: str) -> list[DiscordMessage]:
        self.fetch_calls.append((channel_id, 0, None, None))
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


def test_send_recent_handles_pinned_messages(tmp_path: Path) -> None:
    async def runner() -> None:
        store = ConfigStore(tmp_path / "db.sqlite")
        store.set_setting("discord.token", "token")
        api = DummyAPI()
        dummy_client = DummyDiscordClient()

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
        dummy_client.messages = [
            DiscordMessage(
                id="100",
                channel_id="123",
                guild_id="guild",
                author_id="1",
                author_name="tester",
                content="bootstrap",
                attachments=(),
                embeds=(),
                stickers=(),
                role_ids=set(),
            )
        ]
        admin.args = "123 456 Label"
        await controller._dispatch("add_channel", admin)

        dummy_client.messages = [
            DiscordMessage(
                id="200",
                channel_id="123",
                guild_id="guild",
                author_id="1",
                author_name="tester",
                content="Pinned base",
                attachments=(),
                embeds=(),
                stickers=(),
                role_ids=set(),
            )
        ]
        admin.args = "123 pinned"
        await controller._dispatch("set_monitoring", admin)

        dummy_client.messages = [
            DiscordMessage(
                id="205",
                channel_id="123",
                guild_id="guild",
                author_id="2",
                author_name="Alice",
                content="New pinned 1",
                attachments=(),
                embeds=(),
                stickers=(),
                role_ids=set(),
            ),
            DiscordMessage(
                id="210",
                channel_id="123",
                guild_id="guild",
                author_id="3",
                author_name="Bob",
                content="New pinned 2",
                attachments=(),
                embeds=(),
                stickers=(),
                role_ids=set(),
            ),
            DiscordMessage(
                id="200",
                channel_id="123",
                guild_id="guild",
                author_id="1",
                author_name="tester",
                content="Pinned base",
                attachments=(),
                embeds=(),
                stickers=(),
                role_ids=set(),
            ),
        ]

        api.messages.clear()
        admin.args = "2 123"
        await controller._dispatch("send_recent", admin)

        assert any("закреплён" in message for message in api.messages)

        activity = store.load_manual_forward_activity()
        assert activity is not None
        entry = next((item for item in activity.entries if item.discord_id == "123"), None)
        assert entry is not None
        assert entry.mode == "pinned"
        assert entry.forwarded == 2

        configs = store.load_channel_configurations()
        channel_cfg = next(cfg for cfg in configs if cfg.discord_id == "123")
        assert {"205", "210"}.issubset(channel_cfg.known_pinned_ids)

    import asyncio

    asyncio.run(runner())


def test_status_reports_discord_link_and_manual_activity(tmp_path: Path) -> None:
    async def runner() -> None:
        store = ConfigStore(tmp_path / "db.sqlite")
        store.set_setting("discord.token", "token")
        api = DummyAPI()
        dummy_client = DummyDiscordClient()

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
        dummy_client.messages = [
            DiscordMessage(
                id="100",
                channel_id="123",
                guild_id="guild",
                author_id="1",
                author_name="tester",
                content="bootstrap",
                attachments=(),
                embeds=(),
                stickers=(),
                role_ids=set(),
            )
        ]
        admin.args = "123 456 Label"
        await controller._dispatch("add_channel", admin)

        admin.args = "all on"
        await controller._dispatch("set_discord_link", admin)

        dummy_client.messages = [
            DiscordMessage(
                id="101",
                channel_id="123",
                guild_id="guild",
                author_id="2",
                author_name="Alice",
                content="Recent message",
                attachments=(),
                embeds=(),
                stickers=(),
                role_ids=set(),
            )
        ]
        api.messages.clear()
        admin.args = "1 123"
        await controller._dispatch("send_recent", admin)

        api.messages.clear()
        admin.args = ""
        await controller._dispatch("status", admin)

        status_text = "\n".join(api.messages)
        assert "Ссылка на Discord: включена" in status_text
        assert "Ссылка на Discord: показывается" in status_text
        assert "📨 Ручные пересылки" in status_text
        assert "Запрошено: 1 (лимит 1), переслано: 1" in status_text
        assert "MSK" in status_text
        assert "Проверка состояния:" in status_text

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
        assert ("123", 7, None, None) in dummy_client.fetch_calls

        activity = store.load_manual_forward_activity()
        assert activity is not None
        assert activity.total_forwarded == 2
        assert activity.requested == 2
        assert activity.limit == 2
        assert activity.entries
        assert activity.entries[0].forwarded == 2
        assert activity.entries[0].mode == "messages"


    import asyncio

    asyncio.run(runner())


def test_send_recent_only_new_messages(tmp_path: Path) -> None:
    async def runner() -> None:
        store = ConfigStore(tmp_path / "db.sqlite")
        store.set_setting("discord.token", "token")
        api = DummyAPI()
        dummy_client = DummyDiscordClient()

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
        record = store.get_channel("123")
        assert record is not None
        store.set_last_message(record.id, "105")

        dummy_client.messages = [
            DiscordMessage(
                id="101",
                channel_id="123",
                guild_id="guild",
                author_id="1",
                author_name="Alice",
                content="old message",
                attachments=(),
                embeds=(),
                stickers=(),
                role_ids=set(),
            ),
            DiscordMessage(
                id="106",
                channel_id="123",
                guild_id="guild",
                author_id="2",
                author_name="Bob",
                content="new-one",
                attachments=(),
                embeds=(),
                stickers=(),
                role_ids=set(),
            ),
            DiscordMessage(
                id="107",
                channel_id="123",
                guild_id="guild",
                author_id="3",
                author_name="Carol",
                content="new-two",
                attachments=(),
                embeds=(),
                stickers=(),
                role_ids=set(),
            ),
            DiscordMessage(
                id="108",
                channel_id="123",
                guild_id="guild",
                author_id="4",
                author_name="Dave",
                content="new-three",
                attachments=(),
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
        assert record.last_message_id == "108"
        assert ("123", 7, None, None) in dummy_client.fetch_calls
        assert any("new-three" in message for message in api.messages)
        assert any("new-two" in message for message in api.messages)
        assert all("new-one" not in message for message in api.messages)
        assert any("осталось ещё 1 сообщений" in message for message in api.messages)

    import asyncio

    asyncio.run(runner())


def test_send_recent_ignores_history_when_not_requested(tmp_path: Path) -> None:
    async def runner() -> None:
        store = ConfigStore(tmp_path / "db.sqlite")
        store.set_setting("discord.token", "token")
        api = DummyAPI()
        dummy_client = DummyDiscordClient()

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
        record = store.get_channel("123")
        assert record is not None
        store.set_last_message(record.id, "205")

        dummy_client.messages = [
            DiscordMessage(
                id="210",
                channel_id="123",
                guild_id="guild",
                author_id="1",
                author_name="Alice",
                content="fresh-top",
                attachments=(),
                embeds=(),
                stickers=(),
                role_ids=set(),
            ),
            DiscordMessage(
                id="208",
                channel_id="123",
                guild_id="guild",
                author_id="2",
                author_name="Bob",
                content="fresh-second",
                attachments=(),
                embeds=(),
                stickers=(),
                role_ids=set(),
            ),
            DiscordMessage(
                id="190",
                channel_id="123",
                guild_id="guild",
                author_id="3",
                author_name="Carol",
                content="history-first",
                attachments=(),
                embeds=(),
                stickers=(),
                role_ids=set(),
            ),
            DiscordMessage(
                id="180",
                channel_id="123",
                guild_id="guild",
                author_id="4",
                author_name="Dave",
                content="history-second",
                attachments=(),
                embeds=(),
                stickers=(),
                role_ids=set(),
            ),
        ]

        api.messages.clear()
        admin.args = "3 123"
        await controller._dispatch("send_recent", admin)

        record = store.get_channel("123")
        assert record is not None
        assert record.last_message_id == "210"
        joined = "\n".join(api.messages)
        assert "fresh-top" in joined
        assert "fresh-second" in joined
        assert "history-first" not in joined
        assert not any(
            "осталось ещё" in message for message in api.messages
        )

    import asyncio

    asyncio.run(runner())


def test_set_healthcheck_updates_interval(tmp_path: Path) -> None:
    async def runner() -> None:
        store = ConfigStore(tmp_path / "db.sqlite")
        api = DummyAPI()
        dummy_client = DummyDiscordClient()

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
        admin.args = "30"
        await controller._dispatch("set_healthcheck", admin)
        assert store.get_setting("runtime.health_interval") == "30.00"
        assert api.messages[-1] == "Интервал health-check обновлён"

        admin.args = "5"
        await controller._dispatch("set_healthcheck", admin)
        assert store.get_setting("runtime.health_interval") == "30.00"
        assert api.messages[-1] == "Минимальный интервал — 10 секунд"

    import asyncio

    asyncio.run(runner())

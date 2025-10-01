from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Iterable, cast

from forward_monitor.config_store import ConfigStore
from forward_monitor.discord import DiscordClient, ProxyCheckResult, TokenCheckResult
from forward_monitor.models import DiscordMessage, NetworkOptions
from forward_monitor.telegram import BOT_COMMANDS, CommandContext, TelegramController


class DummyAPI:
    def __init__(self) -> None:
        self.messages: list[tuple[int | str, str]] = []
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
        self.messages.append((chat_id, text))

    async def answer_callback_query(self, callback_id: str, text: str) -> None:
        return None


class DummyDiscordClient:
    def __init__(self) -> None:
        self.tokens: list[str] = []
        self.proxies: list[str | None] = []
        self.set_tokens: list[str | None] = []
        self.network_options: list[NetworkOptions] = []
        self.fetch_requests: list[tuple[str, int, str | None]] = []
        self.responses: dict[str, list[DiscordMessage]] = {}

    def set_token(self, token: str | None) -> None:
        self.set_tokens.append(token)

    def set_network_options(self, options: NetworkOptions) -> None:
        self.network_options.append(options)

    async def fetch_messages(
        self,
        channel_id: str,
        *,
        limit: int = 50,
        after: str | None = None,
    ) -> tuple[DiscordMessage, ...]:
        self.fetch_requests.append((channel_id, limit, after))
        return tuple(self.responses.get(channel_id, []))

    async def verify_token(
        self, token: str, *, network: NetworkOptions | None = None
    ) -> TokenCheckResult:
        self.tokens.append(token)
        return TokenCheckResult(ok=True, display_name="tester")

    async def check_proxy(self, network: NetworkOptions) -> ProxyCheckResult:
        self.proxies.append(getattr(network, "discord_proxy_url", None))
        return ProxyCheckResult(ok=True)


def test_controller_respects_admin_permissions(tmp_path: Path) -> None:
    async def runner() -> None:
        store = ConfigStore(tmp_path / "db.sqlite")
        api = DummyAPI()
        changed = False

        def on_change() -> None:
            nonlocal changed
            changed = True

        controller = TelegramController(
            api,
            store,
            discord_client=cast(DiscordClient, DummyDiscordClient()),
            on_change=on_change,
        )
        ctx = CommandContext(
            chat_id=1,
            user_id=100,
            username="user",
            handle="user",
            args="token",
            message={},
        )

        await controller._dispatch("set_discord_token", ctx)
        assert store.get_setting("discord.token") is None
        assert api.messages == []

        await controller._dispatch("claim", ctx)
        admins = store.list_admins()
        assert len(admins) == 1
        assert admins[0].user_id == 100
        assert admins[0].username == "user"

        await controller._dispatch("set_discord_token", ctx)
        assert store.get_setting("discord.token") == "token"
        assert changed is True

    import asyncio

    asyncio.run(runner())


def test_grant_admin_by_username(tmp_path: Path) -> None:
    async def runner() -> None:
        store = ConfigStore(tmp_path / "db.sqlite")
        api = DummyAPI()
        controller = TelegramController(
            api,
            store,
            discord_client=cast(DiscordClient, DummyDiscordClient()),
            on_change=lambda: None,
        )

        store.add_admin(1, "root")
        admin_ctx = CommandContext(
            chat_id=1,
            user_id=1,
            username="Root",
            handle="root",
            args="@newbie",
            message={},
        )
        await controller._dispatch("grant", admin_ctx)
        admins = store.list_admins()
        assert any(admin.username == "newbie" for admin in admins)

        newcomer_ctx = CommandContext(
            chat_id=1,
            user_id=222,
            username="Newbie",
            handle="newbie",
            args="",
            message={},
        )
        store.remember_user(newcomer_ctx.user_id, newcomer_ctx.handle)
        await controller._dispatch("status", newcomer_ctx)
        assert any("Статус" in text for _, text in api.messages)

    asyncio.run(runner())


def test_non_admin_cannot_invoke_commands_after_admin_exists(tmp_path: Path) -> None:
    async def runner() -> None:
        store = ConfigStore(tmp_path / "db.sqlite")
        api = DummyAPI()
        controller = TelegramController(
            api,
            store,
            discord_client=cast(DiscordClient, DummyDiscordClient()),
            on_change=lambda: None,
        )

        admin_ctx = CommandContext(
            chat_id=1,
            user_id=1,
            username="Admin",
            handle="admin",
            args="",
            message={},
        )
        await controller._dispatch("claim", admin_ctx)

        outsider_ctx = CommandContext(
            chat_id=1,
            user_id=200,
            username="Visitor",
            handle="visitor",
            args="",
            message={},
        )

        before = len(api.messages)
        await controller._dispatch("status", outsider_ctx)
        assert len(api.messages) == before

        await controller._dispatch("help", outsider_ctx)
        assert len(api.messages) == before

    asyncio.run(runner())


def test_controller_handles_command_errors(tmp_path: Path) -> None:
    async def runner() -> None:
        store = ConfigStore(tmp_path / "db.sqlite")
        api = DummyAPI()
        controller = TelegramController(
            api,
            store,
            discord_client=cast(DiscordClient, DummyDiscordClient()),
            on_change=lambda: None,
        )

        admin_ctx = CommandContext(
            chat_id=1,
            user_id=1,
            username="Admin",
            handle="admin",
            args="",
            message={},
        )
        await controller._dispatch("claim", admin_ctx)

        async def failing(_: CommandContext) -> None:
            raise RuntimeError("boom")

        controller.cmd_status = failing  # type: ignore[assignment]

        before = len(api.messages)
        await controller._dispatch("status", admin_ctx)
        assert len(api.messages) == before + 1
        assert "ошиб" in api.messages[-1][1].lower()

        await controller._dispatch("help", admin_ctx)
        assert any("Основные команды" in text for _, text in api.messages)

    asyncio.run(runner())


def test_controller_registers_bot_commands(tmp_path: Path) -> None:
    async def runner() -> None:
        store = ConfigStore(tmp_path / "db.sqlite")
        api = DummyAPI()
        controller = TelegramController(
            api,
            store,
            discord_client=cast(DiscordClient, DummyDiscordClient()),
            on_change=lambda: None,
        )
        controller.stop()
        await controller.run()
        expected = [(info.name, info.summary) for info in BOT_COMMANDS]
        assert api.commands == expected

    asyncio.run(runner())


def test_my_chat_member_updates_control_channel_state(tmp_path: Path) -> None:
    async def runner() -> None:
        store = ConfigStore(tmp_path / "chat.sqlite")
        api = DummyAPI()
        discord = DummyDiscordClient()
        controller = TelegramController(
            api,
            store,
            discord_client=cast(DiscordClient, discord),
            on_change=lambda: None,
        )

        store.set_setting("discord.token", "TOKEN")
        record = store.add_channel("42", "777", "Test")
        store.set_channel_active(record.id, False)

        discord.responses["42"] = [
            DiscordMessage(
                id="500",
                channel_id="42",
                author_id="1",
                author_name="tester",
                content="hello",
                attachments=(),
                embeds=(),
                stickers=(),
            )
        ]

        update = {
            "my_chat_member": {
                "chat": {"id": 777},
                "old_chat_member": {"status": "left"},
                "new_chat_member": {"status": "member"},
            }
        }
        await controller._handle_update(update)

        refreshed = store.get_channel("42")
        assert refreshed is not None
        assert refreshed.active is True
        assert refreshed.last_message_id == "500"
        assert discord.fetch_requests == [("42", 1, None)]

        update["my_chat_member"]["old_chat_member"] = {"status": "member"}
        update["my_chat_member"]["new_chat_member"] = {"status": "kicked"}
        await controller._handle_update(update)

        refreshed = store.get_channel("42")
        assert refreshed is not None
        assert refreshed.active is False

    asyncio.run(runner())

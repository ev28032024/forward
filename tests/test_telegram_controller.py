from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Iterable

from forward_monitor.config_store import ConfigStore
from forward_monitor.telegram import CommandContext, TelegramController


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

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        parse_mode: str | None = None,
        disable_preview: bool = True,
    ) -> None:
        self.messages.append((chat_id, text))

    async def answer_callback_query(self, callback_id: str, text: str) -> None:
        return None

    async def set_commands(self, commands: Iterable[tuple[str, str]]) -> None:
        self.commands = list(commands)


def test_controller_respects_admin_permissions(tmp_path: Path) -> None:
    async def runner() -> None:
        store = ConfigStore(tmp_path / "db.sqlite")
        api = DummyAPI()
        changed = False

        def on_change() -> None:
            nonlocal changed
            changed = True

        controller = TelegramController(api, store, on_change=on_change)
        ctx = CommandContext(chat_id=1, user_id=100, username="user", args="token", message={})

        await controller._dispatch("set_discord_token", ctx)
        assert store.get_setting("discord.token") is None
        assert any("только администраторам" in message for _, message in api.messages)

        await controller._dispatch("claim", ctx)
        assert store.list_admins() == [100]

        await controller._dispatch("set_discord_token", ctx)
        assert store.get_setting("discord.token") == "token"
        assert changed is True

    import asyncio

    asyncio.run(runner())

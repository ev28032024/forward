from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Iterable

from forward_monitor.config_store import ConfigStore
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
    ) -> None:
        self.messages.append(text)

    async def answer_callback_query(self, callback_id: str, text: str) -> None:
        return None


def test_controller_adds_channel_and_updates_formatting(tmp_path: Path) -> None:
    async def runner() -> None:
        store = ConfigStore(tmp_path / "db.sqlite")
        api = DummyAPI()

        controller = TelegramController(api, store, on_change=lambda: None)
        admin = CommandContext(chat_id=1, user_id=1, username="admin", args="", message={})

        await controller._dispatch("claim", admin)
        admin.args = "123 456 Label"
        await controller._dispatch("add_channel", admin)
        assert store.get_channel("123") is not None

        admin.args = "123 привет"
        await controller._dispatch("set_header", admin)
        record = store.get_channel("123")
        assert record is not None
        options = dict(store.iter_channel_options(record.id))
        assert options["formatting.header"] == "привет"

        admin.args = "all markdown"
        await controller._dispatch("set_parse_mode", admin)
        assert store.get_setting("formatting.parse_mode") == "markdown"

    import asyncio

    asyncio.run(runner())

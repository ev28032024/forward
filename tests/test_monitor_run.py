from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from forward_monitor.config import ChannelMapping, MessageCustomization, MessageFilters, MonitorConfig
from forward_monitor import monitor


class DummySession:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self) -> "DummySession":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class DummyDiscordClient:
    def __init__(self, token: str, session: DummySession) -> None:
        self.token = token
        self.session = session


class DummyTelegramClient:
    def __init__(self, token: str, session: DummySession) -> None:
        self.token = token
        self.session = session


@pytest.mark.asyncio
async def test_run_monitor_propagates_cancellation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(monitor.aiohttp, "ClientSession", DummySession)
    monkeypatch.setattr(monitor, "DiscordClient", DummyDiscordClient)
    monkeypatch.setattr(monitor, "TelegramClient", DummyTelegramClient)

    state_file = tmp_path / "state.json"

    async def fake_sync(
        contexts,
        discord,
        telegram,
        state,
        min_delay,
        max_delay,
    ) -> None:
        state.update_last_message_id(123, "456")
        raise asyncio.CancelledError

    monkeypatch.setattr(monitor, "_sync_announcements", fake_sync)

    config = MonitorConfig(
        discord_token="token",
        telegram_token="token",
        telegram_chat_id="chat",
        announcement_channels=[
            ChannelMapping(
                discord_channel_id=123,
                telegram_chat_id="chat",
                filters=MessageFilters(),
                customization=MessageCustomization(),
            )
        ],
        poll_interval=1,
        state_file=state_file,
    )

    with pytest.raises(asyncio.CancelledError):
        await monitor.run_monitor(config)

    assert state_file.exists()
    with state_file.open("r", encoding="utf-8") as file:
        data = json.load(file)
    assert data["last_message_ids"]["123"] == "456"

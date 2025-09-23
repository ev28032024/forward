from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import TracebackType
from typing import Sequence

import pytest

from forward_monitor import monitor
from forward_monitor.config import (
    ChannelDefaults,
    ChannelMapping,
    DiscordSettings,
    MessageCustomization,
    MessageFilters,
    MonitorConfig,
    MonitorRuntime,
    NetworkSettings,
    TelegramSettings,
)


class DummySession:
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def __aenter__(self) -> "DummySession":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


class DummyDiscordClient:
    def __init__(
        self,
        token: str,
        session: DummySession,
        *,
        rate_limiter: object,
        proxy_pool: object,
        user_agents: object,
        **kwargs: object,
    ) -> None:
        self.token = token
        self.session = session
        self.rate_limiter = rate_limiter
        self.proxy_pool = proxy_pool
        self.user_agents = user_agents
        self.kwargs = kwargs


class DummyTelegramClient:
    def __init__(
        self,
        token: str,
        session: DummySession,
        *,
        rate_limiter: object,
        proxy_pool: object,
        user_agents: object,
        default_disable_preview: bool,
        default_parse_mode: str | None,
    ) -> None:
        self.token = token
        self.session = session
        self.rate_limiter = rate_limiter
        self.proxy_pool = proxy_pool
        self.user_agents = user_agents
        self.default_disable_preview = default_disable_preview
        self.default_parse_mode = default_parse_mode


@pytest.mark.asyncio
async def test_run_monitor_propagates_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(monitor.aiohttp, "ClientSession", DummySession)
    monkeypatch.setattr(monitor, "DiscordClient", DummyDiscordClient)
    monkeypatch.setattr(monitor, "TelegramClient", DummyTelegramClient)

    state_file = tmp_path / "state.json"

    async def fake_sync(
        contexts: Sequence[monitor.ChannelContext],
        discord: monitor.DiscordClient,
        telegram: monitor.TelegramClient,
        state: monitor.MonitorState,
        min_delay: float,
        max_delay: float,
        max_messages: int,
        max_fetch_seconds: float,
        api_semaphore: asyncio.Semaphore,
    ) -> None:
        state.update_last_message_id(123, "456")
        raise asyncio.CancelledError

    monkeypatch.setattr(monitor, "_sync_announcements", fake_sync)

    discord_settings = DiscordSettings(token="token")
    telegram_settings = TelegramSettings(token="token", default_chat="chat")
    defaults = ChannelDefaults(
        filters=MessageFilters(),
        customization=MessageCustomization(),
        formatting=telegram_settings.formatting,
    )
    config = MonitorConfig(
        discord=discord_settings,
        telegram=telegram_settings,
        runtime=MonitorRuntime(poll_interval=1, state_file=state_file),
        defaults=defaults,
        channels=(
            ChannelMapping(
                discord_channel_id=123,
                telegram_chat_id="chat",
                filters=MessageFilters(),
                customization=MessageCustomization(),
            ),
        ),
        network=NetworkSettings(),
    )

    with pytest.raises(asyncio.CancelledError):
        await monitor.run_monitor(config)

    assert state_file.exists()
    with state_file.open("r", encoding="utf-8") as file:
        data = json.load(file)
    assert data["last_message_ids"]["123"] == "456"


@pytest.mark.asyncio
async def test_run_monitor_logs_startup_user_agents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(monitor.aiohttp, "ClientSession", DummySession)
    monkeypatch.setattr(monitor, "DiscordClient", DummyDiscordClient)
    monkeypatch.setattr(monitor, "TelegramClient", DummyTelegramClient)

    async def fake_sync(
        *_: object,
        **__: object,
    ) -> tuple[int, int]:
        return 0, 0

    verify_calls: list[Sequence[tuple[str, object]]] = []

    async def capture_verify(
        session: DummySession,
        proxies: Sequence[tuple[str, object]],
    ) -> None:
        verify_calls.append(tuple(proxies))

    events: list[tuple[str, dict[str, object]]] = []

    def capture_event(event: str, **payload: object) -> None:
        events.append((event, payload))

    monkeypatch.setattr(monitor, "_sync_announcements", fake_sync)
    monkeypatch.setattr(monitor, "_verify_startup_proxies", capture_verify)
    monkeypatch.setattr(monitor, "log_event", capture_event)

    state_file = tmp_path / "state.json"
    discord_settings = DiscordSettings(token="token")
    telegram_settings = TelegramSettings(token="token", default_chat="chat")
    defaults = ChannelDefaults(
        filters=MessageFilters(),
        customization=MessageCustomization(),
        formatting=telegram_settings.formatting,
    )
    config = MonitorConfig(
        discord=discord_settings,
        telegram=telegram_settings,
        runtime=MonitorRuntime(poll_interval=1, state_file=state_file),
        defaults=defaults,
        channels=(
            ChannelMapping(
                discord_channel_id=1,
                telegram_chat_id="chat",
                filters=MessageFilters(),
                customization=MessageCustomization(),
            ),
        ),
        network=NetworkSettings(),
    )

    await monitor.run_monitor(config, once=True)

    services = {
        payload.get("extra", {}).get("service")
        for event, payload in events
        if event == "startup_user_agent"
    }
    assert services == {"discord", "telegram"}
    assert verify_calls, "Expected proxies to be validated before the monitor loop"


@pytest.mark.asyncio
async def test_verify_startup_proxies_fails_when_unhealthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingProxyPool:
        def has_proxies(self) -> bool:
            return True

        def endpoints(self) -> tuple[str, ...]:
            return ("http://proxy.invalid",)

        async def ensure_healthy(self, proxy: str, session: object) -> bool:
            assert proxy == "http://proxy.invalid"
            assert session is dummy_session
            return False

    events: list[tuple[str, dict[str, object]]] = []

    def capture_event(event: str, **payload: object) -> None:
        events.append((event, payload))

    dummy_session = DummySession()
    monkeypatch.setattr(monitor, "log_event", capture_event)

    with pytest.raises(RuntimeError):
        await monitor._verify_startup_proxies(
            dummy_session,
            (("telegram", FailingProxyPool()),),
        )

    failure_events = [payload for event, payload in events if event == "proxy_startup_check"]
    assert failure_events, "Expected proxy failure to be logged"
    failure = failure_events[0]
    extra = failure.get("extra", {})
    assert extra.get("service") == "telegram"
    assert extra.get("failed_proxies") == ["http://proxy.invalid"]

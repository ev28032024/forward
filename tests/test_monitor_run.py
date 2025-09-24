from __future__ import annotations

import asyncio
from pathlib import Path
from types import TracebackType
from typing import Sequence, cast

import aiohttp
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
from forward_monitor.networking import ProxyPool
from forward_monitor.types import DiscordMessage


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

    async def fake_sync(
        contexts: Sequence[monitor.ChannelContext],
        discord: monitor.DiscordClient,
        telegram: monitor.TelegramClient,
        state: monitor._RunState,
        min_delay: float,
        max_delay: float,
        max_messages: int,
        max_fetch_seconds: float,
        api_semaphore: asyncio.Semaphore,
    ) -> None:
        state.mark_forwarded(123, "456")
        assert state.last_seen(123) == "456"
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
        runtime=MonitorRuntime(poll_interval=1, state_file=tmp_path / "state.json"),
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
        payload_copy: dict[str, object] = dict(payload)
        events.append((event, payload_copy))

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

    services: set[str] = set()
    for event, payload in events:
        if event != "startup_user_agent":
            continue
        extra = payload.get("extra")
        if not isinstance(extra, dict):
            continue
        service = extra.get("service")
        if isinstance(service, str):
            services.add(service)
    assert services == {"discord", "telegram"}
    assert verify_calls, "Expected proxies to be validated before the monitor loop"


@pytest.mark.asyncio
async def test_sync_announcements_ignores_existing_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapping = ChannelMapping(discord_channel_id=1, telegram_chat_id="chat")
    context = monitor.ChannelContext(
        mapping=mapping,
        filters=mapping.filters.prepare(),
        customization=mapping.customization.prepare(),
        formatting=mapping.formatting,
    )

    backlog: list[DiscordMessage] = [{"id": "100"}, {"id": "101"}]
    new_messages: list[DiscordMessage] = [{"id": "102"}]
    fetch_calls: list[str | None] = []

    async def fake_fetch_channel_messages(
        ctx: monitor.ChannelContext,
        discord: monitor.DiscordService,
        last_seen: str | None,
        *,
        limit: int,
        max_messages: int,
        max_duration_seconds: float,
        api_semaphore: asyncio.Semaphore,
    ) -> monitor._ChannelFetchResult:
        assert ctx is context
        fetch_calls.append(last_seen)
        messages = backlog if len(fetch_calls) == 1 else new_messages
        return monitor._ChannelFetchResult(ctx, messages, False, False)

    forwarded: list[str] = []

    async def fake_forward_message(**kwargs: object) -> bool:
        message = kwargs.get("message")
        assert isinstance(message, dict)
        forwarded.append(str(message.get("id")))
        return True

    monkeypatch.setattr(monitor, "_fetch_channel_messages", fake_fetch_channel_messages)
    monkeypatch.setattr(monitor, "_forward_message", fake_forward_message)

    state = monitor._RunState()

    first_result = await monitor._sync_announcements(
        (context,),
        cast(monitor.DiscordService, object()),
        cast(monitor.TelegramSender, object()),
        state,
        min_delay=0,
        max_delay=0,
        max_messages=10,
        max_fetch_seconds=5.0,
        api_semaphore=asyncio.Semaphore(1),
    )

    assert first_result == (0, 0)
    assert forwarded == []
    assert state.is_initialised(1)
    assert state.last_seen(1) == "101"
    assert fetch_calls == [None]

    second_result = await monitor._sync_announcements(
        (context,),
        cast(monitor.DiscordService, object()),
        cast(monitor.TelegramSender, object()),
        state,
        min_delay=0,
        max_delay=0,
        max_messages=10,
        max_fetch_seconds=5.0,
        api_semaphore=asyncio.Semaphore(1),
    )

    assert second_result == (1, 1)
    assert forwarded == ["102"]
    assert fetch_calls[-1] == "101"


@pytest.mark.asyncio
async def test_verify_startup_proxies_fails_when_unhealthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingProxyPool:
        def has_proxies(self) -> bool:
            return True

        def endpoints(self) -> tuple[str, ...]:
            return ("http://proxy.invalid",)

        async def ensure_healthy(self, proxy: str | None, session: aiohttp.ClientSession) -> bool:
            assert proxy == "http://proxy.invalid"
            session_obj = cast(DummySession, session)
            assert session_obj is dummy_session
            return False

    events: list[tuple[str, dict[str, object]]] = []

    def capture_event(event: str, **payload: object) -> None:
        payload_copy: dict[str, object] = dict(payload)
        events.append((event, payload_copy))

    dummy_session = DummySession()
    monkeypatch.setattr(monitor, "log_event", capture_event)

    with pytest.raises(RuntimeError):
        await monitor._verify_startup_proxies(
            cast(aiohttp.ClientSession, dummy_session),
            (("telegram", cast(ProxyPool, FailingProxyPool())),),
        )

    failure_events = [payload for event, payload in events if event == "proxy_startup_check"]
    assert failure_events, "Expected proxy failure to be logged"
    failure = failure_events[0]
    extra_obj = failure.get("extra", {})
    extra = cast(dict[str, object], extra_obj)
    assert extra.get("service") == "telegram"
    assert extra.get("failed_proxies") == ["http://proxy.invalid"]

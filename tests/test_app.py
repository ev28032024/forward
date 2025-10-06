from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import cast

from forward_monitor.app import ForwardMonitorApp, HealthUpdate
from forward_monitor.config_store import ConfigStore
from forward_monitor.discord import DiscordClient, ProxyCheckResult, TokenCheckResult
from forward_monitor.models import NetworkOptions
from forward_monitor.telegram import TelegramAPI


class DummyDiscordClient:
    def __init__(self) -> None:
        self.token: str | None = None
        self.network: NetworkOptions | None = None
        self.fetch_calls: list[str] = []
        self.verify_calls: list[str] = []
        self.channel_checks: list[str] = []

    def set_token(self, token: str | None) -> None:
        self.token = token

    def set_network_options(self, options: NetworkOptions) -> None:
        self.network = options

    async def fetch_messages(
        self,
        channel_id: str,
        *,
        limit: int = 50,
        after: str | None = None,
        before: str | None = None,
    ) -> list[dict[str, object]]:
        self.fetch_calls.append(channel_id)
        return []

    async def fetch_pinned_messages(self, channel_id: str) -> list[dict[str, object]]:
        return []

    async def check_channel_exists(self, channel_id: str) -> bool:
        self.channel_checks.append(channel_id)
        return False

    async def check_proxy(self, network: NetworkOptions) -> ProxyCheckResult:
        return ProxyCheckResult(ok=True)

    async def verify_token(
        self,
        token: str,
        *,
        network: NetworkOptions | None = None,
    ) -> TokenCheckResult:
        self.verify_calls.append(token)
        return TokenCheckResult(ok=False, error="bad token", status=401)


class DummyTelegramAPI:
    def __init__(self) -> None:
        self.messages: list[tuple[int | str, str]] = []

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

    async def send_photo(
        self,
        chat_id: int | str,
        photo: str,
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
        message_thread_id: int | None = None,
    ) -> None:
        return None


async def _run_monitor_with_health(
    app: ForwardMonitorApp,
    discord: DummyDiscordClient,
    telegram: DummyTelegramAPI,
) -> None:
    monitor_task = asyncio.create_task(
        app._monitor_loop(cast(DiscordClient, discord), cast(TelegramAPI, telegram))
    )
    health_task = asyncio.create_task(
        app._healthcheck_loop(
            cast(DiscordClient, discord),
            cast(TelegramAPI, telegram),
            interval_override=0.05,
        )
    )

    try:
        await asyncio.sleep(0.2)
    finally:
        monitor_task.cancel()
        health_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await monitor_task
        with contextlib.suppress(asyncio.CancelledError):
            await health_task


def test_monitor_waits_for_health_before_processing(tmp_path: Path) -> None:
    async def runner() -> None:
        db_path = tmp_path / "db.sqlite"
        store = ConfigStore(db_path)
        store.set_setting("discord.token", "bad-token")
        store.set_setting("runtime.poll", "0.1")
        store.add_channel("123", "456", label="Test")

        app = ForwardMonitorApp(db_path=db_path, telegram_token="token")
        discord = DummyDiscordClient()
        telegram = DummyTelegramAPI()

        await _run_monitor_with_health(app, discord, telegram)

        assert discord.fetch_calls == []
        assert discord.verify_calls != []
        app._store.close()

    asyncio.run(runner())


def test_health_check_skips_when_proxy_fails(tmp_path: Path) -> None:
    async def runner() -> None:
        db_path = tmp_path / "db.sqlite"
        store = ConfigStore(db_path)
        store.set_setting("discord.token", "token")
        store.add_channel("123", "456", label="Test")
        store.set_setting("proxy.discord.url", "http://proxy.local")

        class ProxyFailDiscord(DummyDiscordClient):
            def __init__(self) -> None:
                super().__init__()
                self.verify_attempts = 0

            async def check_proxy(self, network: NetworkOptions) -> ProxyCheckResult:
                return ProxyCheckResult(ok=False, error="proxy down")

            async def verify_token(
                self, token: str, *, network: NetworkOptions | None = None
            ) -> TokenCheckResult:
                self.verify_attempts += 1
                return TokenCheckResult(ok=True)

            async def check_channel_exists(self, channel_id: str) -> bool:
                raise AssertionError("channel check should not be called when proxy fails")

        app = ForwardMonitorApp(db_path=db_path, telegram_token="token")
        discord = ProxyFailDiscord()
        telegram = DummyTelegramAPI()

        state = app._reload_state()
        await app._run_health_checks(
            state,
            cast(DiscordClient, discord),
            cast(TelegramAPI, telegram),
        )

        assert discord.verify_attempts == 0
        status, message = store.get_health_status("discord_token")
        assert status == "unknown"
        assert message == "Проверка недоступна: прокси не отвечает."
        channel_status, channel_message = store.get_health_status("channel.123")
        assert channel_status == "unknown"
        assert channel_message == "Проверка недоступна: прокси не отвечает."
        app._store.close()

    asyncio.run(runner())


def test_run_health_checks_configures_client(tmp_path: Path) -> None:
    async def runner() -> None:
        db_path = tmp_path / "db.sqlite"
        store = ConfigStore(db_path)
        store.set_setting("discord.token", "token-123")
        store.add_channel("123", "456", label="Test")

        app = ForwardMonitorApp(db_path=db_path, telegram_token="token")
        discord = DummyDiscordClient()
        telegram = DummyTelegramAPI()

        state = app._reload_state()
        await app._run_health_checks(
            state,
            cast(DiscordClient, discord),
            cast(TelegramAPI, telegram),
        )

        assert discord.token == "token-123"
        assert discord.network is not None
        app._store.close()

    asyncio.run(runner())


def test_app_restores_existing_health_status(tmp_path: Path) -> None:
    async def runner() -> None:
        db_path = tmp_path / "db.sqlite"
        store = ConfigStore(db_path)
        store.add_channel("123", "456", label="Test")
        store.set_health_status(
            "channel.123", "error", "Discord канал недоступен или нет прав."
        )
        store.close()

        app = ForwardMonitorApp(db_path=db_path, telegram_token="token")
        assert app._health_status.get("channel.123") == "error"

        telegram = DummyTelegramAPI()
        update = HealthUpdate(
            key="channel.123",
            status="error",
            message="Discord канал недоступен или нет прав.",
            label="Test",
        )
        await app._emit_health_notifications([update], cast(TelegramAPI, telegram))

        assert telegram.messages == []
        app._store.close()

    asyncio.run(runner())


def test_health_check_normalizes_stored_token(tmp_path: Path) -> None:
    async def runner() -> None:
        db_path = tmp_path / "db.sqlite"
        store = ConfigStore(db_path)
        store.set_setting("discord.token", "raw-token")
        store.add_channel("123", "456", label="Test")

        class NormalizingDiscord(DummyDiscordClient):
            async def verify_token(
                self,
                token: str,
                *,
                network: NetworkOptions | None = None,
            ) -> TokenCheckResult:
                self.verify_calls.append(token)
                return TokenCheckResult(
                    ok=True,
                    display_name="bot",
                    normalized_token="Bot raw-token",
                )

            async def check_channel_exists(self, channel_id: str) -> bool:
                self.channel_checks.append(channel_id)
                return True

        app = ForwardMonitorApp(db_path=db_path, telegram_token="token")
        discord = NormalizingDiscord()
        telegram = DummyTelegramAPI()

        state = app._reload_state()
        await app._run_health_checks(
            state,
            cast(DiscordClient, discord),
            cast(TelegramAPI, telegram),
        )

        assert store.get_setting("discord.token") == "Bot raw-token"
        assert discord.token == "Bot raw-token"

        store.close()
        app._store.close()

    asyncio.run(runner())

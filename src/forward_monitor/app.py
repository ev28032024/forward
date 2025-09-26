"""Application bootstrap for Forward Monitor."""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from .config_store import ConfigStore
from .discord import DiscordClient
from .filters import FilterEngine
from .formatting import format_discord_message
from .models import ChannelConfig, NetworkOptions, RuntimeOptions
from .telegram import TelegramAPI, TelegramController, send_formatted
from .utils import RateLimiter

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MonitorState:
    channels: list[ChannelConfig]
    runtime: RuntimeOptions
    network: NetworkOptions
    discord_token: str | None


class ForwardMonitorApp:
    """High level coordinator tying together Discord, Telegram and configuration."""

    def __init__(self, *, db_path: Path, telegram_token: str):
        self._store = ConfigStore(db_path)
        self._telegram_token = telegram_token
        self._refresh_event = asyncio.Event()
        self._refresh_event.set()

    async def run(self) -> None:
        async with aiohttp.ClientSession() as session:
            discord_client = DiscordClient(session)
            telegram_api = TelegramAPI(self._telegram_token, session)
            controller = TelegramController(
                telegram_api,
                self._store,
                discord_client=discord_client,
                on_change=self._signal_refresh,
            )

            monitor_task = asyncio.create_task(
                self._monitor_loop(discord_client, telegram_api), name="forward-monitor"
            )
            bot_task = asyncio.create_task(controller.run(), name="telegram-controller")

            await asyncio.gather(monitor_task, bot_task)

    def _signal_refresh(self) -> None:
        self._refresh_event.set()

    async def _monitor_loop(
        self,
        discord_client: DiscordClient,
        telegram_api: TelegramAPI,
    ) -> None:
        runtime = self._load_runtime()
        discord_rate = RateLimiter(runtime.rate_per_second)
        telegram_rate = RateLimiter(runtime.rate_per_second)
        state = MonitorState(
            channels=[],
            runtime=runtime,
            network=self._store.load_network_options(),
            discord_token=self._store.get_setting("discord.token"),
        )

        while True:
            if self._refresh_event.is_set():
                self._refresh_event.clear()
                state = self._reload_state()
                discord_client.set_token(state.discord_token)
                discord_client.set_network_options(state.network)
                discord_rate.update_rate(state.runtime.rate_per_second)
                telegram_rate.update_rate(state.runtime.rate_per_second)
                logger.info("Конфигурация обновлена: %d каналов", len(state.channels))

            if not state.discord_token:
                await asyncio.sleep(3.0)
                continue

            for channel in list(state.channels):
                if self._refresh_event.is_set():
                    break
                await discord_rate.wait()
                await self._process_channel(
                    channel,
                    discord_client,
                    telegram_api,
                    telegram_rate,
                    state.runtime,
                )

            try:
                await asyncio.wait_for(
                    self._refresh_event.wait(),
                    timeout=state.runtime.poll_interval,
                )
            except asyncio.TimeoutError:
                pass

    async def _process_channel(
        self,
        channel: ChannelConfig,
        discord_client: DiscordClient,
        telegram_api: TelegramAPI,
        telegram_rate: RateLimiter,
        runtime: RuntimeOptions,
    ) -> None:
        if not channel.active:
            return

        messages = await discord_client.fetch_messages(
            channel.discord_id,
            after=channel.last_message_id,
        )
        if not messages:
            return

        def sort_key(message_id: str) -> tuple[int, str]:
            return (int(message_id), message_id) if message_id.isdigit() else (0, message_id)

        ordered = sorted(messages, key=lambda msg: sort_key(msg.id))
        engine = FilterEngine(channel.filters)
        last_seen = channel.last_message_id
        for msg in ordered:
            decision = engine.evaluate(msg)
            last_seen = msg.id
            if not decision.allowed:
                continue
            formatted = format_discord_message(msg, channel)
            await telegram_rate.wait()
            await send_formatted(telegram_api, channel.telegram_chat_id, formatted)
            await self._sleep_within(runtime)

        if last_seen and channel.storage_id is not None:
            self._store.set_last_message(channel.storage_id, last_seen)
            channel.last_message_id = last_seen

    async def _sleep_within(self, runtime: RuntimeOptions) -> None:
        delay_ms = 0
        if runtime.max_delay_ms > 0:
            delay_ms = random.randint(runtime.min_delay_ms, runtime.max_delay_ms)
        if delay_ms:
            await asyncio.sleep(delay_ms / 1000)

    def _reload_state(self) -> MonitorState:
        runtime = self._load_runtime()
        network = self._store.load_network_options()
        channels = self._store.load_channel_configurations()
        discord_token = self._store.get_setting("discord.token")
        return MonitorState(
            channels=channels,
            runtime=runtime,
            network=network,
            discord_token=discord_token,
        )

    def _load_runtime(self) -> RuntimeOptions:
        def _float(key: str, default: float) -> float:
            value = self._store.get_setting(key)
            if value is None:
                return default
            try:
                return float(value)
            except ValueError:
                return default

        def _int(key: str, default: int) -> int:
            value = self._store.get_setting(key)
            if value is None:
                return default
            try:
                return int(value)
            except ValueError:
                return default

        rate = self._store.get_setting("runtime.rate")
        if rate is not None:
            try:
                rate_value = float(rate)
            except ValueError:
                rate_value = 8.0
        else:
            # Backwards compatibility with older split settings
            legacy_discord = _float("runtime.discord_rate", 8.0)
            legacy_telegram = _float("runtime.telegram_rate", legacy_discord)
            rate_value = max(legacy_discord, legacy_telegram)

        return RuntimeOptions(
            poll_interval=_float("runtime.poll", 2.0),
            min_delay_ms=_int("runtime.delay_min", 0),
            max_delay_ms=_int("runtime.delay_max", 0),
            rate_per_second=rate_value,
        )

    def _load_network_options(self) -> NetworkOptions:
        return self._store.load_network_options()

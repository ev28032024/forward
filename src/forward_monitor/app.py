"""Application bootstrap for Forward Monitor."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import aiohttp

from .config_store import ConfigStore
from .discord import DiscordClient
from .filters import FilterEngine
from .formatting import format_discord_message
from .models import ChannelConfig, NetworkOptions, RuntimeOptions
from .telegram import TelegramAPI, TelegramController, send_formatted
from .utils import RateLimiter, parse_delay_setting


def _parse_discord_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

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

            async def run_monitor() -> None:
                await self._monitor_loop(discord_client, telegram_api)

            async def run_controller() -> None:
                await controller.run()

            monitor_task = asyncio.create_task(
                self._supervise("forward-monitor", run_monitor),
                name="forward-monitor-supervisor",
            )
            bot_task = asyncio.create_task(
                self._supervise("telegram-controller", run_controller),
                name="telegram-controller-supervisor",
            )

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
                try:
                    await self._process_channel(
                        channel,
                        discord_client,
                        telegram_api,
                        telegram_rate,
                        state.runtime,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Ошибка при обработке канала Discord %s", channel.discord_id
                    )
                    await asyncio.sleep(1.0)

            try:
                await asyncio.wait_for(
                    self._refresh_event.wait(),
                    timeout=state.runtime.poll_interval,
                )
            except asyncio.TimeoutError:
                pass

    async def _supervise(
        self,
        name: str,
        factory: Callable[[], Awaitable[None]],
        *,
        retry_delay: float = 5.0,
    ) -> None:
        while True:
            try:
                await factory()
            except asyncio.CancelledError:
                logger.info("Задача %s остановлена", name)
                raise
            except Exception:
                logger.exception("Задача %s завершилась с ошибкой", name)
            else:
                logger.warning("Задача %s завершилась неожиданно, будет перезапущена", name)
            await asyncio.sleep(retry_delay)

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

        if channel.pinned_only:
            await self._process_pinned_channel(
                channel,
                discord_client,
                telegram_api,
                telegram_rate,
                runtime,
            )
            return

        baseline = channel.added_at
        bootstrap = channel.last_message_id is None

        try:
            messages = await discord_client.fetch_messages(
                channel.discord_id,
                after=channel.last_message_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Ошибка при запросе сообщений Discord для канала %s",
                channel.discord_id,
            )
            await asyncio.sleep(1.0)
            return
        if not messages:
            return

        def sort_key(message_id: str) -> tuple[int, str]:
            return (int(message_id), message_id) if message_id.isdigit() else (0, message_id)

        ordered = sorted(messages, key=lambda msg: sort_key(msg.id))
        engine = FilterEngine(channel.filters)
        last_seen = channel.last_message_id
        interrupted = False
        for msg in ordered:
            if self._refresh_event.is_set():
                interrupted = True
                break
            candidate_id = msg.id
            if bootstrap:
                if baseline is not None:
                    msg_time = _parse_discord_timestamp(msg.timestamp)
                    if msg_time is not None and msg_time < baseline:
                        last_seen = candidate_id
                        continue
                bootstrap = False
            decision = engine.evaluate(msg)
            if not decision.allowed:
                last_seen = candidate_id
                continue
            formatted = format_discord_message(msg, channel)
            await telegram_rate.wait()
            if self._refresh_event.is_set():
                interrupted = True
                break
            try:
                await send_formatted(
                    telegram_api,
                    channel.telegram_chat_id,
                    formatted,
                    thread_id=channel.telegram_thread_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Не удалось отправить сообщение %s в Telegram чат %s",
                    msg.id,
                    channel.telegram_chat_id,
                )
                last_seen = candidate_id
                continue
            last_seen = candidate_id
            await self._sleep_within(runtime)

        if not interrupted and last_seen and channel.storage_id is not None:
            self._store.set_last_message(channel.storage_id, last_seen)
            channel.last_message_id = last_seen

    async def _process_pinned_channel(
        self,
        channel: ChannelConfig,
        discord_client: DiscordClient,
        telegram_api: TelegramAPI,
        telegram_rate: RateLimiter,
        runtime: RuntimeOptions,
    ) -> None:
        try:
            messages = await discord_client.fetch_pinned_messages(channel.discord_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Ошибка при запросе закреплённых сообщений Discord для канала %s",
                channel.discord_id,
            )
            await asyncio.sleep(1.0)
            return

        current_ids = {msg.id for msg in messages}
        previous_known = set(channel.known_pinned_ids)

        if not channel.pinned_synced:
            if channel.storage_id is not None:
                self._store.set_known_pinned_messages(channel.storage_id, current_ids)
                self._store.set_pinned_synced(channel.storage_id, synced=True)
                channel.known_pinned_ids = set(current_ids)
                channel.pinned_synced = True
            else:
                channel.pinned_synced = True
            return

        new_ids = current_ids - previous_known

        if not messages:
            if channel.storage_id is not None and channel.known_pinned_ids:
                self._store.set_known_pinned_messages(channel.storage_id, [])
                channel.known_pinned_ids = set()
            return

        if not new_ids and previous_known == current_ids:
            return

        def sort_key(message_id: str) -> tuple[int, str]:
            return (int(message_id), message_id) if message_id.isdigit() else (0, message_id)

        engine = FilterEngine(channel.filters)
        ordered = sorted(
            (msg for msg in messages if msg.id in new_ids),
            key=lambda msg: sort_key(msg.id),
        )
        processed_ids: set[str] = set()
        interrupted = False

        for msg in ordered:
            if self._refresh_event.is_set():
                interrupted = True
                break
            decision = engine.evaluate(msg)
            if not decision.allowed:
                processed_ids.add(msg.id)
                continue
            formatted = format_discord_message(msg, channel)
            await telegram_rate.wait()
            if self._refresh_event.is_set():
                interrupted = True
                break
            try:
                await send_formatted(
                    telegram_api,
                    channel.telegram_chat_id,
                    formatted,
                    thread_id=channel.telegram_thread_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Не удалось отправить закреплённое сообщение %s в Telegram чат %s",
                    msg.id,
                    channel.telegram_chat_id,
                )
                continue
            processed_ids.add(msg.id)
            await self._sleep_within(runtime)

        if channel.storage_id is None:
            return

        base_known = previous_known & current_ids
        if interrupted:
            updated_known = base_known | processed_ids
        else:
            updated_known = current_ids

        if updated_known != channel.known_pinned_ids:
            self._store.set_known_pinned_messages(channel.storage_id, updated_known)
            self._store.set_pinned_synced(channel.storage_id, synced=True)
            channel.known_pinned_ids = updated_known
            channel.pinned_synced = True

    async def _sleep_within(self, runtime: RuntimeOptions) -> None:
        delay_seconds = 0.0
        if runtime.max_delay_seconds > 0:
            delay_seconds = random.uniform(
                runtime.min_delay_seconds, runtime.max_delay_seconds
            )
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)

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

        min_delay = parse_delay_setting(self._store.get_setting("runtime.delay_min"), 0.0)
        max_delay = parse_delay_setting(self._store.get_setting("runtime.delay_max"), 0.0)
        if max_delay < min_delay:
            max_delay = min_delay

        return RuntimeOptions(
            poll_interval=_float("runtime.poll", 2.0),
            min_delay_seconds=min_delay,
            max_delay_seconds=max_delay,
            rate_per_second=rate_value,
        )

    def _load_network_options(self) -> NetworkOptions:
        return self._store.load_network_options()

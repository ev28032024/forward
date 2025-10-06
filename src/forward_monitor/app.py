"""Application bootstrap for Forward Monitor."""

from __future__ import annotations

import asyncio
import html
import logging
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
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


def _discord_snowflake_from_datetime(moment: datetime | None) -> int | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    epoch = datetime(2015, 1, 1, tzinfo=timezone.utc)
    delta = moment - epoch
    milliseconds = int(delta.total_seconds() * 1000)
    if milliseconds < 0:
        return 0
    return milliseconds << 22

logger = logging.getLogger(__name__)

_FORWARDABLE_MESSAGE_TYPES: set[int] = {0, 19, 20, 21, 23}


@dataclass(slots=True)
class MonitorState:
    channels: list[ChannelConfig]
    runtime: RuntimeOptions
    network: NetworkOptions
    discord_token: str | None


@dataclass(slots=True)
class HealthUpdate:
    """Single subject health result produced by the checker."""

    key: str
    status: str
    message: str | None
    label: str


class ForwardMonitorApp:
    """High level coordinator tying together Discord, Telegram and configuration."""

    def __init__(self, *, db_path: Path, telegram_token: str):
        self._store = ConfigStore(db_path)
        self._telegram_token = telegram_token
        self._refresh_event = asyncio.Event()
        self._refresh_event.set()
        self._health_wakeup = asyncio.Event()
        self._health_status: dict[str, str] = {}

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
            health_task = asyncio.create_task(
                self._supervise(
                    "health-monitor",
                    lambda: self._healthcheck_loop(discord_client, telegram_api),
                ),
                name="health-monitor-supervisor",
            )

            await asyncio.gather(monitor_task, bot_task, health_task)

    def _signal_refresh(self) -> None:
        self._refresh_event.set()
        self._health_wakeup.set()

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

    async def _healthcheck_loop(
        self,
        discord_client: DiscordClient,
        telegram_api: TelegramAPI,
        *,
        interval: float = 180.0,
    ) -> None:
        first_iteration = True
        while True:
            if first_iteration:
                first_iteration = False
                if self._health_wakeup.is_set():
                    self._health_wakeup.clear()
            else:
                try:
                    await asyncio.wait_for(self._health_wakeup.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass
                else:
                    self._health_wakeup.clear()

            state = self._reload_state()
            await self._run_health_checks(state, discord_client, telegram_api)

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

    async def _run_health_checks(
        self,
        state: MonitorState,
        discord_client: DiscordClient,
        telegram_api: TelegramAPI,
    ) -> None:
        updates: list[HealthUpdate] = []

        proxy_result = await discord_client.check_proxy(state.network)
        if state.network.discord_proxy_url:
            proxy_status = "ok" if proxy_result.ok else "error"
            proxy_message = proxy_result.error
        else:
            proxy_status = "disabled"
            proxy_message = None
        updates.append(
            HealthUpdate(
                key="proxy",
                status=proxy_status,
                message=proxy_message,
                label="Прокси Discord",
            )
        )

        token = state.discord_token or ""
        token_result = await discord_client.verify_token(token, network=state.network)
        token_status = "ok" if token_result.ok else "error"
        token_message = token_result.error
        updates.append(
            HealthUpdate(
                key="discord_token",
                status=token_status,
                message=token_message,
                label="Discord токен",
            )
        )

        channel_ids: set[str] = {channel.discord_id for channel in state.channels}
        check_rate = RateLimiter(max(1.0, state.runtime.rate_per_second))
        for channel in state.channels:
            key = f"channel.{channel.discord_id}"
            label = f"Канал {channel.label or channel.discord_id}"
            if not channel.active:
                updates.append(
                    HealthUpdate(key=key, status="disabled", message=None, label=label)
                )
                continue
            if token_status != "ok":
                updates.append(
                    HealthUpdate(
                        key=key,
                        status="unknown",
                        message="Проверка недоступна: нет валидного токена Discord.",
                        label=label,
                    )
                )
                continue
            await check_rate.wait()
            exists = await discord_client.check_channel_exists(channel.discord_id)
            if exists:
                updates.append(HealthUpdate(key=key, status="ok", message=None, label=label))
            else:
                updates.append(
                    HealthUpdate(
                        key=key,
                        status="error",
                        message="Discord канал недоступен или нет прав.",
                        label=label,
                    )
                )

        self._store.clean_channel_health_statuses(channel_ids)
        for update in updates:
            self._store.set_health_status(update.key, update.status, update.message)

        current_keys = {update.key for update in updates}
        for key in list(self._health_status.keys()):
            if key.startswith("channel.") and key not in current_keys:
                self._health_status.pop(key, None)

        await self._emit_health_notifications(updates, telegram_api)

    async def _emit_health_notifications(
        self, updates: list[HealthUpdate], telegram_api: TelegramAPI
    ) -> None:
        errors: list[HealthUpdate] = []
        recoveries: list[HealthUpdate] = []
        for update in updates:
            previous = self._health_status.get(update.key)
            if previous == update.status:
                continue
            self._health_status[update.key] = update.status
            if update.status == "error":
                logger.warning(
                    "Проблема со здоровьем компонента %s: %s",
                    update.key,
                    update.message,
                )
                errors.append(update)
            elif previous == "error" and update.status == "ok":
                recoveries.append(update)

        if errors:
            message = self._format_health_summary(errors, recovered=False)
            await self._notify_admins(telegram_api, message)
        if recoveries:
            message = self._format_health_summary(recoveries, recovered=True)
            await self._notify_admins(telegram_api, message)
        if errors or recoveries:
            self._refresh_event.set()

    async def _notify_admins(self, telegram_api: TelegramAPI, message: str) -> None:
        for admin in self._store.list_admins():
            if admin.user_id is None:
                continue
            try:
                await telegram_api.send_message(
                    admin.user_id,
                    message,
                    parse_mode="HTML",
                )
            except Exception:
                logger.exception(
                    "Не удалось отправить уведомление администратору %s", admin.user_id
                )

    def _format_health_summary(
        self, updates: Sequence[HealthUpdate], *, recovered: bool
    ) -> str:
        if not updates:
            return ""
        if recovered:
            header = "✅ <b>Компоненты восстановлены</b>"
            lines = [header, ""]
            for update in updates:
                lines.append(f"• {html.escape(update.label)}")
            return "\n".join(lines)

        header = "🔴 <b>Обнаружены проблемы</b>"
        lines = [header, ""]
        for update in updates:
            description = html.escape(update.message or "Причина не указана.")
            lines.append(f"• <b>{html.escape(update.label)}</b> — {description}")
        return "\n".join(lines)

    async def _process_channel(
        self,
        channel: ChannelConfig,
        discord_client: DiscordClient,
        telegram_api: TelegramAPI,
        telegram_rate: RateLimiter,
        runtime: RuntimeOptions,
    ) -> None:
        if not channel.active or channel.blocked_by_health:
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
        baseline_marker = _discord_snowflake_from_datetime(baseline)
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
            if msg.message_type not in _FORWARDABLE_MESSAGE_TYPES and not (
                msg.attachments or msg.embeds
            ):
                last_seen = candidate_id
                continue
            if bootstrap:
                if baseline_marker is not None and candidate_id.isdigit():
                    marker = baseline_marker
                    candidate_numeric = int(candidate_id)
                    if candidate_numeric <= marker:
                        last_seen = candidate_id
                        continue
                if baseline is not None:
                    baseline_ts = baseline
                    msg_time = _parse_discord_timestamp(msg.timestamp)
                    if msg_time is not None and msg_time <= baseline_ts:
                        last_seen = candidate_id
                        continue
                bootstrap = False
            decision = engine.evaluate(msg)
            if not decision.allowed:
                last_seen = candidate_id
                continue
            formatted = format_discord_message(msg, channel, message_kind="message")
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
        if not channel.active or channel.blocked_by_health:
            return
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
            formatted = format_discord_message(msg, channel, message_kind="pinned")
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

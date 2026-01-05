"""Telegram bot facade for configuration and message delivery."""

from __future__ import annotations

import asyncio
import html
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Iterable,
    Mapping,
    Protocol,
    Sequence,
)
from urllib.parse import urlparse

import aiohttp

from .config_store import (
    AdminRecord,
    ConfigStore,
    ManualForwardEntry,
    format_filter_value,
    normalize_filter_value,
)
from .deduplication import MessageDeduplicator, build_message_signature
from .discord import DiscordClient
from .filters import FilterEngine
from .formatting import format_discord_message
from .models import DiscordMessage, FilterConfig, FormattedTelegramMessage
from .utils import (
    ChannelProcessingGuard,
    RateLimiter,
    as_moscow_time,
    normalize_username,
    parse_bool,
    parse_delay_setting,
)

if TYPE_CHECKING:
    from .models import ChannelConfig

_API_BASE = "https://api.telegram.org"

_FILTER_LABELS = {
    "whitelist": "белый список",
    "blacklist": "чёрный список",
    "allowed_senders": "разрешённые авторы",
    "blocked_senders": "запрещённые авторы",
    "allowed_types": "разрешённые типы",
    "blocked_types": "запрещённые типы",
    "allowed_roles": "разрешённые роли",
    "blocked_roles": "запрещённые роли",
}

_FILTER_TYPES: tuple[str, ...] = tuple(_FILTER_LABELS.keys())

_NBSP = "\u00A0"
_INDENT = _NBSP * 2
_DOUBLE_INDENT = _NBSP * 4

_FORWARDABLE_MESSAGE_TYPES: set[int] = {0, 19, 20, 21, 23}


_HEALTH_ICONS = {
    "ok": "🟢",
    "error": "🔴",
    "unknown": "🟡",
    "disabled": "⚪️",
}


def _health_icon(status: str) -> str:
    return _HEALTH_ICONS.get(status, "🟢")


def _format_seconds(value: float) -> str:
    return (f"{value:.2f}").rstrip("0").rstrip(".") or "0"


def _format_rate(value: float) -> str:
    formatted = f"{value:.2f}"
    if "." in formatted:
        formatted = formatted.rstrip("0")
        if formatted.endswith("."):
            formatted += "0"
    return formatted


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_discord_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_discord_url(value: str) -> str | None:
    """Extract channel/thread ID from Discord URL or return None if not a URL.

    Supports:
    - Forum thread: discord.com/channels/{guild}/{forum}/threads/{thread_id}
    - Regular channel: discord.com/channels/{guild}/{channel_id}
    """
    import re

    if not value.startswith(("http://", "https://", "discord.com")):
        return None

    # Forum thread: /channels/{guild}/{forum}/threads/{thread_id}
    thread_match = re.search(r"discord\.com/channels/\d+/\d+/threads/(\d+)", value)
    if thread_match:
        return thread_match.group(1)

    # Regular channel: /channels/{guild}/{channel_id}
    channel_match = re.search(r"discord\.com/channels/\d+/(\d+)", value)
    if channel_match:
        return channel_match.group(1)

    return None


def _message_timestamp(message: "DiscordMessage") -> datetime | None:
    """Return the original creation timestamp for sorting purposes."""
    created = _parse_discord_timestamp(message.timestamp)
    if created is not None:
        return created
    return _parse_discord_timestamp(message.edited_timestamp)



def _message_id_sort_key(message_id: str) -> tuple[int, str]:
    return (
        (int(message_id), message_id)
        if message_id.isdigit()
        else (0, message_id)
    )


def _message_order_key(message: "DiscordMessage") -> tuple[datetime, tuple[int, str]]:
    moment = _message_timestamp(message)
    if moment is None:
        moment = datetime.fromtimestamp(0, timezone.utc)
    return (moment, _message_id_sort_key(message.id))


def _prepare_recent_messages(
    messages: Sequence["DiscordMessage"], *, invocation_time: datetime
) -> list["DiscordMessage"]:
    """Deduplicate and sort messages up to the invocation moment."""

    seen_ids: set[str] = set()
    epoch = datetime.fromtimestamp(0, timezone.utc)
    sortable: list[tuple[tuple[datetime, tuple[int, str], int], "DiscordMessage"]] = []
    for index, message in enumerate(messages):
        if message.id in seen_ids:
            continue
        seen_ids.add(message.id)
        moment = _message_timestamp(message)
        if moment is not None and moment > invocation_time:
            continue
        key = (
            moment or epoch,
            _message_id_sort_key(message.id),
            index,
        )
        sortable.append((key, message))

    sortable.sort(key=lambda item: item[0])
    return [message for _, message in sortable]


def _normalize_label(label: str | None, fallback: str) -> str:
    candidate = (label or "").strip()
    return candidate or fallback


def _chat_sort_key(chat_id: str) -> tuple[int, str]:
    try:
        numeric = int(chat_id)
    except (TypeError, ValueError):
        return (1, chat_id.lower())
    return (0, f"{numeric:020d}")


def _channel_sort_key(
    label: str, discord_id: str, thread_id: int | None
) -> tuple[str, int, tuple[int, str]]:
    normalized = _normalize_label(label, discord_id).casefold()
    thread_sort = -1 if thread_id is None else thread_id
    return (normalized, thread_sort, _message_id_sort_key(discord_id))


def _panel_header(title: str, icon: str) -> str:
    return f"<b>{icon} {html.escape(title)}</b>"


def _panel_note(text: str, *, escape: bool = True) -> str:
    content = html.escape(text) if escape else text
    return f"<i>{content}</i>"


def _panel_bullet(
    text: str, *, indent: int = 1, icon: str | None = None
) -> str:
    prefix = _INDENT * indent
    bullet = f"{icon} " if icon else "• "
    return f"{prefix}{bullet}{text}"


def _panel_message(
    title: str,
    *,
    icon: str,
    description: str | None = None,
    description_escape: bool = True,
    rows: Sequence[str] = (),
) -> str:
    lines = [_panel_header(title, icon)]
    if description is not None:
        lines.append(_panel_note(description, escape=description_escape))
    if rows:
        if description is not None:
            lines.append("")
        lines.extend(rows)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _group_channels_by_chat_and_thread(
    records: Sequence[Any],
) -> dict[str, dict[int | None, list[Any]]]:
    grouped: dict[str, dict[int | None, list[Any]]] = {}
    for record in records:
        chat_id = getattr(record, "telegram_chat_id")
        thread_id = getattr(record, "telegram_thread_id", None)
        grouped.setdefault(chat_id, {}).setdefault(thread_id, []).append(record)
    return grouped


def _thread_sort_key(thread_id: int | None) -> tuple[int, int]:
    if thread_id is None:
        return (0, 0)
    return (1, thread_id)


def _format_thread_title(thread_id: int | None) -> str:
    if thread_id is None:
        return "Основной чат"
    return f"Тема <code>{html.escape(str(thread_id))}</code>"


def _format_channel_groups(
    grouped: Mapping[str, Mapping[int | None, Sequence[Any]]],
    *,
    render_entry: Callable[[Any], tuple[str, str, Sequence[tuple[int, str | None, str]]]],
) -> list[str]:
    lines: list[str] = []
    for chat_id, threads in sorted(grouped.items(), key=lambda item: _chat_sort_key(item[0])):
        escaped_chat = html.escape(chat_id)
        total = sum(len(items) for items in threads.values())
        lines.append(
            f"<b>Telegram <code>{escaped_chat}</code></b> · "
            f"{total} "
            + ("связка" if total == 1 else "связки")
        )
        for index, (thread_id, items) in enumerate(
            sorted(threads.items(), key=lambda item: _thread_sort_key(item[0]))
        ):
            if index > 0:
                lines.append("")
            lines.append(f"{_INDENT}🧵 <b>{_format_thread_title(thread_id)}</b>")
            for record in sorted(
                items,
                key=lambda item: _channel_sort_key(
                    getattr(item, "label", item.discord_id),
                    item.discord_id,
                    getattr(item, "telegram_thread_id", None),
                ),
            ):
                icon, header, extra_rows = render_entry(record)
                lines.append(_panel_bullet(header, indent=2, icon=icon))
                for indent_level, extra_icon, extra_text in extra_rows:
                    lines.append(
                        _panel_bullet(extra_text, indent=indent_level, icon=extra_icon)
                    )
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    return lines


class TelegramAPIProtocol(Protocol):
    async def get_updates(
        self,
        offset: int | None = None,
        timeout: int = 30,
    ) -> list[dict[str, Any]]: ...

    async def set_my_commands(self, commands: Iterable[tuple[str, str]]) -> None: ...

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        parse_mode: str | None = None,
        disable_preview: bool = True,
        message_thread_id: int | None = None,
    ) -> None: ...

    async def send_photo(
        self,
        chat_id: int | str,
        photo: str,
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
        message_thread_id: int | None = None,
    ) -> None: ...

    async def answer_callback_query(self, callback_id: str, text: str) -> None: ...


class TelegramAPI:
    """Lightweight Telegram Bot API wrapper."""

    def __init__(self, token: str, session: aiohttp.ClientSession):
        self._token = token
        self._session = session

    async def get_updates(
        self,
        offset: int | None = None,
        timeout: int = 30,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        url = f"{_API_BASE}/bot{self._token}/getUpdates"
        try:
            timeout_cfg = aiohttp.ClientTimeout(total=timeout + 5)
            async with self._session.get(
                url,
                params=params,
                timeout=timeout_cfg,
            ) as resp:
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return []
        if not payload.get("ok"):
            return []
        return list(payload.get("result") or [])

    async def set_my_commands(self, commands: Iterable[tuple[str, str]]) -> None:
        url = f"{_API_BASE}/bot{self._token}/setMyCommands"
        payload = {
            "commands": [
                {"command": name, "description": description[:256]}
                for name, description in commands
            ]
        }
        try:
            timeout_cfg = aiohttp.ClientTimeout(total=15)
            async with self._session.post(
                url,
                json=payload,
                timeout=timeout_cfg,
            ) as resp:
                await resp.read()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        parse_mode: str | None = None,
        disable_preview: bool = True,
        message_thread_id: int | None = None,
    ) -> None:
        url = f"{_API_BASE}/bot{self._token}/sendMessage"
        data: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_preview,
        }
        if parse_mode:
            data["parse_mode"] = parse_mode
        if message_thread_id is not None:
            data["message_thread_id"] = message_thread_id
        try:
            timeout_cfg = aiohttp.ClientTimeout(total=15)
            async with self._session.post(
                url,
                json=data,
                timeout=timeout_cfg,
            ) as resp:
                await resp.read()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return

    async def answer_callback_query(self, callback_id: str, text: str) -> None:
        url = f"{_API_BASE}/bot{self._token}/answerCallbackQuery"
        data = {"callback_query_id": callback_id, "text": text[:200]}
        try:
            timeout_cfg = aiohttp.ClientTimeout(total=10)
            async with self._session.post(
                url,
                json=data,
                timeout=timeout_cfg,
            ) as resp:
                await resp.read()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return

    async def send_photo(
        self,
        chat_id: int | str,
        photo: str,
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
        message_thread_id: int | None = None,
    ) -> None:
        url = f"{_API_BASE}/bot{self._token}/sendPhoto"
        data: dict[str, Any] = {"chat_id": chat_id, "photo": photo}
        if caption:
            data["caption"] = caption
        if parse_mode:
            data["parse_mode"] = parse_mode
        if message_thread_id is not None:
            data["message_thread_id"] = message_thread_id
        try:
            timeout_cfg = aiohttp.ClientTimeout(total=15)
            async with self._session.post(
                url,
                json=data,
                timeout=timeout_cfg,
            ) as resp:
                await resp.read()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return


@dataclass(slots=True)
class CommandContext:
    chat_id: int
    user_id: int
    username: str
    handle: str | None
    args: str
    message: dict[str, Any]


AdminCheck = Callable[[str], bool]


@dataclass(frozen=True, slots=True)
class _CommandInfo:
    name: str
    summary: str
    help_text: str
    admin_only: bool = True


BOT_COMMANDS: tuple[_CommandInfo, ...] = (
    _CommandInfo(
        name="start",
        summary="Приветствие бота.",
        help_text="/start — короткое приветствие и напоминание про /help.",
        admin_only=False,
    ),
    _CommandInfo(
        name="help",
        summary="Показать справку.",
        help_text="/help — открыть структурированное описание команд.",
        admin_only=False,
    ),
    _CommandInfo(
        name="claim",
        summary="Назначить себя администратором.",
        help_text="/claim — стать администратором (если список пуст)",
        admin_only=False,
    ),
    _CommandInfo(
        name="status",
        summary="Краткий обзор настроек.",
        help_text="/status — показать токен, сеть, фильтры и каналы.",
    ),
    _CommandInfo(
        name="admins",
        summary="Показать администраторов.",
        help_text="/admins — показать администраторов",
    ),
    _CommandInfo(
        name="grant",
        summary="Выдать права администрирования.",
        help_text="/grant <id|@username> — выдать права",
    ),
    _CommandInfo(
        name="revoke",
        summary="Отозвать права администрирования.",
        help_text="/revoke <id|@username> — отобрать права",
    ),
    _CommandInfo(
        name="set_discord_token",
        summary="Сохранить токен Discord.",
        help_text="/set_discord_token <token>",
    ),
    _CommandInfo(
        name="add_channel",
        summary="Добавить связку каналов.",
        help_text="/add_channel <discord_id> <telegram_chat[:thread]> <название>",
    ),
    _CommandInfo(
        name="set_thread",
        summary="Настроить тему Telegram.",
        help_text="/set_thread <discord_id> <thread_id|clear>",
    ),
    _CommandInfo(
        name="remove_channel",
        summary="Удалить связку каналов.",
        help_text="/remove_channel <discord_id>",
    ),
    _CommandInfo(
        name="list_channels",
        summary="Показать все связки каналов.",
        help_text="/list_channels",
    ),
    _CommandInfo(
        name="set_disable_preview",
        summary="Управлять предпросмотром ссылок.",
        help_text="/set_disable_preview <discord_id|all> <on|off>",
    ),
    _CommandInfo(
        name="set_max_length",
        summary="Ограничить длину сообщений.",
        help_text="/set_max_length <discord_id|all> <число>",
    ),
    _CommandInfo(
        name="set_attachments",
        summary="Выбрать стиль вложений.",
        help_text="/set_attachments <discord_id|all> <summary|links>",
    ),
    _CommandInfo(
        name="set_discord_link",
        summary="Управлять ссылкой на сообщение Discord.",
        help_text="/set_discord_link <discord_id|all> <on|off>",
    ),
    _CommandInfo(
        name="set_monitoring",
        summary="Выбрать режим мониторинга сообщений.",
        help_text="/set_monitoring <discord_id|all> <messages|pinned>",
    ),
    _CommandInfo(
        name="set_duplicate_filter",
        summary="Фильтровать дублирующиеся сообщения.",
        help_text="/set_duplicate_filter <discord_id|all> <on|off>",
    ),
    _CommandInfo(
        name="add_filter",
        summary="Добавить фильтр сообщений.",
        help_text="/add_filter <discord_id|all> <тип> <значение>",
    ),
    _CommandInfo(
        name="clear_filter",
        summary="Удалить фильтры сообщений.",
        help_text="/clear_filter <discord_id|all> <тип> [значение]",
    ),
    _CommandInfo(
        name="send_recent",
        summary="Ручная пересылка последних сообщений.",
        help_text="/send_recent <кол-во> [discord_id|all]",
    ),
    _CommandInfo(
        name="set_proxy",
        summary="Настроить прокси Discord.",
        help_text="/set_proxy <url|clear> [логин] [пароль]",
    ),
    _CommandInfo(
        name="set_user_agent",
        summary="Сохранить user-agent Discord.",
        help_text="/set_user_agent <значение>",
    ),
    _CommandInfo(
        name="set_poll",
        summary="Изменить интервал опроса Discord.",
        help_text="/set_poll <секунды>",
    ),
    _CommandInfo(
        name="set_healthcheck",
        summary="Настроить интервал health-check.",
        help_text="/set_healthcheck <секунды>",
    ),
    _CommandInfo(
        name="set_delay",
        summary="Настроить случайную задержку отправки.",
        help_text="/set_delay <min_s> <max_s>",
    ),
    _CommandInfo(
        name="set_rate",
        summary="Настроить лимиты запросов.",
        help_text="/set_rate <в_секунду>",
    ),
)

_COMMAND_MAP = {info.name: info for info in BOT_COMMANDS}


class TelegramController:
    """Interactive Telegram bot that manages runtime configuration."""

    def __init__(
        self,
        api: TelegramAPIProtocol,
        store: ConfigStore,
        *,
        discord_client: DiscordClient,
        on_change: Callable[[], None],
        channel_guard: ChannelProcessingGuard | None = None,
        deduplicator: MessageDeduplicator | None = None,
    ) -> None:
        self._api = api
        self._store = store
        self._discord = discord_client
        stored_offset = self._store.get_telegram_offset()
        self._offset = stored_offset if stored_offset is not None else 0
        self._running = True
        self._on_change = on_change
        self._commands_registered = False
        self._stop_requested = False
        self._channel_guard = channel_guard
        self._deduplicator = deduplicator or MessageDeduplicator()

    async def run(self) -> None:
        self._running = True
        await self._ensure_commands_registered()
        if self._stop_requested:
            self._stop_requested = False
            self._running = False
            return
        try:
            while self._running:
                updates = await self._api.get_updates(self._offset, timeout=25)
                highest_offset = self._offset
                for update in updates:
                    update_offset = self._extract_update_offset(update)
                    if update_offset is not None and update_offset > highest_offset:
                        highest_offset = update_offset
                    await self._handle_update(update)
                if highest_offset != self._offset:
                    self._offset = highest_offset
                    self._store.set_telegram_offset(highest_offset)
        finally:
            self._stop_requested = False

    def stop(self) -> None:
        """Stop the controller loop on the next iteration."""

        self._running = False
        self._stop_requested = True

    async def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") or update.get("edited_message")
        if not message:
            return
        text = str(message.get("text") or "").strip()
        if not text.startswith("/"):
            return
        command, _, args = text.partition(" ")
        command = command.split("@")[0][1:].lower()
        sender = message["from"]
        handle_raw = sender.get("username")
        display_name = str(handle_raw or sender.get("first_name") or "user")
        ctx = CommandContext(
            chat_id=int(message["chat"]["id"]),
            user_id=int(sender["id"]),
            username=display_name,
            handle=str(handle_raw) if handle_raw else None,
            args=args.strip(),
            message=message,
        )
        self._store.remember_user(ctx.user_id, ctx.handle)
        await self._dispatch(command, ctx)

    async def _dispatch(self, command: str, ctx: CommandContext) -> None:
        # Игнорируем команды из групп - бот отвечает только в личных сообщениях
        chat_type = ctx.message.get("chat", {}).get("type", "")
        if chat_type != "private":
            return

        handler = getattr(self, f"cmd_{command}", None)
        if handler is None:
            if self._is_admin(ctx):
                await self._send_panel_message(
                    ctx,
                    title="Команда не найдена",
                    icon="ℹ️",
                    description=(
                        f"Не удалось распознать <code>/{html.escape(command)}</code>. "
                        "Откройте <code>/help</code> для полного списка."
                    ),
                    description_escape=False,
                )
            return

        info = _COMMAND_MAP.get(command)
        has_admins = self._store.has_admins()
        is_admin = self._is_admin(ctx)

        if command == "claim" and not has_admins:
            await self._execute_command(handler, ctx, notify_on_error=True)
            return

        if not is_admin:
            if not has_admins:
                if info is not None and not info.admin_only:
                    await self._execute_command(
                        handler, ctx, notify_on_error=False
                    )
                return
            if info is not None and not info.admin_only:
                await self._execute_command(handler, ctx, notify_on_error=False)
                return
            await self._notify_access_denied(ctx)
            return

        await self._execute_command(handler, ctx, notify_on_error=True)

    async def _execute_command(
        self,
        handler: Callable[[CommandContext], Awaitable[None]],
        ctx: CommandContext,
        *,
        notify_on_error: bool,
    ) -> None:
        try:
            await handler(ctx)
        except asyncio.CancelledError:
            raise
        except sqlite3.Error:
            logger.exception("Database error while executing command %s", handler.__name__)
            if notify_on_error:
                await self._api.send_message(
                    ctx.chat_id,
                    (
                        "⚠️ <b>Ошибка базы данных</b>\n"
                        "Не удалось выполнить команду. Попробуйте немного позже."
                    ),
                    parse_mode="HTML",
                )
        except Exception:
            logger.exception("Unexpected error while executing command %s", handler.__name__)
            if notify_on_error:
                await self._api.send_message(
                    ctx.chat_id,
                    (
                        "⚠️ <b>Внутренняя ошибка</b>\n"
                        "Команда завершилась неудачно. Попробуйте повторить позже."
                    ),
                    parse_mode="HTML",
                )

    def _is_admin(self, ctx: CommandContext) -> bool:
        normalized_handle = normalize_username(ctx.handle)
        for admin in self._store.list_admins():
            if admin.user_id is not None and admin.user_id == ctx.user_id:
                return True
            if (
                normalized_handle
                and admin.username is not None
                and admin.username.lower() == normalized_handle
            ):
                return True
        return False

    def _format_admin(self, admin: AdminRecord) -> str:
        parts: list[str] = []
        if admin.username:
            parts.append(f"@{html.escape(admin.username)}")
        if admin.user_id is not None:
            parts.append(f"<code>{admin.user_id}</code>")
        if not parts:
            return "—"
        return " / ".join(parts)

    async def _send_panel_message(
        self,
        ctx: CommandContext,
        *,
        title: str,
        icon: str = "ℹ️",
        description: str | None = None,
        description_escape: bool = True,
        rows: Sequence[str] = (),
    ) -> None:
        message = _panel_message(
            title,
            icon=icon,
            description=description,
            description_escape=description_escape,
            rows=rows,
        )
        await self._api.send_message(
            ctx.chat_id,
            message,
            parse_mode="HTML",
        )

    async def _send_usage_error(
        self,
        ctx: CommandContext,
        usage: str,
        *,
        tip: str | None = None,
    ) -> None:
        rows = [
            _panel_bullet(
                f"Использование: <code>{html.escape(usage)}</code>",
                icon="📌",
            )
        ]
        if tip:
            rows.append(_panel_bullet(html.escape(tip), icon="💡"))
        await self._send_panel_message(
            ctx,
            title="Неверный ввод",
            icon="⚠️",
            rows=rows,
        )

    async def _send_status_notice(
        self,
        ctx: CommandContext,
        *,
        title: str,
        icon: str,
        message: str,
        message_icon: str,
        escape: bool = True,
    ) -> None:
        rows = [_panel_bullet(message if not escape else html.escape(message), icon=message_icon)]
        await self._send_panel_message(
            ctx,
            title=title,
            icon=icon,
            rows=rows,
            description=None,
            description_escape=True,
        )

    @staticmethod
    def _extract_update_offset(update: dict[str, Any]) -> int | None:
        try:
            update_id = int(update.get("update_id", 0))
        except (TypeError, ValueError):
            return None
        return max(0, update_id + 1)

    # ------------------------------------------------------------------
    # Basic commands
    # ------------------------------------------------------------------
    async def cmd_start(self, ctx: CommandContext) -> None:
        await self._send_panel_message(
            ctx,
            title="Forward Monitor",
            icon="👋",
            description=(
                "Готов помочь настроить пересылку. "
                "Откройте <code>/help</code>, чтобы перейти в панель управления."
            ),
            description_escape=False,
            rows=[
                _panel_bullet(
                    "Используйте команды ниже, чтобы управлять настройками.",
                    icon="🛠️",
                )
            ],
        )

    async def cmd_help(self, ctx: CommandContext) -> None:
        sections: list[tuple[str, list[tuple[str, str]]]] = [
            (
                "🚀 Старт",
                [
                    ("/start", "Приветствие и проверка связи."),
                    ("/help", "Эта справка."),
                    ("/status", "Краткий отчёт по настройкам."),
                ],
            ),
            (
                "👑 Администрирование",
                [
                    ("/claim", "Занять роль первого администратора."),
                    ("/admins", "Показать текущий список администраторов."),
                    ("/grant <id|@user>", "Выдать права указанному пользователю."),
                    ("/revoke <id|@user>", "Удалить права администратора."),
                ],
            ),
            (
                "📡 Каналы",
                [
                    (
                        (
                            "/add_channel <discord_id> <telegram_chat[:thread]> <название>"
                            " [messages|pinned]"
                        ),
                        "Создать новую связку, выбрать тему, режим и задать имя.",
                    ),
                    (
                        "/set_thread <discord_id> <thread_id|clear>",
                        "Обновить тему в существующей связке.",
                    ),
                    ("/remove_channel <discord_id>", "Удалить связку."),
                    ("/list_channels", "Краткий перечень настроенных связок."),
                ],
            ),
            (
                "⚙️ Подключение",
                [
                    ("/set_discord_token <token>", "Сохранить токен Discord."),
                    ("/set_proxy <url|clear> [логин] [пароль]", "Настроить или отключить прокси."),
                    ("/set_user_agent <строка>", "Передавать собственный User-Agent."),
                ],
            ),
            (
                "⏱ Режим работы",
                [
                    ("/set_poll <секунды>", "Частота опроса Discord."),
                    (
                        "/set_healthcheck <секунды>",
                        "Частота проверки состояния каналов.",
                    ),
                    ("/set_delay <min_s> <max_s>", "Случайная пауза между сообщениями."),
                    ("/set_rate <в_секунду>", "Общий лимит запросов в секунду."),
                ],
            ),
            (
                "🧹 Отображение и фильтры",
                [
                    (
                        "/set_disable_preview <discord_id|all> <on|off>",
                        "Управлять предпросмотром ссылок.",
                    ),
                    (
                        "/set_max_length <discord_id|all> <число>",
                        "Разбивать длинные тексты на части.",
                    ),
                    (
                        "/set_attachments <discord_id|all> <summary|links>",
                        "Выбрать формат блока вложений.",
                    ),
                    (
                        "/set_monitoring <discord_id|all> <messages|pinned>",
                        "Выбрать режим (новые или закреплённые сообщения).",
                    ),
                    (
                        "/set_duplicate_filter <discord_id|all> <on|off>",
                        "Фильтровать дубликаты глобально или для конкретного канала.",
                    ),
                    (
                        "/add_filter <discord_id|all> <тип> <значение>",
                        "Добавить фильтр (whitelist, blacklist и т.д.).",
                    ),
                    (
                        "/clear_filter <discord_id|all> <тип> [значение]",
                        "Удалить фильтр полностью или по значению.",
                    ),
                ],
            ),
            (
                "📨 Дополнительно",
                [
                    (
                        "/set_discord_link <discord_id|all> <on|off>",
                        "Прикреплять ссылку на оригинальное сообщение в Discord.",
                    ),
                    (
                        "/send_recent <кол-во> [discord_id|all]",
                        "Ручная пересылка свежих сообщений из канала.",
                    ),
                ],
            ),
        ]
        lines = [
            "<b>🛠️ Forward Monitor • Основные команды</b>",
            "<i>Все настройки выполняются из этого чата: категории ниже.</i>",
            "",
            _panel_bullet(
                "Для <code>/add_channel</code> можно указать режим <code>messages</code> "
                "или <code>pinned</code> в конце команды, чтобы выбрать тип мониторинга.",
                icon="💡",
            ),
            "",
        ]
        for title, commands in sections:
            lines.append(f"<b>{title}</b>")
            for command, description in commands:
                lines.append(f"• <code>{html.escape(command)}</code> — {html.escape(description)}")
            lines.append("")
        while lines and lines[-1] == "":
            lines.pop()

        for chunk in _split_html_lines(lines):
            await self._api.send_message(
                ctx.chat_id,
                chunk,
                parse_mode="HTML",
            )

    async def cmd_status(self, ctx: CommandContext) -> None:
        token_value = self._store.get_setting("discord.token")
        token_status = "есть" if token_value else "не задан"
        token_health, token_message = self._store.get_health_status("discord_token")

        proxy_url = self._store.get_setting("proxy.discord.url")
        proxy_login = self._store.get_setting("proxy.discord.login")
        proxy_password = self._store.get_setting("proxy.discord.password")
        proxy_health, proxy_message = self._store.get_health_status("proxy")

        user_agent = self._store.get_setting("ua.discord") or "стандартный"
        poll = self._store.get_setting("runtime.poll") or "2.0"
        delay_min_raw = self._store.get_setting("runtime.delay_min")
        delay_max_raw = self._store.get_setting("runtime.delay_max")
        delay_min_value = parse_delay_setting(delay_min_raw, 0.0)
        delay_max_value = parse_delay_setting(delay_max_raw, 0.0)
        if delay_max_value < delay_min_value:
            delay_max_value = delay_min_value

        def _safe_float(value: str | None, fallback: float) -> float:
            if value is None:
                return fallback
            try:
                return float(value)
            except ValueError:
                return fallback

        health_interval_raw = self._store.get_setting("runtime.health_interval")
        health_interval_value = _safe_float(health_interval_raw, 180.0)
        if health_interval_value < 10.0:
            health_interval_value = 10.0

        rate_value: float | None
        rate_raw = self._store.get_setting("runtime.rate")
        if rate_raw is not None:
            try:
                rate_value = float(rate_raw)
            except ValueError:
                rate_value = None
        else:
            rate_value = None

        if rate_value is None:
            legacy_discord = _safe_float(self._store.get_setting("runtime.discord_rate"), 4.0)
            legacy_telegram = _safe_float(
                self._store.get_setting("runtime.telegram_rate"), legacy_discord
            )
            rate_value = max(legacy_discord, legacy_telegram)

        rate_display = _format_rate(rate_value)
        health_display = _format_seconds(health_interval_value)
        deduplicate_default = parse_bool(
            self._store.get_setting("runtime.deduplicate_messages"), False
        )

        formatting_settings = {
            key.removeprefix("formatting."): value
            for key, value in self._store.iter_settings("formatting.")
        }
        disable_preview_default = (
            formatting_settings.get("disable_preview", "true").lower() != "false"
        )
        max_length_default = formatting_settings.get("max_length", "3500")
        attachments_default = formatting_settings.get("attachments_style", "summary")
        attachments_desc = (
            "краткий список" if attachments_default.lower() == "summary" else "список ссылок"
        )
        preview_desc = "без предпросмотра" if disable_preview_default else "с предпросмотром"
        show_link_default = (
            formatting_settings.get("show_discord_link", "false").lower() == "true"
        )
        link_desc = "включена" if show_link_default else "отключена"
        monitoring_mode_default = (
            self._store.get_setting("monitoring.mode") or "messages"
        ).strip().lower()
        monitoring_default_desc = (
            "закреплённые сообщения"
            if monitoring_mode_default == "pinned"
            else "новые сообщения"
        )

        default_filter_config = self._store.get_filter_config(0)
        manual_activity = self._store.load_manual_forward_activity()

        def _collect_filter_sets(filter_config: FilterConfig) -> dict[str, dict[str, str]]:
            collected: dict[str, dict[str, str]] = {name: {} for name in _FILTER_TYPES}
            for filter_type in _FILTER_TYPES:
                values = getattr(filter_config, filter_type)
                for value in values:
                    normalized = normalize_filter_value(filter_type, value)
                    if not normalized:
                        continue
                    _, key = normalized
                    collected[filter_type][key] = format_filter_value(filter_type, value)
            return collected

        def _describe_filters(
            filter_sets: dict[str, dict[str, str]], *, indent: str, empty_message: str
        ) -> list[str]:
            rows: list[str] = []
            for filter_type in _FILTER_TYPES:
                values = filter_sets.get(filter_type, {})
                if not values:
                    continue
                label = html.escape(_FILTER_LABELS.get(filter_type, filter_type))
                rows.append(f"{indent}• <b>{label}</b>")
                for display in sorted(values.values(), key=str.lower):
                    rows.append(
                        f"{indent}{_NBSP}{_NBSP}◦ {html.escape(display)}"
                    )
            if not rows and empty_message:
                rows.append(f"{indent}{empty_message}")
            return rows

        proxy_lines: list[str] = []
        proxy_lines.append(
            f"{_INDENT}• {_health_icon(proxy_health)} Статус: "
            + ("<b>включён</b>" if proxy_url else "<b>отключён</b>")
        )
        if proxy_message:
            proxy_lines.append(f"{_DOUBLE_INDENT}• {html.escape(proxy_message)}")
        if proxy_url:
            proxy_lines.append(f"{_DOUBLE_INDENT}• URL: <code>{html.escape(proxy_url)}</code>")
            if proxy_login:
                proxy_lines.append(
                    f"{_DOUBLE_INDENT}• Логин: <code>{html.escape(proxy_login)}</code>"
                )
            if proxy_password:
                proxy_lines.append(f"{_DOUBLE_INDENT}• Пароль: ••••••")
            if not proxy_login and not proxy_password:
                proxy_lines.append(f"{_DOUBLE_INDENT}• Без авторизации")
        else:
            proxy_lines.append(f"{_DOUBLE_INDENT}• Без прокси")

        channel_configs = self._store.load_channel_configurations()
        deduplicate_overrides = sum(
            1 for cfg in channel_configs if not cfg.deduplicate_inherited
        )
        default_filter_sets = _collect_filter_sets(default_filter_config)
        has_default_filters = any(default_filter_sets[name] for name in _FILTER_TYPES)

        lines: list[str] = [
            "<b>⚙️ Forward Monitor — статус</b>",
            "",
            "<b>🔌 Подключения</b>",
            f"• {_health_icon(token_health)} Discord токен: <b>{html.escape(token_status)}</b>",
            f"• User-Agent: <code>{html.escape(user_agent)}</code>",
        ]
        if token_message:
            lines.append(f"{_INDENT}• {html.escape(token_message)}")
        lines.extend(
            [
                "",
                "<b>🌐 Прокси</b>",
                *proxy_lines,
                "",
                "<b>⏱️ Режим работы</b>",
                f"• Опрос Discord: {html.escape(str(poll))} с",
                f"• Проверка состояния: {html.escape(health_display)} с",
                "• Пауза между сообщениями: "
                f"{html.escape(_format_seconds(delay_min_value))}–"
                f"{html.escape(_format_seconds(delay_max_value))} с",
                f"• Лимит запросов: {html.escape(str(rate_display))} в секунду",
                "• Фильтр дубликатов (по умолчанию): "
                f"{'<b>включён</b>' if deduplicate_default else 'выключен'}",
                *(
                    [
                        f"{_INDENT}• Индивидуальные настройки: {deduplicate_overrides}"
                    ]
                    if deduplicate_overrides
                    else []
                ),
                "",
                "<b>🎨 Оформление по умолчанию</b>",
                f"• Предпросмотр ссылок: {html.escape(preview_desc)}",
                f"• Максимальная длина: {html.escape(str(max_length_default))} символов",
                f"• Вложения: {html.escape(attachments_desc)}",
                f"• Ссылка на Discord: {html.escape(link_desc)}",
                f"• Режим мониторинга: {html.escape(monitoring_default_desc)}",
                "",
                "<b>🚦 Глобальные фильтры</b>",
            ]
        )

        if has_default_filters:
            lines.extend(
                _describe_filters(
                    default_filter_sets,
                    indent=_INDENT,
                    empty_message="• Нет активных фильтров",
                )
            )
        else:
            lines.append(f"{_INDENT}• Нет активных фильтров")

        if manual_activity:
            timestamp_display = as_moscow_time(manual_activity.timestamp)
            formatted_ts = timestamp_display.strftime("%Y-%m-%d %H:%M:%S %Z")
            lines.extend(
                [
                    "",
                    "<b>📨 Ручные пересылки</b>",
                    (
                        "• Последняя команда: "
                        f"{html.escape(formatted_ts)}"
                    ),
                    (
                        "• Запрошено: "
                        f"{manual_activity.requested} (лимит {manual_activity.limit}), "
                        f"переслано: {manual_activity.total_forwarded}"
                    ),
                ]
            )
            if manual_activity.entries:
                for entry in manual_activity.entries:
                    entry_label = entry.label or entry.discord_id
                    mode_desc = (
                        "закреплённые сообщения"
                        if entry.mode == "pinned"
                        else "обычные сообщения"
                    )
                    note_text = entry.note or "без изменений"
                    lines.append(
                        f"{_INDENT}• <b>{html.escape(entry_label)}</b> — {html.escape(note_text)}"
                    )
                    lines.append(
                        f"{_DOUBLE_INDENT}• Режим: {html.escape(mode_desc)}, "
                        f"переслано: {entry.forwarded}"
                    )
            else:
                lines.append(f"{_INDENT}• История недоступна")

        lines.append("")
        lines.append("<b>📡 Каналы</b>")
        if channel_configs:
            grouped = _group_channels_by_chat_and_thread(channel_configs)

            def _render_config(channel: "ChannelConfig") -> tuple[
                str, str, Sequence[tuple[int, str | None, str]]
            ]:
                health_status = channel.health_status
                health_message = channel.health_message
                if not channel.active:
                    health_status = "disabled"
                status_icon = _health_icon(health_status)
                label = html.escape(_normalize_label(channel.label, channel.discord_id))
                discord_display = html.escape(channel.discord_id)
                header = f"<b>{label}</b> · Discord <code>{discord_display}</code>"

                preview_label = (
                    "выключен"
                    if channel.formatting.disable_preview
                    else "включен"
                )
                link_channel_desc = (
                    "показывается"
                    if channel.formatting.show_discord_link
                    else "скрыта"
                )
                attachment_mode = (
                    "краткий список"
                    if channel.formatting.attachments_style.lower() == "summary"
                    else "список ссылок"
                )
                mode_label = (
                    "закреплённые сообщения"
                    if channel.pinned_only
                    else "новые сообщения"
                )
                deduplicate_label = "включён" if channel.deduplicate_messages else "выключен"
                if channel.deduplicate_inherited:
                    deduplicate_label += " (по умолчанию)"

                extra_rows: list[tuple[int, str | None, str]] = []
                if health_message:
                    extra_rows.append((3, None, html.escape(health_message)))
                if not channel.active:
                    extra_rows.append((3, None, html.escape("Канал отключён.")))

                details = [
                    ("Режим", mode_label),
                    ("Фильтр дубликатов", deduplicate_label),
                    ("Предпросмотр", preview_label),
                    (
                        "Максимальная длина",
                        f"{channel.formatting.max_length} символов",
                    ),
                    ("Ссылка на Discord", link_channel_desc),
                    ("Вложения", attachment_mode),
                ]

                for title, value in details:
                    extra_rows.append(
                        (3, None, f"{title}: {html.escape(str(value))}")
                    )

                channel_filter_sets = _collect_filter_sets(channel.filters)
                extra_filters = {
                    key: {
                        value_key: value
                        for value_key, value in channel_filter_sets.get(key, {}).items()
                        if value_key not in default_filter_sets.get(key, {})
                    }
                    for key in _FILTER_TYPES
                }
                if any(extra_filters[name] for name in _FILTER_TYPES):
                    extra_rows.append((3, None, "Дополнительные фильтры:"))
                    for filter_type in _FILTER_TYPES:
                        values = extra_filters.get(filter_type, {})
                        if not values:
                            continue
                        filter_label = html.escape(
                            _FILTER_LABELS.get(filter_type, filter_type)
                        )
                        extra_rows.append((4, None, f"{filter_label}:"))
                        for display in sorted(values.values(), key=str.lower):
                            extra_rows.append((5, None, html.escape(display)))
                else:
                    message = (
                        "Дополнительных фильтров нет, используются глобальные."
                        if has_default_filters
                        else "Дополнительные фильтры отсутствуют."
                    )
                    extra_rows.append((3, None, html.escape(message)))

                return status_icon, header, extra_rows

            lines.extend(
                [""]
                + _format_channel_groups(
                    grouped,
                    render_entry=_render_config,
                )
            )
        else:
            lines.append(_panel_bullet("Не настроены", icon="ℹ️"))

        while lines and lines[-1] == "":
            lines.pop()

        for chunk in _split_html_lines(lines):
            await self._api.send_message(
                ctx.chat_id,
                chunk,
                parse_mode="HTML",
            )

    async def cmd_claim(self, ctx: CommandContext) -> None:
        if self._store.has_admins():
            if not self._is_admin(ctx):
                await self._notify_access_denied(ctx)
                return
            self._store.add_admin(ctx.user_id, ctx.handle)
            self._on_change()
            await self._send_panel_message(
                ctx,
                title="Администрирование",
                icon="👑",
                rows=[
                    _panel_bullet(
                        "Ваши данные администратора обновлены.",
                        icon="✅",
                    )
                ],
            )
            return
        self._store.add_admin(ctx.user_id, ctx.handle)
        self._on_change()
        await self._send_panel_message(
            ctx,
            title="Администрирование",
            icon="👑",
            rows=[
                _panel_bullet(
                    "Вы назначены администратором.",
                    icon="🎉",
                )
            ],
        )

    async def cmd_admins(self, ctx: CommandContext) -> None:
        admins = self._store.list_admins()
        if admins:
            rows = [
                _panel_bullet(
                    self._format_admin(admin),
                    icon="🧑\u200d💼",
                )
                for admin in admins
            ]
            await self._send_panel_message(
                ctx,
                title="Администраторы",
                icon="👑",
                description="Список пользователей с правами управления.",
                rows=rows,
            )
        else:
            await self._send_panel_message(
                ctx,
                title="Администраторы",
                icon="👑",
                description="Список администраторов пуст.",
                rows=[
                    _panel_bullet(
                        "Используйте <code>/grant</code>, чтобы выдать права.",
                        icon="💡",
                    )
                ],
                description_escape=True,
            )

    async def cmd_grant(self, ctx: CommandContext) -> None:
        target = ctx.args.strip()
        if not target:
            await self._send_usage_error(ctx, "/grant <id|@user>")
            return
        user_id: int | None
        username: str | None
        if target.lstrip("-").isdigit():
            user_id = int(target)
            username = None
        else:
            normalized_username = normalize_username(target)
            if normalized_username is None:
                await self._send_status_notice(
                    ctx,
                    title="Администрирование",
                    icon="⚠️",
                    message="Неверное имя пользователя.",
                    message_icon="❗️",
                )
                return
            username = normalized_username
            user_id = self._store.resolve_user_id(username)
        self._store.add_admin(user_id, username)
        self._on_change()
        label = self._format_admin(AdminRecord(user_id=user_id, username=username))
        if user_id is None:
            await self._send_panel_message(
                ctx,
                title="Администрирование",
                icon="👑",
                rows=[
                    _panel_bullet(
                        (
                            f"Выдан доступ {label}. Активируется после первого обращения."
                        ),
                        icon="✅",
                    )
                ],
                description_escape=True,
            )
        else:
            await self._send_panel_message(
                ctx,
                title="Администрирование",
                icon="👑",
                rows=[
                    _panel_bullet(
                        f"Выдан доступ {label}",
                        icon="✅",
                    )
                ],
                description_escape=True,
            )

    async def cmd_revoke(self, ctx: CommandContext) -> None:
        target = ctx.args.strip()
        if not target:
            await self._send_usage_error(ctx, "/revoke <id|@user>")
            return
        label: str
        removed: bool
        if target.lstrip("-").isdigit():
            identifier = int(target)
            removed = self._store.remove_admin(identifier)
            label = self._format_admin(AdminRecord(user_id=identifier, username=None))
        else:
            normalized = normalize_username(target)
            if normalized is None:
                await self._send_status_notice(
                    ctx,
                    title="Администрирование",
                    icon="⚠️",
                    message="Неверное имя пользователя.",
                    message_icon="❗️",
                )
                return
            removed = self._store.remove_admin(normalized)
            label = self._format_admin(AdminRecord(user_id=None, username=normalized))
        if not removed:
            await self._send_status_notice(
                ctx,
                title="Администрирование",
                icon="⚠️",
                message="Администратор не найден.",
                message_icon="❗️",
            )
            return
        self._on_change()
        await self._send_panel_message(
            ctx,
            title="Администрирование",
            icon="👑",
            rows=[
                _panel_bullet(
                    f"Доступ отозван у {label}",
                    icon="✅",
                )
            ],
            description_escape=True,
        )

    # ------------------------------------------------------------------
    # Core configuration commands
    # ------------------------------------------------------------------
    async def cmd_set_discord_token(self, ctx: CommandContext) -> None:
        token = ctx.args.strip()
        if not token:
            await self._send_usage_error(ctx, "/set_discord_token <token>")
            return

        network = self._store.load_network_options()
        result = await self._discord.verify_token(token, network=network)
        if not result.ok:
            await self._send_status_notice(
                ctx,
                title="Discord",
                icon="⚠️",
                message=result.error or "Не удалось проверить токен Discord.",
                message_icon="❗️",
            )
            return

        stored_value = result.normalized_token or token
        self._store.set_setting("discord.token", stored_value)
        self._on_change()
        display = result.display_name or "пользователь"
        await self._send_panel_message(
            ctx,
            title="Discord",
            icon="✅",
            rows=[
                _panel_bullet(
                    f"Авторизация прошла успешно: {html.escape(display)}",
                    icon="🔐",
                )
            ],
        )

    async def cmd_set_proxy(self, ctx: CommandContext) -> None:
        parts = ctx.args.split()
        if not parts:
            await self._send_usage_error(
                ctx,
                "/set_proxy <url|clear> [логин] [пароль]",
            )
            return
        if parts[0].lower() == "clear":
            if len(parts) > 1:
                await self._send_status_notice(
                    ctx,
                    title="Прокси",
                    icon="⚠️",
                    message="Лишние параметры для отключения прокси.",
                    message_icon="❗️",
                )
                return
            self._store.delete_setting("proxy.discord.url")
            self._store.delete_setting("proxy.discord.login")
            self._store.delete_setting("proxy.discord.password")
            self._store.delete_setting("proxy.discord")
            self._on_change()
            await self._send_panel_message(
                ctx,
                title="Прокси",
                icon="🌐",
                rows=[
                    _panel_bullet("Прокси отключён.", icon="✅"),
                ],
            )
            return

        if len(parts) > 3:
            await self._send_usage_error(
                ctx,
                "/set_proxy <url> [логин] [пароль]",
                tip="Укажите не более трёх параметров.",
            )
            return

        proxy_url = parts[0]
        parsed = urlparse(proxy_url)
        if not parsed.scheme or not parsed.netloc:
            await self._send_status_notice(
                ctx,
                title="Прокси",
                icon="⚠️",
                message="Укажите корректный URL (например, http://host:port).",
                message_icon="❗️",
            )
            return

        allowed_schemes = {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}
        if parsed.scheme.lower() not in allowed_schemes:
            await self._send_status_notice(
                ctx,
                title="Прокси",
                icon="⚠️",
                message="Поддерживаются схемы http, https, socks4, socks5.",
                message_icon="❗️",
            )
            return

        proxy_login = parts[1] if len(parts) >= 2 else None
        proxy_password = parts[2] if len(parts) >= 3 else None
        if proxy_login and ":" in proxy_login:
            await self._send_status_notice(
                ctx,
                title="Прокси",
                icon="⚠️",
                message="Логин не должен содержать двоеточие.",
                message_icon="❗️",
            )
            return

        network = self._store.load_network_options()
        network.discord_proxy_url = proxy_url
        network.discord_proxy_login = proxy_login
        network.discord_proxy_password = proxy_password

        result = await self._discord.check_proxy(network)
        if not result.ok:
            await self._send_status_notice(
                ctx,
                title="Прокси",
                icon="⚠️",
                message=result.error or "Прокси не отвечает. Проверьте настройки.",
                message_icon="❗️",
            )
            return

        self._store.set_setting("proxy.discord.url", proxy_url)
        if proxy_login:
            self._store.set_setting("proxy.discord.login", proxy_login)
        else:
            self._store.delete_setting("proxy.discord.login")
        if proxy_password:
            self._store.set_setting("proxy.discord.password", proxy_password)
        else:
            self._store.delete_setting("proxy.discord.password")
        self._store.delete_setting("proxy.discord")
        self._on_change()
        rows = [
            _panel_bullet(
                f"URL: <code>{html.escape(proxy_url)}</code>",
                icon="🔗",
            ),
            _panel_bullet("Подключение проверено.", icon="✅"),
        ]
        if proxy_login:
            rows.append(
                _panel_bullet(
                    f"Логин: <code>{html.escape(proxy_login)}</code>",
                    icon="👤",
                )
            )
        await self._send_panel_message(
            ctx,
            title="Прокси",
            icon="🌐",
            rows=rows,
        )

    async def cmd_set_user_agent(self, ctx: CommandContext) -> None:
        value = ctx.args.strip()
        if not value:
            await self._send_usage_error(ctx, "/set_user_agent <значение>")
            return
        self._store.set_setting("ua.discord", value)
        self._store.delete_setting("ua.discord.desktop")
        self._store.delete_setting("ua.discord.mobile")
        self._store.delete_setting("ua.discord.mobile_ratio")
        self._on_change()
        await self._send_panel_message(
            ctx,
            title="Discord",
            icon="🧾",
            rows=[
                _panel_bullet("User-Agent сохранён.", icon="✅"),
            ],
        )

    async def cmd_set_poll(self, ctx: CommandContext) -> None:
        try:
            value = float(ctx.args)
        except ValueError:
            await self._send_status_notice(
                ctx,
                title="Параметры",
                icon="⚠️",
                message="Укажите число секунд.",
                message_icon="❗️",
            )
            return
        self._store.set_setting("runtime.poll", f"{max(0.5, value):.2f}")
        self._on_change()
        await self._send_panel_message(
            ctx,
            title="Параметры",
            icon="⏱️",
            rows=[
                _panel_bullet("Интервал опроса обновлён.", icon="✅"),
            ],
        )

    async def cmd_set_healthcheck(self, ctx: CommandContext) -> None:
        value_str = ctx.args.strip()
        if not value_str:
            await self._send_usage_error(ctx, "/set_healthcheck <секунды>")
            return
        try:
            value = float(value_str)
        except ValueError:
            await self._send_status_notice(
                ctx,
                title="Параметры",
                icon="⚠️",
                message="Укажите число секунд.",
                message_icon="❗️",
            )
            return
        if value < 10.0:
            await self._send_status_notice(
                ctx,
                title="Параметры",
                icon="⚠️",
                message="Минимальный интервал — 10 секунд.",
                message_icon="❗️",
            )
            return
        self._store.set_setting("runtime.health_interval", f"{value:.2f}")
        self._on_change()
        await self._send_panel_message(
            ctx,
            title="Параметры",
            icon="⏱️",
            rows=[
                _panel_bullet("Интервал health-check обновлён.", icon="✅"),
            ],
        )

    async def cmd_set_delay(self, ctx: CommandContext) -> None:
        parts = ctx.args.split()
        if len(parts) != 2:
            await self._send_usage_error(ctx, "/set_delay <min_s> <max_s>")
            return
        try:
            min_seconds = float(parts[0])
            max_seconds = float(parts[1])
        except ValueError:
            await self._send_status_notice(
                ctx,
                title="Параметры",
                icon="⚠️",
                message="Укажите числа в секундах.",
                message_icon="❗️",
            )
            return
        if min_seconds < 0 or max_seconds < min_seconds:
            await self._send_status_notice(
                ctx,
                title="Параметры",
                icon="⚠️",
                message="Неверный диапазон значений.",
                message_icon="❗️",
            )
            return
        self._store.set_setting("runtime.delay_min", f"{min_seconds:.2f}")
        self._store.set_setting("runtime.delay_max", f"{max_seconds:.2f}")
        self._on_change()
        await self._send_panel_message(
            ctx,
            title="Параметры",
            icon="⏱️",
            rows=[
                _panel_bullet("Диапазон задержек сохранён.", icon="✅"),
            ],
        )

    async def cmd_set_rate(self, ctx: CommandContext) -> None:
        value_str = ctx.args.strip()
        if not value_str:
            await self._send_usage_error(ctx, "/set_rate <в_секунду>")
            return
        try:
            value = float(value_str)
        except ValueError:
            await self._send_status_notice(
                ctx,
                title="Параметры",
                icon="⚠️",
                message="Неверное число.",
                message_icon="❗️",
            )
            return
        self._store.set_setting("runtime.rate", f"{max(0.1, value):.2f}")
        self._store.delete_setting("runtime.discord_rate")
        self._store.delete_setting("runtime.telegram_rate")
        self._on_change()
        await self._send_panel_message(
            ctx,
            title="Параметры",
            icon="⏱️",
            rows=[
                _panel_bullet("Единый лимит обновлён.", icon="✅"),
            ],
        )

    # ------------------------------------------------------------------
    # Channel management
    # ------------------------------------------------------------------
    async def cmd_add_channel(self, ctx: CommandContext) -> None:
        parts = ctx.args.split()
        usage = (
            "/add_channel <discord_id> <telegram_chat[:thread]> <название> [messages|pinned]"
        )
        if len(parts) < 3:
            await self._send_usage_error(ctx, usage)
            return

        mode_override: str | None = None
        mode_aliases = {
            "messages": "messages",
            "message": "messages",
            "default": "messages",
            "pinned": "pinned",
            "pin": "pinned",
            "pins": "pinned",
        }
        tail = parts[-1].lower()
        if tail.startswith("mode="):
            candidate = tail.split("=", 1)[1]
            mode_override = mode_aliases.get(candidate)
            if mode_override is None:
                await self._send_status_notice(
                    ctx,
                    title="Каналы",
                    icon="⚠️",
                    message="Допустимые режимы: messages, pinned.",
                    message_icon="❗️",
                )
                return
            parts = parts[:-1]
        elif tail in mode_aliases:
            mode_override = mode_aliases[tail]
            parts = parts[:-1]

        if len(parts) < 3:
            await self._send_usage_error(ctx, usage)
            return

        discord_id, telegram_chat_raw, *label_parts = parts
        # Try to parse Discord URL if input looks like a URL
        parsed_id = _parse_discord_url(discord_id)
        if parsed_id:
            discord_id = parsed_id
        label = " ".join(label_parts).strip()

        if not label:
            await self._send_status_notice(
                ctx,
                title="Каналы",
                icon="⚠️",
                message="Укажите название канала.",
                message_icon="❗️",
            )
            return
        thread_id: int | None = None
        telegram_chat = telegram_chat_raw
        if ":" in telegram_chat_raw:
            chat_part, thread_part = telegram_chat_raw.split(":", 1)
            if not chat_part or not thread_part:
                await self._send_status_notice(
                    ctx,
                    title="Каналы",
                    icon="⚠️",
                    message="Неверный формат chat:thread.",
                    message_icon="❗️",
                )
                return
            telegram_chat = chat_part
            try:
                thread_id = int(thread_part)
            except ValueError:
                await self._send_status_notice(
                    ctx,
                    title="Каналы",
                    icon="⚠️",
                    message="Thread ID должен быть числом.",
                    message_icon="❗️",
                )
                return
            if thread_id <= 0:
                await self._send_status_notice(
                    ctx,
                    title="Каналы",
                    icon="⚠️",
                    message="Thread ID должен быть положительным.",
                    message_icon="❗️",
                )
                return
        if self._store.get_channel(discord_id):
            await self._send_status_notice(
                ctx,
                title="Каналы",
                icon="⚠️",
                message="Связка с таким Discord каналом уже существует.",
                message_icon="❗️",
            )
            return
        token = self._store.get_setting("discord.token")
        if not token:
            await self._send_status_notice(
                ctx,
                title="Каналы",
                icon="⚠️",
                message=(
                    "Сначала задайте токен командой <code>/set_discord_token</code>."
                ),
                message_icon="❗️",
                escape=False,
            )
            return

        network = self._store.load_network_options()
        self._discord.set_token(token)
        self._discord.set_network_options(network)

        try:
            exists = await self._discord.check_channel_exists(discord_id)
        except Exception:
            logger.exception(
                "Не удалось проверить наличие Discord канала %s при создании связки",
                discord_id,
            )
            await self._send_status_notice(
                ctx,
                title="Каналы",
                icon="⚠️",
                message="Не удалось проверить канал. Проверьте идентификатор и доступ.",
                message_icon="❗️",
            )
            return

        if not exists:
            await self._send_status_notice(
                ctx,
                title="Каналы",
                icon="⚠️",
                message="Канал не найден или нет доступа. Проверьте идентификатор и права.",
                message_icon="❗️",
            )
            return

        last_message_id: str | None = None
        try:
            messages = await self._discord.fetch_messages(discord_id, limit=1)
        except Exception:
            logger.exception(
                "Не удалось получить последнее сообщение канала %s при создании связки",
                discord_id,
            )
        else:
            if messages:
                latest = max(
                    messages,
                    key=lambda msg: (
                        (int(msg.id), msg.id) if msg.id.isdigit() else (0, msg.id)
                    ),
                )
                last_message_id = latest.id
        record = self._store.add_channel(
            discord_id,
            telegram_chat,
            label,
            telegram_thread_id=thread_id,
            last_message_id=last_message_id,
        )

        default_mode = (
            self._store.get_setting("monitoring.mode") or "messages"
        ).strip().lower()
        mode_to_apply = mode_override or default_mode
        explicit_override = mode_override is not None

        if explicit_override:
            self._store.set_channel_option(
                record.id, "monitoring.mode", mode_to_apply
            )
        else:
            if mode_to_apply != default_mode:
                self._store.set_channel_option(
                    record.id, "monitoring.mode", mode_to_apply
                )
            else:
                self._store.delete_channel_option(record.id, "monitoring.mode")

        mode_label = "новые сообщения"
        if mode_to_apply == "messages":
            self._store.clear_known_pinned_messages(record.id)
            self._store.set_pinned_synced(record.id, synced=False)
        else:
            mode_label = "закреплённые сообщения"
            pinned_messages = None
            if token:
                try:
                    pinned_messages = list(
                        await self._discord.fetch_pinned_messages(discord_id)
                    )
                except Exception:  # pragma: no cover - network failure logged
                    logger.exception(
                        "Не удалось получить закреплённые сообщения канала %s при создании связки",
                        discord_id,
                    )
                    pinned_messages = None
            if pinned_messages is not None:
                self._store.set_known_pinned_messages(
                    record.id, (msg.id for msg in pinned_messages)
                )
                self._store.set_pinned_synced(record.id, synced=True)
            else:
                self._store.set_known_pinned_messages(record.id, [])
                self._store.set_pinned_synced(record.id, synced=False)

        self._on_change()
        label_display = html.escape(label)
        discord_display = html.escape(discord_id)
        telegram_display = html.escape(telegram_chat)
        mode_display = html.escape(mode_label)
        rows = [
            _panel_bullet(f"Название: <b>{label_display}</b>", icon="🏷️"),
            _panel_bullet(f"Discord: <code>{discord_display}</code>", icon="🛰️"),
            _panel_bullet(f"Telegram: <code>{telegram_display}</code>", icon="💬"),
            _panel_bullet(f"Режим: {mode_display}", icon="🎯"),
        ]
        if thread_id is not None:
            rows.append(
                _panel_bullet(
                    f"Тема: <code>{thread_id}</code>",
                    icon="🧵",
                )
            )
        await self._send_panel_message(
            ctx,
            title="Связка создана",
            icon="📡",
            rows=rows,
        )

    async def cmd_set_thread(self, ctx: CommandContext) -> None:
        parts = ctx.args.split()
        if len(parts) < 2:
            await self._send_usage_error(ctx, "/set_thread <discord_id> <thread_id|clear>")
            return
        discord_id, value = parts[:2]
        record = self._store.get_channel(discord_id)
        if not record:
            await self._send_status_notice(
                ctx,
                title="Каналы",
                icon="⚠️",
                message="Канал не найден.",
                message_icon="❗️",
            )
            return
        thread_id: int | None
        value_lower = value.lower()
        if value_lower in {"clear", "none", "off", "0"}:
            thread_id = None
        else:
            try:
                thread_id = int(value)
            except ValueError:
                await self._send_status_notice(
                    ctx,
                    title="Каналы",
                    icon="⚠️",
                    message="Thread ID должен быть числом.",
                    message_icon="❗️",
                )
                return
            if thread_id <= 0:
                await self._send_status_notice(
                    ctx,
                    title="Каналы",
                    icon="⚠️",
                    message="Thread ID должен быть положительным.",
                    message_icon="❗️",
                )
                return
        self._store.set_channel_thread(record.id, thread_id)
        self._on_change()
        if thread_id is None:
            await self._send_panel_message(
                ctx,
                title="Каналы",
                icon="🧵",
                rows=[_panel_bullet("Тема очищена.", icon="✅")],
            )
        else:
            await self._send_panel_message(
                ctx,
                title="Каналы",
                icon="🧵",
                rows=[
                    _panel_bullet(
                        f"Установлена тема <code>{thread_id}</code>",
                        icon="✅",
                    )
                ],
                description_escape=True,
            )

    async def cmd_remove_channel(self, ctx: CommandContext) -> None:
        if not ctx.args:
            await self._send_usage_error(ctx, "/remove_channel <discord_id>")
            return
        removed = self._store.remove_channel(ctx.args)
        self._on_change()
        if removed:
            await self._send_panel_message(
                ctx,
                title="Каналы",
                icon="🗑️",
                rows=[_panel_bullet("Связка удалена.", icon="✅")],
            )
        else:
            await self._send_status_notice(
                ctx,
                title="Каналы",
                icon="⚠️",
                message="Канал не найден.",
                message_icon="❗️",
            )

    async def cmd_list_channels(self, ctx: CommandContext) -> None:
        channels = self._store.list_channels()
        if not channels:
            await self._send_panel_message(
                ctx,
                title="Каналы не настроены",
                icon="ℹ️",
                description="Подключения отсутствуют.",
                rows=[
                    _panel_bullet(
                        "Добавьте связку командой <code>/add_channel</code>.",
                        icon="💡",
                    )
                ],
                description_escape=True,
            )
            return
        grouped = _group_channels_by_chat_and_thread(channels)

        lines = [
            "<b>📡 Настроенные каналы</b>",
            "<i>Сгруппированы по чатам Telegram и темам.</i>",
            "",
        ]

        def _render_record(record: Any) -> tuple[
            str, str, Sequence[tuple[int, str | None, str]]
        ]:
            health_status, health_message = self._store.get_health_status(
                f"channel.{record.discord_id}"
            )
            if not record.active:
                health_status = "disabled"
            status_icon = _health_icon(health_status)
            label = html.escape(_normalize_label(record.label, record.discord_id))
            discord_id = html.escape(record.discord_id)
            header = f"<b>{label}</b> · Discord <code>{discord_id}</code>"
            extra: list[tuple[int, str | None, str]] = []
            if health_message:
                extra.append((3, None, html.escape(health_message)))
            if not record.active:
                extra.append((3, None, html.escape("Связка отключена.")))
            return status_icon, header, extra

        lines.extend(
            _format_channel_groups(
                grouped,
                render_entry=_render_record,
            )
        )

        for chunk in _split_html_lines(lines):
            await self._api.send_message(
                ctx.chat_id,
                chunk,
                parse_mode="HTML",
            )

    async def cmd_send_recent(self, ctx: CommandContext) -> None:
        parts = ctx.args.split()
        if not parts:
            await self._send_usage_error(
                ctx,
                "/send_recent <кол-во> [discord_id|all]",
            )
            return

        try:
            requested = int(parts[0])
        except ValueError:
            await self._send_status_notice(
                ctx,
                title="Ручная пересылка",
                icon="⚠️",
                message="Количество должно быть числом.",
                message_icon="❗️",
            )
            return

        if requested <= 0:
            await self._send_status_notice(
                ctx,
                title="Ручная пересылка",
                icon="⚠️",
                message="Количество должно быть положительным.",
                message_icon="❗️",
            )
            return

        target = parts[1] if len(parts) > 1 else "all"
        limit = min(requested, 100)
        token = self._store.get_setting("discord.token")
        if not token:
            await self._send_status_notice(
                ctx,
                title="Ручная пересылка",
                icon="⚠️",
                message=(
                    "Сначала задайте токен через <code>/set_discord_token</code>."
                ),
                message_icon="❗️",
                escape=False,
            )
            return

        network = self._store.load_network_options()
        self._discord.set_token(token)
        self._discord.set_network_options(network)

        configs = self._store.load_channel_configurations()
        channels_by_id: dict[str, ChannelConfig] = {
            config.discord_id: config for config in configs
        }

        if target.lower() in {"all", "*"}:
            selected = list(configs)
        else:
            channel = channels_by_id.get(target)
            if not channel:
                await self._send_status_notice(
                    ctx,
                    title="Ручная пересылка",
                    icon="⚠️",
                    message="Канал не найден.",
                    message_icon="❗️",
                )
                return
            selected = [channel]

        if not selected:
            await self._send_status_notice(
                ctx,
                title="Ручная пересылка",
                icon="⚠️",
                message="Каналы не настроены.",
                message_icon="❗️",
            )
            return

        await self._send_panel_message(
            ctx,
            title="Ручная пересылка",
            icon="📨",
            rows=[
                _panel_bullet(
                    f"Каналов: {len(selected)}", icon="📡"
                ),
                _panel_bullet(f"Лимит: {limit}", icon="🎯"),
                _panel_bullet(
                    "Пересылка запущена, это может занять несколько минут.",
                    icon="⏳",
                ),
            ],
        )

        rate_setting = self._store.get_setting("runtime.rate")
        try:
            rate_value = float(rate_setting) if rate_setting is not None else 8.0
        except ValueError:
            rate_value = 8.0
        rate_value = max(rate_value, 0.1)
        limiter = RateLimiter(rate_value)

        summary_lines: list[str] = ["<b>📨 Ручная пересылка</b>", ""]
        total_forwarded = 0
        state_changed = False
        activity_entries: list[ManualForwardEntry] = []

        if requested > 100:
            summary_lines.append(
                "Запрошено больше 100 сообщений, будет переслано не более 100 из каждого канала."
            )

        def is_newer(message_id: str, marker: str | None) -> bool:
            if marker is None:
                return True
            return _message_id_sort_key(message_id) > _message_id_sort_key(marker)

        invocation_time = _utcnow()
        guard = self._channel_guard

        async def process_single_channel(channel_cfg: ChannelConfig) -> None:
            nonlocal total_forwarded, state_changed

            raw_label = channel_cfg.label or channel_cfg.discord_id
            label = html.escape(raw_label)
            mode = "pinned" if channel_cfg.pinned_only else "messages"
            deduplicate_enabled = channel_cfg.deduplicate_messages
            forwarded = 0

            def _record(note_text: str, forwarded_count: int = 0) -> None:
                summary_lines.append(f"{label}: {note_text}")
                activity_entries.append(
                    ManualForwardEntry(
                        discord_id=channel_cfg.discord_id,
                        label=raw_label,
                        forwarded=forwarded_count,
                        mode=mode,
                        note=note_text,
                    )
                )

            if not channel_cfg.active:
                _record("канал отключён, пропущено")
                return
            if channel_cfg.blocked_by_health:
                _record("канал недоступен по результатам health-check, пропущено")
                return

            if channel_cfg.pinned_only:
                try:
                    messages = await self._discord.fetch_pinned_messages(
                        channel_cfg.discord_id
                    )
                except Exception:
                    logger.exception(
                        "Не удалось получить закреплённые сообщения канала %s при ручной пересылке",
                        channel_cfg.discord_id,
                    )
                    _record("ошибка при запросе закреплённых сообщений")
                    return

                current_ids = {msg.id for msg in messages}
                previous_known = set(channel_cfg.known_pinned_ids)

                if not messages:
                    if (
                        channel_cfg.storage_id is not None
                        and channel_cfg.known_pinned_ids
                    ):
                        self._store.set_known_pinned_messages(channel_cfg.storage_id, [])
                        channel_cfg.known_pinned_ids = set()
                        state_changed = True
                    _record("закреплённых сообщений нет")
                    return

                if not channel_cfg.pinned_synced:
                    if channel_cfg.storage_id is not None:
                        self._store.set_known_pinned_messages(
                            channel_cfg.storage_id, current_ids
                        )
                        self._store.set_pinned_synced(
                            channel_cfg.storage_id, synced=True
                        )
                        channel_cfg.known_pinned_ids = set(current_ids)
                        channel_cfg.pinned_synced = True
                        state_changed = True
                    else:
                        channel_cfg.pinned_synced = True
                    _record("закреплённые синхронизированы, новых нет")
                    return

                ordered = sorted(messages, key=_message_order_key)
                subset = ordered[-limit:]
                if not subset:
                    _record("закреплённых сообщений нет")
                    return

                engine = FilterEngine(channel_cfg.filters)
                processed_new_ids: set[str] = set()

                for msg in subset:
                    candidate_id = msg.id
                    if candidate_id not in previous_known:
                        decision = engine.evaluate(msg)
                        if decision.allowed:
                            if deduplicate_enabled and self._deduplicator.is_duplicate(
                                build_message_signature(msg)
                            ):
                                processed_new_ids.add(candidate_id)
                                continue
                            formatted = format_discord_message(
                                msg, channel_cfg, message_kind="pinned"
                            )
                            try:
                                await limiter.wait()
                                await send_formatted(
                                    self._api,
                                    channel_cfg.telegram_chat_id,
                                    formatted,
                                    thread_id=channel_cfg.telegram_thread_id,
                                )
                            except Exception:
                                logger.exception(
                                    "Не удалось отправить закреплённое сообщение %s "
                                    "в Telegram чат %s",
                                    msg.id,
                                    channel_cfg.telegram_chat_id,
                                )
                                continue
                            forwarded += 1
                            total_forwarded += 1
                            processed_new_ids.add(candidate_id)
                    else:
                        processed_new_ids.add(candidate_id)

                if (
                    channel_cfg.storage_id is not None
                    and processed_new_ids
                    and processed_new_ids != previous_known
                ):
                    self._store.set_known_pinned_messages(
                        channel_cfg.storage_id, processed_new_ids
                    )
                    channel_cfg.known_pinned_ids = set(processed_new_ids)
                    state_changed = True

                if forwarded:
                    _record(
                        f"переслано {forwarded} закреплённых из {len(subset)} сообщений",
                        forwarded,
                    )
                else:
                    _record("подходящих закреплённых сообщений не найдено")
                return

            marker = channel_cfg.last_message_id
            fetch_limit = min(100, max(limit + 5, 2 * limit))
            try:
                messages = await self._discord.fetch_messages(
                    channel_cfg.discord_id,
                    limit=fetch_limit,
                )
            except Exception:
                logger.exception(
                    "Не удалось получить сообщения канала %s при ручной пересылке",
                    channel_cfg.discord_id,
                )
                _record("ошибка при запросе сообщений")
                return

            if not messages:
                _record("сообщения не найдены")
                return

            eligible = _prepare_recent_messages(
                messages, invocation_time=invocation_time
            )

            if not eligible:
                _record("подходящих сообщений не найдено")
                return

            total_candidates = len(eligible)
            subset = eligible[-limit:]
            processed_candidates = len(subset)

            engine = FilterEngine(channel_cfg.filters)
            last_seen = marker
            for msg in subset:
                candidate_id = msg.id
                if (
                    msg.message_type not in _FORWARDABLE_MESSAGE_TYPES
                    and not (msg.attachments or msg.embeds)
                ):
                    if is_newer(candidate_id, last_seen):
                        last_seen = candidate_id
                    continue
                decision = engine.evaluate(msg)
                if not decision.allowed:
                    if is_newer(candidate_id, last_seen):
                        last_seen = candidate_id
                    continue
                if deduplicate_enabled and self._deduplicator.is_duplicate(
                    build_message_signature(msg)
                ):
                    if is_newer(candidate_id, last_seen):
                        last_seen = candidate_id
                    continue
                formatted = format_discord_message(
                    msg, channel_cfg, message_kind="message"
                )
                try:
                    await limiter.wait()
                    await send_formatted(
                        self._api,
                        channel_cfg.telegram_chat_id,
                        formatted,
                        thread_id=channel_cfg.telegram_thread_id,
                    )
                except Exception:
                    logger.exception(
                        "Не удалось отправить сообщение %s в Telegram чат %s при ручной пересылке",
                        msg.id,
                        channel_cfg.telegram_chat_id,
                    )
                    if is_newer(candidate_id, last_seen):
                        last_seen = candidate_id
                    continue
                if is_newer(candidate_id, last_seen):
                    last_seen = candidate_id
                forwarded += 1
                total_forwarded += 1

            if (
                last_seen
                and channel_cfg.storage_id is not None
                and last_seen != channel_cfg.last_message_id
            ):
                self._store.set_last_message(channel_cfg.storage_id, last_seen)
                state_changed = True

            if forwarded:
                note_parts = [
                    f"переслано {forwarded} из {processed_candidates} сообщений"
                ]
            else:
                note_parts = ["подходящих сообщений не найдено"]
            remaining = total_candidates - processed_candidates
            if remaining > 0:
                note_parts.append(f"осталось ещё {remaining} сообщений")
            note = ", ".join(note_parts)
            _record(note, forwarded)

        for channel in selected:
            if guard is None:
                await process_single_channel(channel)
            else:
                async with guard.lock(channel.discord_id):
                    await process_single_channel(channel)

        if activity_entries:
            self._store.record_manual_forward_activity(
                requested=requested,
                limit=limit,
                total_forwarded=total_forwarded,
                entries=activity_entries,
            )

        if state_changed:
            self._on_change()

        summary_lines.append("")
        summary_lines.append(f"Всего переслано: {total_forwarded}")

        for chunk in _split_html_lines(summary_lines):
            await self._api.send_message(
                ctx.chat_id,
                chunk,
                parse_mode="HTML",
            )

    async def cmd_set_disable_preview(self, ctx: CommandContext) -> None:
        await self._set_format_option(
            ctx,
            "disable_preview",
            allowed={"on", "off"},
        )

    async def cmd_set_max_length(self, ctx: CommandContext) -> None:
        await self._set_format_option(ctx, "max_length")

    async def cmd_set_attachments(self, ctx: CommandContext) -> None:
        await self._set_format_option(ctx, "attachments_style", allowed={"summary", "links"})

    async def cmd_set_discord_link(self, ctx: CommandContext) -> None:
        await self._set_format_option(ctx, "show_discord_link", allowed={"on", "off"})

    async def cmd_set_monitoring(self, ctx: CommandContext) -> None:
        parts = ctx.args.split(maxsplit=1)
        if len(parts) < 2:
            await self._send_usage_error(
                ctx,
                "/set_monitoring <discord_id|all> <messages|pinned>",
            )
            return
        target_key, mode_raw = parts
        mode_key = mode_raw.strip().lower()
        mode_map = {
            "messages": "messages",
            "message": "messages",
            "default": "messages",
            "pinned": "pinned",
            "pins": "pinned",
            "pin": "pinned",
        }
        normalized_mode = mode_map.get(mode_key)
        if normalized_mode is None:
            await self._send_status_notice(
                ctx,
                title="Режим мониторинга",
                icon="⚠️",
                message="Допустимые режимы: messages, pinned.",
                message_icon="❗️",
            )
            return

        if target_key.lower() in {"all", "*"}:
            self._store.set_setting("monitoring.mode", normalized_mode)
            self._on_change()
            description = (
                "По умолчанию отслеживаются закреплённые сообщения"
                if normalized_mode == "pinned"
                else "По умолчанию отслеживаются новые сообщения"
            )
            await self._send_panel_message(
                ctx,
                title="Режим мониторинга",
                icon="🎯",
                rows=[_panel_bullet(description, icon="✅")],
            )
            return

        record = self._store.get_channel(target_key)
        if not record:
            await self._send_status_notice(
                ctx,
                title="Режим мониторинга",
                icon="⚠️",
                message="Канал не найден.",
                message_icon="❗️",
            )
            return

        if normalized_mode == "messages":
            self._store.delete_channel_option(record.id, "monitoring.mode")
            self._store.clear_known_pinned_messages(record.id)
            self._store.set_pinned_synced(record.id, synced=False)
            self._on_change()
            await self._send_panel_message(
                ctx,
                title="Режим мониторинга",
                icon="🎯",
                rows=[
                    _panel_bullet(
                        "Канал переключён на обычные сообщения.",
                        icon="✅",
                    )
                ],
            )
            return

        self._store.set_channel_option(record.id, "monitoring.mode", normalized_mode)
        try:
            pinned_messages = list(
                await self._discord.fetch_pinned_messages(record.discord_id)
            )
        except Exception:  # pragma: no cover - network failure is logged but ignored
            logger.exception(
                "Не удалось получить список закреплённых сообщений для канала %s",
                record.discord_id,
            )
            pinned_messages = None
        if pinned_messages is not None:
            self._store.set_known_pinned_messages(
                record.id,
                (msg.id for msg in pinned_messages),
            )
            self._store.set_pinned_synced(record.id, synced=True)
        else:
            self._store.set_pinned_synced(record.id, synced=False)
        self._on_change()
        await self._send_panel_message(
            ctx,
            title="Режим мониторинга",
            icon="🎯",
            rows=[
                _panel_bullet(
                    "Канал переключён на закреплённые сообщения.",
                    icon="✅",
                )
            ],
        )

    async def cmd_set_duplicate_filter(self, ctx: CommandContext) -> None:
        parts = ctx.args.split(maxsplit=1)
        if not parts:
            await self._send_usage_error(
                ctx, "/set_duplicate_filter <discord_id|all> <on|off>"
            )
            return

        if len(parts) == 1:
            target_key, value_raw = "all", parts[0]
        else:
            target_key, value_raw = parts

        value = value_raw.strip().lower()
        if value not in {"on", "off"}:
            await self._send_usage_error(
                ctx, "/set_duplicate_filter <discord_id|all> <on|off>"
            )
            return

        enabled = value == "on"
        setting_value = "true" if enabled else "false"
        target_label: str

        if target_key.lower() in {"all", "*"}:
            self._store.set_setting("runtime.deduplicate_messages", setting_value)
            target_label = "По умолчанию для всех каналов"
        else:
            record = self._store.get_channel(target_key)
            if not record:
                await self._send_status_notice(
                    ctx,
                    title="Фильтр дубликатов",
                    icon="⚠️",
                    message="Канал не найден.",
                    message_icon="❗️",
                )
                return
            self._store.set_channel_option(
                record.id, "runtime.deduplicate_messages", setting_value
            )
            target_label = f"Канал: <b>{html.escape(record.label or record.discord_id)}</b>"

        self._on_change()

        message_text = (
            "Фильтр дублирующихся сообщений включён."
            if enabled
            else "Фильтр дублирующихся сообщений отключён."
        )

        await self._send_panel_message(
            ctx,
            title="Фильтр дубликатов",
            icon="🧹",
            rows=[
                _panel_bullet(message_text, icon="✅"),
                _panel_bullet(target_label, icon="🛰️"),
            ],
            description_escape=True,
        )

    async def cmd_add_filter(self, ctx: CommandContext) -> None:
        parts = ctx.args.split(maxsplit=2)
        if len(parts) < 3:
            await self._send_usage_error(
                ctx,
                "/add_filter <discord_id|all> <тип> <значение>",
            )
            return
        target_key, filter_type_raw, value = parts
        filter_type = filter_type_raw.strip().lower()
        if filter_type not in _FILTER_TYPES:
            await self._send_status_notice(
                ctx,
                title="Фильтры",
                icon="⚠️",
                message="Неизвестный тип фильтра. Допустимо: "
                + ", ".join(_FILTER_TYPES),
                message_icon="❗️",
            )
            return
        channel_ids = self._resolve_channel_ids(target_key)
        if not channel_ids:
            await self._send_status_notice(
                ctx,
                title="Фильтры",
                icon="⚠️",
                message="Канал не найден.",
                message_icon="❗️",
            )
            return
        added = False
        for channel_id in channel_ids:
            try:
                changed = self._store.add_filter(channel_id, filter_type, value)
            except ValueError:
                await self._send_status_notice(
                    ctx,
                    title="Фильтры",
                    icon="⚠️",
                    message="Неверное значение фильтра.",
                    message_icon="❗️",
                )
                return
            added = added or changed
        if added:
            self._on_change()
            await self._send_panel_message(
                ctx,
                title="Фильтры",
                icon="🛡️",
                rows=[_panel_bullet("Фильтр добавлен.", icon="✅")],
            )
        else:
            await self._send_status_notice(
                ctx,
                title="Фильтры",
                icon="⚠️",
                message="Такой фильтр уже существует.",
                message_icon="❗️",
            )

    async def cmd_clear_filter(self, ctx: CommandContext) -> None:
        parts = ctx.args.split(maxsplit=2)
        if len(parts) < 2:
            await self._send_usage_error(
                ctx,
                "/clear_filter <discord_id|all> <тип> [значение]",
            )
            return
        target_key, filter_type_raw = parts[0], parts[1]
        filter_type = filter_type_raw.strip().lower()
        value = parts[2] if len(parts) == 3 else None
        channel_ids = self._resolve_channel_ids(target_key)
        if not channel_ids:
            await self._send_status_notice(
                ctx,
                title="Фильтры",
                icon="⚠️",
                message="Канал не найден.",
                message_icon="❗️",
            )
            return
        if filter_type in {"all", "*"}:
            removed = sum(self._store.clear_filters(channel_id) for channel_id in channel_ids)
            if removed:
                self._on_change()
                await self._send_panel_message(
                    ctx,
                    title="Фильтры",
                    icon="🛡️",
                    rows=[_panel_bullet("Все фильтры очищены.", icon="✅")],
                )
            else:
                await self._send_status_notice(
                    ctx,
                    title="Фильтры",
                    icon="⚠️",
                    message="Фильтры уже очищены.",
                    message_icon="❗️",
                )
            return

        if filter_type not in _FILTER_TYPES:
            await self._send_status_notice(
                ctx,
                title="Фильтры",
                icon="⚠️",
                message="Неизвестный тип фильтра. Допустимо: "
                + ", ".join(_FILTER_TYPES),
                message_icon="❗️",
            )
            return

        removed = 0
        if value is None:
            for channel_id in channel_ids:
                removed += self._store.remove_filter(channel_id, filter_type, None)
            if removed:
                self._on_change()
                await self._send_panel_message(
                    ctx,
                    title="Фильтры",
                    icon="🛡️",
                    rows=[_panel_bullet("Фильтры удалены.", icon="✅")],
                )
            else:
                await self._send_status_notice(
                    ctx,
                    title="Фильтры",
                    icon="⚠️",
                    message="Фильтров этого типа не найдено.",
                    message_icon="❗️",
                )
            return

        for channel_id in channel_ids:
            removed += self._store.remove_filter(channel_id, filter_type, value)
        if removed:
            self._on_change()
            await self._send_panel_message(
                ctx,
                title="Фильтры",
                icon="🛡️",
                rows=[_panel_bullet("Фильтр удалён.", icon="✅")],
            )
        else:
            await self._send_status_notice(
                ctx,
                title="Фильтры",
                icon="⚠️",
                message="Такого фильтра не существует.",
                message_icon="❗️",
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _set_format_option(
        self,
        ctx: CommandContext,
        option: str,
        *,
        allowed: Iterable[str] | None = None,
    ) -> None:
        parts = ctx.args.split(maxsplit=1)
        if len(parts) < 2:
            await self._send_status_notice(
                ctx,
                title="Настройки оформления",
                icon="⚠️",
                message="Неверные аргументы.",
                message_icon="❗️",
            )
            return
        target_key, value = parts[0], parts[1].strip()
        if allowed and value.lower() not in {item.lower() for item in allowed}:
            await self._send_status_notice(
                ctx,
                title="Настройки оформления",
                icon="⚠️",
                message=f"Допустимо: {', '.join(allowed)}",
                message_icon="❗️",
            )
            return

        if option in {"disable_preview", "show_discord_link"}:
            value = "true" if value.lower() in {"true", "on", "1", "yes"} else "false"
        elif option == "max_length":
            try:
                int(value)
            except ValueError:
                await self._send_status_notice(
                    ctx,
                    title="Настройки оформления",
                    icon="⚠️",
                    message="Введите целое число.",
                    message_icon="❗️",
                )
                return

        if target_key.lower() in {"all", "*"}:
            self._store.set_setting(f"formatting.{option}", value)
        else:
            record = self._store.get_channel(target_key)
            if not record:
                await self._send_status_notice(
                    ctx,
                    title="Настройки оформления",
                    icon="⚠️",
                    message="Канал не найден.",
                    message_icon="❗️",
                )
                return
            self._store.set_channel_option(record.id, f"formatting.{option}", value)
        self._on_change()
        await self._send_panel_message(
            ctx,
            title="Настройки оформления",
            icon="🎨",
            rows=[_panel_bullet("Обновлено.", icon="✅")],
        )

    def _resolve_channel_ids(self, key: str) -> list[int]:
        key_lower = key.lower()
        if key_lower in {"all", "*"}:
            return [0]
        record = self._store.get_channel(key)
        if not record:
            return []
        return [record.id]

    async def _ensure_commands_registered(self) -> None:
        if self._commands_registered:
            return
        await self._api.set_my_commands((info.name, info.summary) for info in BOT_COMMANDS)
        self._commands_registered = True

    async def _notify_access_denied(self, ctx: CommandContext) -> None:
        await self._send_panel_message(
            ctx,
            title="Нет доступа",
            icon="🚫",
            rows=[
                _panel_bullet(
                    (
                        "Эта команда доступна только администраторам. "
                        "Попросите выдать права через <code>/grant</code>."
                    ),
                    icon="🛡️",
                )
            ],
            description="Недостаточно прав для выполнения команды.",
            description_escape=True,
        )


async def send_formatted(
    api: TelegramAPIProtocol,
    chat_id: str,
    message: FormattedTelegramMessage,
    *,
    thread_id: int | None = None,
) -> None:
    if message.text:
        await api.send_message(
            chat_id,
            message.text,
            parse_mode=message.parse_mode,
            disable_preview=message.disable_preview,
            message_thread_id=thread_id,
        )
    for extra in message.extra_messages:
        await api.send_message(
            chat_id,
            extra,
            parse_mode=message.parse_mode,
            disable_preview=message.disable_preview,
            message_thread_id=thread_id,
        )
    for photo in message.image_urls:
        await api.send_photo(
            chat_id,
            photo,
            parse_mode=None,
            message_thread_id=thread_id,
        )


def _split_html_lines(lines: Sequence[str], limit: int = 3500) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    def flush() -> None:
        nonlocal current, current_len
        if current:
            chunks.append("\n".join(current))
            current = []
            current_len = 0

    def append_line(text: str) -> None:
        nonlocal current_len
        parts = _split_single_line(text, limit)
        for part in parts:
            part_len = len(part)
            extra = part_len + (1 if current else 0)
            if current and current_len + extra > limit:
                flush()
            if current:
                current.append(part)
                current_len += part_len + 1
            else:
                current.append(part)
                current_len += part_len

    def append_block(block: list[str]) -> None:
        nonlocal current_len
        if not block:
            return
        block_len = sum(len(item) for item in block) + max(len(block) - 1, 0)
        if block_len > limit:
            for item in block:
                append_line(item)
            return
        extra = block_len + (1 if current else 0)
        if current and current_len + extra > limit:
            flush()
        for index, item in enumerate(block):
            if current:
                current.append(item)
                current_len += len(item) + 1
            else:
                current.append(item)
                current_len += len(item)

    block: list[str] = []
    for line in lines:
        if line == "":
            append_block(block)
            block = []
            if current and current_len + 1 > limit:
                flush()
            if current:
                current.append("")
                current_len += 1
            else:
                current.append("")
        else:
            block.append(line)
    append_block(block)
    flush()
    return chunks or [""]


def _split_single_line(text: str, limit: int) -> list[str]:
    if not text:
        return [""]
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            parts.append(remaining)
            break
        split = remaining.rfind(", ", 0, limit)
        if split == -1 or split < limit // 2:
            split = remaining.rfind(" ", 0, limit)
        if split == -1 or split < limit // 2:
            split = limit
        chunk = remaining[:split].rstrip()
        if not chunk:
            chunk = remaining[:limit]
            split = limit
        parts.append(chunk)
        remaining = remaining[split:].lstrip(", ")
    return parts


logger = logging.getLogger(__name__)

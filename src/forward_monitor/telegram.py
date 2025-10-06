"""Telegram bot facade for configuration and message delivery."""

from __future__ import annotations

import asyncio
import html
import logging
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Iterable, Protocol, Sequence
from urllib.parse import urlparse

import aiohttp

from .config_store import (
    AdminRecord,
    ConfigStore,
    format_filter_value,
    normalize_filter_value,
)
from .discord import DiscordClient
from .filters import FilterEngine
from .formatting import format_discord_message
from .models import FilterConfig, FormattedTelegramMessage
from .utils import RateLimiter, normalize_username, parse_delay_setting

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
    ) -> None:
        self._api = api
        self._store = store
        self._discord = discord_client
        self._offset = 0
        self._running = True
        self._on_change = on_change
        self._commands_registered = False

    async def run(self) -> None:
        await self._ensure_commands_registered()
        while self._running:
            updates = await self._api.get_updates(self._offset, timeout=25)
            for update in updates:
                self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
                await self._handle_update(update)

    def stop(self) -> None:
        """Stop the controller loop on the next iteration."""

        self._running = False

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
        handler = getattr(self, f"cmd_{command}", None)
        if handler is None:
            if self._is_admin(ctx):
                await self._api.send_message(
                    ctx.chat_id,
                    (
                        "ℹ️ <b>Команда не найдена</b>\n"
                        f"Не удалось распознать <code>/{html.escape(command)}</code>. "
                        "Откройте <code>/help</code> для полного списка."
                    ),
                    parse_mode="HTML",
                )
            return

        info = _COMMAND_MAP.get(command)
        has_admins = self._store.has_admins()
        is_admin = self._is_admin(ctx)

        if command == "claim" and not has_admins:
            await self._execute_command(handler, ctx, notify_on_error=True)
            return

        if not is_admin:
            if not has_admins and info is not None and not info.admin_only:
                await self._execute_command(handler, ctx, notify_on_error=False)
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

    # ------------------------------------------------------------------
    # Basic commands
    # ------------------------------------------------------------------
    async def cmd_start(self, ctx: CommandContext) -> None:
        welcome_message = (
            "👋 <b>Forward Monitor</b> на связи.\n"
            "Откройте <code>/help</code>, чтобы перейти в панель управления."
        )
        await self._api.send_message(
            ctx.chat_id,
            welcome_message,
            parse_mode="HTML",
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
                        "/add_channel <discord_id> <telegram_chat[:thread]> <название>",
                        "Создать новую связку, выбрать тему и задать имя.",
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
                        "/add_filter <discord_id|all> <тип> <значение>",
                        "Добавить фильтр (whitelist, blacklist и т.д.).",
                    ),
                    (
                        "/clear_filter <discord_id|all> <тип> [значение]",
                        "Удалить фильтр полностью или по значению.",
                    ),
                ],
            ),
        ]
        lines = [
            "<b>🛠️ Forward Monitor • Основные команды</b>",
            "<i>Все настройки выполняются из этого чата: категории ниже.</i>",
            "",
        ]
        for title, commands in sections:
            lines.append(f"<b>{title}</b>")
            for command, description in commands:
                lines.append(f"• <code>{html.escape(command)}</code> — {html.escape(description)}")
            lines.append("")
        await self._api.send_message(
            ctx.chat_id,
            "\n".join(lines),
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
        monitoring_mode_default = (
            self._store.get_setting("monitoring.mode") or "messages"
        ).strip().lower()
        monitoring_default_desc = (
            "закреплённые сообщения"
            if monitoring_mode_default == "pinned"
            else "новые сообщения"
        )

        default_filter_config = self._store.get_filter_config(0)

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
                "• Пауза между сообщениями: "
                f"{html.escape(_format_seconds(delay_min_value))}–"
                f"{html.escape(_format_seconds(delay_max_value))} с",
                f"• Лимит запросов: {html.escape(str(rate_display))} в секунду",
                "",
                "<b>🎨 Оформление по умолчанию</b>",
                f"• Предпросмотр ссылок: {html.escape(preview_desc)}",
                f"• Максимальная длина: {html.escape(str(max_length_default))} символов",
                f"• Вложения: {html.escape(attachments_desc)}",
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

        lines.append("")
        lines.append("<b>📡 Каналы</b>")
        if channel_configs:
            for channel in channel_configs:
                health_status, health_message = self._store.get_health_status(
                    f"channel.{channel.discord_id}"
                )
                if not channel.active:
                    health_status = "disabled"
                status_icon = _health_icon(health_status)
                lines.append(f"{status_icon} <b>{html.escape(channel.label)}</b>")
                lines.append(
                    f"{_INDENT}• Discord: <code>{html.escape(channel.discord_id)}</code>"
                )
                lines.append(
                    f"{_INDENT}• Telegram: <code>{html.escape(channel.telegram_chat_id)}</code>"
                )
                if channel.telegram_thread_id is not None:
                    thread_value = html.escape(str(channel.telegram_thread_id))
                    lines.append(
                        f"{_INDENT}• Тема: <code>{thread_value}</code>"
                    )
                if health_message:
                    lines.append(f"{_INDENT}• Статус: {html.escape(health_message)}")
                lines.append(
                    f"{_INDENT}• Предпросмотр ссылок: "
                    + (
                        "выключен"
                        if channel.formatting.disable_preview
                        else "включен"
                    )
                )
                lines.append(
                    f"{_INDENT}• Максимальная длина: {channel.formatting.max_length} символов"
                )
                attachment_mode = (
                    "краткий список"
                    if channel.formatting.attachments_style.lower() == "summary"
                    else "список ссылок"
                )
                lines.append(f"{_INDENT}• Вложения: {attachment_mode}")
                lines.append(
                    f"{_INDENT}• Режим: "
                    + (
                        "закреплённые сообщения"
                        if channel.pinned_only
                        else "новые сообщения"
                    )
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
                    lines.append(f"{_INDENT}• Дополнительные фильтры")
                    lines.extend(
                        _describe_filters(
                            extra_filters,
                            indent=_DOUBLE_INDENT,
                            empty_message="",
                        )
                    )
                else:
                    if has_default_filters:
                        lines.append(
                            f"{_INDENT}• Дополнительные фильтры: нет, используются глобальные"
                        )
                    else:
                        lines.append(f"{_INDENT}• Дополнительные фильтры: нет")

                lines.append("")
        else:
            lines.append(f"{_INDENT}• Не настроены")

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
                return
            self._store.add_admin(ctx.user_id, ctx.handle)
            self._on_change()
            await self._api.send_message(
                ctx.chat_id,
                "Ваши административные данные обновлены",
            )
            return
        self._store.add_admin(ctx.user_id, ctx.handle)
        self._on_change()
        await self._api.send_message(ctx.chat_id, "Вы назначены администратором")

    async def cmd_admins(self, ctx: CommandContext) -> None:
        admins = self._store.list_admins()
        if admins:
            lines = ["<b>👑 Администраторы</b>", ""]
            for admin in admins:
                lines.append(f"• {self._format_admin(admin)}")
        else:
            lines = [
                "<b>👑 Администраторы</b>",
                "",
                "Пока никого нет. Используйте <code>/grant</code>, чтобы добавить доступ.",
            ]
        await self._api.send_message(
            ctx.chat_id,
            "\n".join(lines),
            parse_mode="HTML",
        )

    async def cmd_grant(self, ctx: CommandContext) -> None:
        target = ctx.args.strip()
        if not target:
            await self._api.send_message(
                ctx.chat_id,
                "Укажите ID или @username",
            )
            return
        user_id: int | None
        username: str | None
        if target.lstrip("-").isdigit():
            user_id = int(target)
            username = None
        else:
            normalized_username = normalize_username(target)
            if normalized_username is None:
                await self._api.send_message(ctx.chat_id, "Неверное имя пользователя")
                return
            username = normalized_username
            user_id = self._store.resolve_user_id(username)
        self._store.add_admin(user_id, username)
        self._on_change()
        label = self._format_admin(AdminRecord(user_id=user_id, username=username))
        if user_id is None:
            await self._api.send_message(
                ctx.chat_id,
                f"Выдан доступ {label}. Активируется после первого обращения.",
                parse_mode="HTML",
            )
        else:
            await self._api.send_message(
                ctx.chat_id,
                f"Выдан доступ {label}",
                parse_mode="HTML",
            )

    async def cmd_revoke(self, ctx: CommandContext) -> None:
        target = ctx.args.strip()
        if not target:
            await self._api.send_message(
                ctx.chat_id,
                "Укажите ID или @username",
            )
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
                await self._api.send_message(ctx.chat_id, "Неверное имя пользователя")
                return
            removed = self._store.remove_admin(normalized)
            label = self._format_admin(AdminRecord(user_id=None, username=normalized))
        if not removed:
            await self._api.send_message(ctx.chat_id, "Администратор не найден")
            return
        self._on_change()
        await self._api.send_message(
            ctx.chat_id,
            f"Доступ отозван у {label}",
            parse_mode="HTML",
        )

    # ------------------------------------------------------------------
    # Core configuration commands
    # ------------------------------------------------------------------
    async def cmd_set_discord_token(self, ctx: CommandContext) -> None:
        token = ctx.args.strip()
        if not token:
            await self._api.send_message(ctx.chat_id, "Нужно передать токен")
            return
        if token.lower().startswith("bot "):
            await self._api.send_message(
                ctx.chat_id,
                "Укажите пользовательский токен без префикса Bot.",
            )
            return

        network = self._store.load_network_options()
        result = await self._discord.verify_token(token, network=network)
        if not result.ok:
            await self._api.send_message(
                ctx.chat_id,
                result.error or "Не удалось проверить токен Discord.",
            )
            return

        self._store.set_setting("discord.token", token)
        self._on_change()
        display = result.display_name or "пользователь"
        await self._api.send_message(
            ctx.chat_id,
            f"Токен Discord обновлён. Авторизация прошла успешно: {display}",
        )

    async def cmd_set_proxy(self, ctx: CommandContext) -> None:
        parts = ctx.args.split()
        if not parts:
            await self._api.send_message(
                ctx.chat_id,
                "Использование: /set_proxy <url|clear> [логин] [пароль]",
            )
            return
        if parts[0].lower() == "clear":
            if len(parts) > 1:
                await self._api.send_message(
                    ctx.chat_id,
                    "Лишние параметры. Использование: /set_proxy clear",
                )
                return
            self._store.delete_setting("proxy.discord.url")
            self._store.delete_setting("proxy.discord.login")
            self._store.delete_setting("proxy.discord.password")
            self._store.delete_setting("proxy.discord")
            self._on_change()
            await self._api.send_message(ctx.chat_id, "Прокси отключён")
            return

        if len(parts) > 3:
            await self._api.send_message(
                ctx.chat_id,
                "Слишком много параметров. Использование: /set_proxy <url> [логин] [пароль]",
            )
            return

        proxy_url = parts[0]
        parsed = urlparse(proxy_url)
        if not parsed.scheme or not parsed.netloc:
            await self._api.send_message(
                ctx.chat_id,
                "Укажите корректный URL прокси (например, http://host:port).",
            )
            return

        allowed_schemes = {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}
        if parsed.scheme.lower() not in allowed_schemes:
            await self._api.send_message(
                ctx.chat_id,
                "Поддерживаются схемы http, https, socks4, socks5.",
            )
            return

        proxy_login = parts[1] if len(parts) >= 2 else None
        proxy_password = parts[2] if len(parts) >= 3 else None
        if proxy_login and ":" in proxy_login:
            await self._api.send_message(
                ctx.chat_id,
                "Логин не должен содержать двоеточие.",
            )
            return

        network = self._store.load_network_options()
        network.discord_proxy_url = proxy_url
        network.discord_proxy_login = proxy_login
        network.discord_proxy_password = proxy_password

        result = await self._discord.check_proxy(network)
        if not result.ok:
            await self._api.send_message(
                ctx.chat_id,
                result.error or "Прокси не отвечает. Проверьте настройки.",
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
        await self._api.send_message(ctx.chat_id, "Прокси обновлён. Проверка прошла успешно.")

    async def cmd_set_user_agent(self, ctx: CommandContext) -> None:
        value = ctx.args.strip()
        if not value:
            await self._api.send_message(
                ctx.chat_id,
                "Использование: /set_user_agent <значение>",
            )
            return
        self._store.set_setting("ua.discord", value)
        self._store.delete_setting("ua.discord.desktop")
        self._store.delete_setting("ua.discord.mobile")
        self._store.delete_setting("ua.discord.mobile_ratio")
        self._on_change()
        await self._api.send_message(ctx.chat_id, "User-Agent сохранён")

    async def cmd_set_poll(self, ctx: CommandContext) -> None:
        try:
            value = float(ctx.args)
        except ValueError:
            await self._api.send_message(ctx.chat_id, "Укажите число секунд")
            return
        self._store.set_setting("runtime.poll", f"{max(0.5, value):.2f}")
        self._on_change()
        await self._api.send_message(ctx.chat_id, "Интервал опроса обновлён")

    async def cmd_set_delay(self, ctx: CommandContext) -> None:
        parts = ctx.args.split()
        if len(parts) != 2:
            await self._api.send_message(
                ctx.chat_id, "Использование: /set_delay <min_s> <max_s>"
            )
            return
        try:
            min_seconds = float(parts[0])
            max_seconds = float(parts[1])
        except ValueError:
            await self._api.send_message(ctx.chat_id, "Укажите числа в секундах")
            return
        if min_seconds < 0 or max_seconds < min_seconds:
            await self._api.send_message(ctx.chat_id, "Неверный диапазон")
            return
        self._store.set_setting("runtime.delay_min", f"{min_seconds:.2f}")
        self._store.set_setting("runtime.delay_max", f"{max_seconds:.2f}")
        self._on_change()
        await self._api.send_message(ctx.chat_id, "Диапазон задержек сохранён")

    async def cmd_set_rate(self, ctx: CommandContext) -> None:
        value_str = ctx.args.strip()
        if not value_str:
            await self._api.send_message(
                ctx.chat_id,
                "Использование: /set_rate <в_секунду>",
            )
            return
        try:
            value = float(value_str)
        except ValueError:
            await self._api.send_message(ctx.chat_id, "Неверное число")
            return
        self._store.set_setting("runtime.rate", f"{max(0.1, value):.2f}")
        self._store.delete_setting("runtime.discord_rate")
        self._store.delete_setting("runtime.telegram_rate")
        self._on_change()
        await self._api.send_message(ctx.chat_id, "Единый лимит обновлён")

    # ------------------------------------------------------------------
    # Channel management
    # ------------------------------------------------------------------
    async def cmd_add_channel(self, ctx: CommandContext) -> None:
        parts = ctx.args.split()
        if len(parts) < 3:
            await self._api.send_message(
                ctx.chat_id,
                "Использование: /add_channel <discord_id> <telegram_chat[:thread]> <название>",
            )
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
                await self._api.send_message(
                    ctx.chat_id,
                    "Допустимые режимы: messages, pinned",
                )
                return
            parts = parts[:-1]
        elif tail in mode_aliases:
            mode_override = mode_aliases[tail]
            parts = parts[:-1]

        if len(parts) < 3:
            await self._api.send_message(
                ctx.chat_id,
                "Использование: /add_channel <discord_id> <telegram_chat[:thread]> <название>",
            )
            return

        discord_id, telegram_chat_raw, *label_parts = parts
        label = " ".join(label_parts).strip()
        if not label:
            await self._api.send_message(ctx.chat_id, "Укажите название канала")
            return
        thread_id: int | None = None
        telegram_chat = telegram_chat_raw
        if ":" in telegram_chat_raw:
            chat_part, thread_part = telegram_chat_raw.split(":", 1)
            if not chat_part or not thread_part:
                await self._api.send_message(ctx.chat_id, "Неверный формат chat:thread")
                return
            telegram_chat = chat_part
            try:
                thread_id = int(thread_part)
            except ValueError:
                await self._api.send_message(ctx.chat_id, "Thread ID должен быть числом")
                return
            if thread_id <= 0:
                await self._api.send_message(ctx.chat_id, "Thread ID должен быть положительным")
                return
        if self._store.get_channel(discord_id):
            await self._api.send_message(ctx.chat_id, "Канал уже существует")
            return
        token = self._store.get_setting("discord.token")
        if not token:
            await self._api.send_message(
                ctx.chat_id, "Сначала задайте токен Discord командой /set_discord_token"
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
            await self._api.send_message(
                ctx.chat_id,
                "Не удалось проверить канал. Убедитесь в корректности идентификатора.",
            )
            return

        if not exists:
            await self._api.send_message(
                ctx.chat_id,
                "Канал не найден или нет доступа. Проверьте идентификатор и права бота.",
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
        response = f"Связка {discord_id} → {telegram_chat} создана"
        if thread_id is not None:
            response += f" (тема {thread_id})"
        response += f" • режим: {mode_label}"
        await self._api.send_message(ctx.chat_id, response)

    async def cmd_set_thread(self, ctx: CommandContext) -> None:
        parts = ctx.args.split()
        if len(parts) < 2:
            await self._api.send_message(
                ctx.chat_id,
                "Использование: /set_thread <discord_id> <thread_id|clear>",
            )
            return
        discord_id, value = parts[:2]
        record = self._store.get_channel(discord_id)
        if not record:
            await self._api.send_message(ctx.chat_id, "Канал не найден")
            return
        thread_id: int | None
        value_lower = value.lower()
        if value_lower in {"clear", "none", "off", "0"}:
            thread_id = None
        else:
            try:
                thread_id = int(value)
            except ValueError:
                await self._api.send_message(ctx.chat_id, "Thread ID должен быть числом")
                return
            if thread_id <= 0:
                await self._api.send_message(ctx.chat_id, "Thread ID должен быть положительным")
                return
        self._store.set_channel_thread(record.id, thread_id)
        self._on_change()
        if thread_id is None:
            await self._api.send_message(ctx.chat_id, "Тема очищена")
        else:
            await self._api.send_message(ctx.chat_id, f"Установлена тема {thread_id}")

    async def cmd_remove_channel(self, ctx: CommandContext) -> None:
        if not ctx.args:
            await self._api.send_message(ctx.chat_id, "Укажите discord_id")
            return
        removed = self._store.remove_channel(ctx.args)
        self._on_change()
        if removed:
            await self._api.send_message(ctx.chat_id, "Связка удалена")
        else:
            await self._api.send_message(ctx.chat_id, "Канал не найден")

    async def cmd_list_channels(self, ctx: CommandContext) -> None:
        channels = self._store.list_channels()
        if not channels:
            await self._api.send_message(ctx.chat_id, "Каналы не настроены")
            return
        lines = ["<b>📡 Настроенные каналы</b>", ""]
        for record in channels:
            label = html.escape(record.label or record.discord_id)
            discord_id = html.escape(record.discord_id)
            chat_id = html.escape(record.telegram_chat_id)
            health_status, _ = self._store.get_health_status(
                f"channel.{record.discord_id}"
            )
            if not record.active:
                health_status = "disabled"
            status_icon = _health_icon(health_status)
            thread_info = ""
            if record.telegram_thread_id is not None:
                thread_info = (
                    f" (тема <code>{html.escape(str(record.telegram_thread_id))}</code>)"
                )
            lines.append(
                f"{status_icon} <b>{label}</b> — Discord <code>{discord_id}</code> → "
                f"Telegram <code>{chat_id}</code>{thread_info}"
            )
        await self._api.send_message(
            ctx.chat_id,
            "\n".join(lines),
            parse_mode="HTML",
        )

    async def cmd_send_recent(self, ctx: CommandContext) -> None:
        parts = ctx.args.split()
        if not parts:
            await self._api.send_message(
                ctx.chat_id, "Использование: /send_recent <кол-во> [discord_id|all]"
            )
            return

        try:
            requested = int(parts[0])
        except ValueError:
            await self._api.send_message(ctx.chat_id, "Количество должно быть числом")
            return

        if requested <= 0:
            await self._api.send_message(
                ctx.chat_id, "Количество должно быть положительным"
            )
            return

        target = parts[1] if len(parts) > 1 else "all"
        limit = min(requested, 100)
        token = self._store.get_setting("discord.token")
        if not token:
            await self._api.send_message(
                ctx.chat_id, "Сначала задайте токен Discord командой /set_discord_token"
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
                await self._api.send_message(ctx.chat_id, "Канал не найден")
                return
            selected = [channel]

        if not selected:
            await self._api.send_message(ctx.chat_id, "Каналы не настроены")
            return

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

        if requested > 100:
            summary_lines.append(
                "Запрошено больше 100 сообщений, будет переслано не более 100 из каждого канала."
            )

        for channel in selected:
            label = html.escape(channel.label)
            if not channel.active:
                summary_lines.append(f"{label}: канал отключён, пропущено")
                continue
            if channel.pinned_only:
                summary_lines.append(
                    f"{label}: канал отслеживает только закреплённые сообщения, пропущено"
                )
                continue

            try:
                messages = await self._discord.fetch_messages(
                    channel.discord_id,
                    limit=limit,
                )
            except Exception:
                logger.exception(
                    "Не удалось получить сообщения канала %s при ручной пересылке",
                    channel.discord_id,
                )
                summary_lines.append(f"{label}: ошибка при запросе сообщений")
                continue

            if not messages:
                summary_lines.append(f"{label}: новых сообщений нет")
                continue

            def sort_key(message_id: str) -> tuple[int, str]:
                return (int(message_id), message_id) if message_id.isdigit() else (0, message_id)

            ordered = sorted(messages, key=lambda msg: sort_key(msg.id))
            subset = ordered[-limit:]
            engine = FilterEngine(channel.filters)
            forwarded = 0
            last_seen = channel.last_message_id

            for msg in subset:
                candidate_id = msg.id
                if (
                    msg.message_type not in _FORWARDABLE_MESSAGE_TYPES
                    and not (msg.attachments or msg.embeds)
                ):
                    last_seen = candidate_id
                    continue
                decision = engine.evaluate(msg)
                if not decision.allowed:
                    last_seen = candidate_id
                    continue
                formatted = format_discord_message(msg, channel)
                try:
                    await limiter.wait()
                    await send_formatted(
                        self._api,
                        channel.telegram_chat_id,
                        formatted,
                        thread_id=channel.telegram_thread_id,
                    )
                except Exception:
                    logger.exception(
                        "Не удалось отправить сообщение %s в Telegram чат %s при ручной пересылке",
                        msg.id,
                        channel.telegram_chat_id,
                    )
                    last_seen = candidate_id
                    continue
                last_seen = candidate_id
                forwarded += 1
                total_forwarded += 1

            if forwarded:
                summary_lines.append(
                    f"{label}: переслано {forwarded} из {len(subset)} сообщений"
                )
            else:
                summary_lines.append(
                    f"{label}: подходящих сообщений не найдено"
                )

            if (
                last_seen
                and channel.storage_id is not None
                and last_seen != channel.last_message_id
            ):
                self._store.set_last_message(channel.storage_id, last_seen)
                state_changed = True

        if state_changed:
            self._on_change()

        summary_lines.append("")
        summary_lines.append(f"Всего переслано: {total_forwarded}")

        await self._api.send_message(
            ctx.chat_id,
            "\n".join(summary_lines),
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
            await self._api.send_message(
                ctx.chat_id,
                "Использование: /set_monitoring <discord_id|all> <messages|pinned>",
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
            await self._api.send_message(
                ctx.chat_id,
                "Допустимые режимы: messages, pinned",
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
            await self._api.send_message(ctx.chat_id, description)
            return

        record = self._store.get_channel(target_key)
        if not record:
            await self._api.send_message(ctx.chat_id, "Канал не найден")
            return

        if normalized_mode == "messages":
            self._store.delete_channel_option(record.id, "monitoring.mode")
            self._store.clear_known_pinned_messages(record.id)
            self._store.set_pinned_synced(record.id, synced=False)
            self._on_change()
            await self._api.send_message(ctx.chat_id, "Канал переключён на обычные сообщения")
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
        await self._api.send_message(
            ctx.chat_id,
            "Канал переключён на закреплённые сообщения",
        )

    async def cmd_add_filter(self, ctx: CommandContext) -> None:
        parts = ctx.args.split(maxsplit=2)
        if len(parts) < 3:
            await self._api.send_message(
                ctx.chat_id,
                "Использование: /add_filter <discord_id|all> <тип> <значение>",
            )
            return
        target_key, filter_type_raw, value = parts
        filter_type = filter_type_raw.strip().lower()
        if filter_type not in _FILTER_TYPES:
            await self._api.send_message(
                ctx.chat_id,
                "Неизвестный тип фильтра. Допустимо: " + ", ".join(_FILTER_TYPES),
            )
            return
        channel_ids = self._resolve_channel_ids(target_key)
        if not channel_ids:
            await self._api.send_message(ctx.chat_id, "Канал не найден")
            return
        added = False
        for channel_id in channel_ids:
            try:
                changed = self._store.add_filter(channel_id, filter_type, value)
            except ValueError:
                await self._api.send_message(ctx.chat_id, "Неверное значение фильтра")
                return
            added = added or changed
        if added:
            self._on_change()
            await self._api.send_message(ctx.chat_id, "Фильтр добавлен")
        else:
            await self._api.send_message(ctx.chat_id, "Такой фильтр уже существует")

    async def cmd_clear_filter(self, ctx: CommandContext) -> None:
        parts = ctx.args.split(maxsplit=2)
        if len(parts) < 2:
            await self._api.send_message(
                ctx.chat_id,
                "Использование: /clear_filter <discord_id|all> <тип> [значение]",
            )
            return
        target_key, filter_type_raw = parts[0], parts[1]
        filter_type = filter_type_raw.strip().lower()
        value = parts[2] if len(parts) == 3 else None
        channel_ids = self._resolve_channel_ids(target_key)
        if not channel_ids:
            await self._api.send_message(ctx.chat_id, "Канал не найден")
            return
        if filter_type in {"all", "*"}:
            removed = sum(self._store.clear_filters(channel_id) for channel_id in channel_ids)
            if removed:
                self._on_change()
                await self._api.send_message(ctx.chat_id, "Все фильтры очищены")
            else:
                await self._api.send_message(ctx.chat_id, "Фильтры уже очищены")
            return

        if filter_type not in _FILTER_TYPES:
            await self._api.send_message(
                ctx.chat_id,
                "Неизвестный тип фильтра. Допустимо: " + ", ".join(_FILTER_TYPES),
            )
            return

        removed = 0
        if value is None:
            for channel_id in channel_ids:
                removed += self._store.remove_filter(channel_id, filter_type, None)
            if removed:
                self._on_change()
                await self._api.send_message(ctx.chat_id, "Фильтры удалены")
            else:
                await self._api.send_message(
                    ctx.chat_id, "Фильтров этого типа не найдено"
                )
            return

        for channel_id in channel_ids:
            removed += self._store.remove_filter(channel_id, filter_type, value)
        if removed:
            self._on_change()
            await self._api.send_message(ctx.chat_id, "Фильтр удалён")
        else:
            await self._api.send_message(ctx.chat_id, "Такого фильтра не существует")

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
            await self._api.send_message(ctx.chat_id, "Неверные аргументы")
            return
        target_key, value = parts[0], parts[1].strip()
        if allowed and value.lower() not in {item.lower() for item in allowed}:
            await self._api.send_message(ctx.chat_id, f"Допустимо: {', '.join(allowed)}")
            return

        if option in {"disable_preview", "show_discord_link"}:
            value = "true" if value.lower() in {"true", "on", "1", "yes"} else "false"
        elif option == "max_length":
            try:
                int(value)
            except ValueError:
                await self._api.send_message(ctx.chat_id, "Введите целое число")
                return

        if target_key.lower() in {"all", "*"}:
            self._store.set_setting(f"formatting.{option}", value)
        else:
            record = self._store.get_channel(target_key)
            if not record:
                await self._api.send_message(ctx.chat_id, "Канал не найден")
                return
            self._store.set_channel_option(record.id, f"formatting.{option}", value)
        self._on_change()
        await self._api.send_message(ctx.chat_id, "Обновлено")

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

    for line in lines:
        parts = _split_single_line(line, limit)
        for part in parts:
            part_len = len(part)
            extra = part_len + (1 if current else 0)
            if current and current_len + extra > limit:
                chunks.append("\n".join(current))
                current = [part]
                current_len = part_len
            else:
                if current:
                    current_len += 1
                current.append(part)
                current_len += part_len
    if current:
        chunks.append("\n".join(current))
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

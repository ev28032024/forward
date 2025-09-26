"""Telegram bot facade for configuration and message delivery."""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Protocol

import aiohttp

from .config_store import ConfigStore
from .models import FormattedTelegramMessage

_API_BASE = "https://api.telegram.org"


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
        except aiohttp.ClientError:
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
        except aiohttp.ClientError:
            return

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        parse_mode: str | None = None,
        disable_preview: bool = True,
    ) -> None:
        url = f"{_API_BASE}/bot{self._token}/sendMessage"
        data: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_preview,
        }
        if parse_mode:
            data["parse_mode"] = parse_mode
        try:
            timeout_cfg = aiohttp.ClientTimeout(total=15)
            async with self._session.post(
                url,
                json=data,
                timeout=timeout_cfg,
            ) as resp:
                await resp.read()
        except aiohttp.ClientError:
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
        except aiohttp.ClientError:
            return


@dataclass(slots=True)
class CommandContext:
    chat_id: int
    user_id: int
    username: str
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
        summary="Приветствие и краткая справка.",
        help_text="/start — Forward Monitor готов. Используйте /help для списка команд.",
        admin_only=False,
    ),
    _CommandInfo(
        name="help",
        summary="Список команд управления.",
        help_text="/help — полный список команд.",
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
        summary="Показать текущие настройки.",
        help_text="/status — текущая конфигурация",
        admin_only=False,
    ),
    _CommandInfo(
        name="admins",
        summary="Показать администраторов.",
        help_text="/admins — показать администраторов",
    ),
    _CommandInfo(
        name="grant",
        summary="Выдать права администрирования.",
        help_text="/grant <id> — выдать права",
    ),
    _CommandInfo(
        name="revoke",
        summary="Отозвать права администрирования.",
        help_text="/revoke <id> — отобрать права",
    ),
    _CommandInfo(
        name="set_discord_token",
        summary="Сохранить токен Discord.",
        help_text="/set_discord_token <token>",
    ),
    _CommandInfo(
        name="add_channel",
        summary="Добавить связку каналов.",
        help_text="/add_channel <discord_id> <telegram_chat> [метка]",
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
        name="set_header",
        summary="Задать шапку сообщений.",
        help_text="/set_header <discord_id|all> <текст>",
    ),
    _CommandInfo(
        name="set_footer",
        summary="Задать подпись сообщений.",
        help_text="/set_footer <discord_id|all> <текст>",
    ),
    _CommandInfo(
        name="set_chip",
        summary="Задать маркер-стикер.",
        help_text="/set_chip <discord_id|all> <текст>",
    ),
    _CommandInfo(
        name="set_parse_mode",
        summary="Выбрать режим форматирования.",
        help_text="/set_parse_mode <discord_id|all> <markdownv2|markdown|html|text>",
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
        name="add_replace",
        summary="Добавить правило замены.",
        help_text="/add_replace <discord_id|all> шаблон => замена",
    ),
    _CommandInfo(
        name="clear_replace",
        summary="Удалить правила замены.",
        help_text="/clear_replace <discord_id|all> [шаблон]",
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
        help_text="/set_delay <min_ms> <max_ms>",
    ),
    _CommandInfo(
        name="set_rate",
        summary="Настроить лимиты запросов.",
        help_text="/set_rate <в_секунду>",
    ),
    _CommandInfo(
        name="set_fallback_chat",
        summary="Указать резервный чат Telegram.",
        help_text="/set_fallback_chat <chat_id>",
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
        on_change: Callable[[], None],
    ) -> None:
        self._api = api
        self._store = store
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
        username = str(sender.get("username") or sender.get("first_name") or "user")
        ctx = CommandContext(
            chat_id=int(message["chat"]["id"]),
            user_id=int(sender["id"]),
            username=username,
            args=args.strip(),
            message=message,
        )
        await self._dispatch(command, ctx)

    async def _dispatch(self, command: str, ctx: CommandContext) -> None:
        handler = getattr(self, f"cmd_{command}", None)
        if handler is None:
            await self._api.send_message(ctx.chat_id, f"Неизвестная команда: {command}")
            return
        info = _COMMAND_MAP.get(command)
        requires_admin = info.admin_only if info else True
        if requires_admin and ctx.user_id not in self._store.list_admins():
            await self._api.send_message(ctx.chat_id, "Команда доступна только администраторам")
            return
        await handler(ctx)

    # ------------------------------------------------------------------
    # Basic commands
    # ------------------------------------------------------------------
    async def cmd_start(self, ctx: CommandContext) -> None:
        welcome_message = (
            "✨ Forward Monitor готов к работе. "
            "Используйте /help, чтобы увидеть панель команд."
        )
        await self._api.send_message(ctx.chat_id, welcome_message)

    async def cmd_help(self, ctx: CommandContext) -> None:
        lines = ["<b>📚 Панель управления Forward Monitor</b>", ""]
        for info in BOT_COMMANDS:
            if info.name in {"start", "help"}:
                continue
            icon = "🔐" if info.admin_only else "✨"
            lines.append(
                f"{icon} <code>/{html.escape(info.name)}</code> — {html.escape(info.summary)}"
            )
            lines.append(f"<i>{html.escape(info.help_text)}</i>")
            lines.append("")
        await self._api.send_message(
            ctx.chat_id,
            "\n".join(lines),
            parse_mode="HTML",
        )

    async def cmd_status(self, ctx: CommandContext) -> None:
        discord_token = "✅ установлено" if self._store.get_setting("discord.token") else "⛔ нет"
        fallback = self._store.get_setting("telegram.fallback_chat") or "не задан"
        proxy_url = self._store.get_setting("proxy.discord.url")
        proxy_login = self._store.get_setting("proxy.discord.login")
        proxy_password = self._store.get_setting("proxy.discord.password")
        user_agent = self._store.get_setting("ua.discord") or "по умолчанию"
        poll_value = self._store.get_setting("runtime.poll", "2.0")
        poll = poll_value if poll_value is not None else "2.0"
        delay_min_value = self._store.get_setting("runtime.delay_min", "0")
        delay_min = delay_min_value if delay_min_value is not None else "0"
        delay_max_value = self._store.get_setting("runtime.delay_max", "0")
        delay_max = delay_max_value if delay_max_value is not None else "0"
        rate = self._store.get_setting("runtime.rate")
        if rate is None:
            legacy_discord = self._store.get_setting("runtime.discord_rate") or "4.0"
            legacy_telegram = self._store.get_setting("runtime.telegram_rate") or legacy_discord
            rate_display = f"{legacy_discord}/{legacy_telegram} (legacy)"
        else:
            rate_display = f"{rate}"

        proxy_lines: list[str] = []
        if proxy_url:
            proxy_lines.append(f"• URL: {html.escape(proxy_url)}")
            if proxy_login:
                proxy_lines.append(f"• Логин: {html.escape(proxy_login)}")
            if proxy_password:
                proxy_lines.append("• Пароль: ••••••")
        else:
            proxy_lines.append("• не задан")

        channels = self._store.list_channels()
        channel_lines = []
        for record in channels[:8]:
            label = record.label or record.discord_id
            status_icon = "🟢" if record.active else "⚪️"
            discord_id = html.escape(str(record.discord_id))
            chat_id = html.escape(str(record.telegram_chat_id))
            channel_label = html.escape(str(label))
            channel_lines.append(
                (
                    f"{status_icon} <code>{discord_id}</code> → "
                    f"<code>{chat_id}</code> — {channel_label}"
                )
            )
        if len(channels) > 8:
            channel_lines.append(f"… и ещё {len(channels) - 8} каналов")
        if not channel_lines:
            channel_lines.append("Каналы не настроены")

        lines = [
            "<b>⚙️ Статус Forward Monitor</b>",
            "",
            f"🔑 <b>Discord токен:</b> {discord_token}",
            f"💬 <b>Fallback чат:</b> {html.escape(fallback)}",
            "",
            "<b>🌐 Прокси Discord</b>",
            *proxy_lines,
            "",
            f"🧾 <b>User-Agent:</b> {html.escape(user_agent)}",
            f"⏱️ <b>Интервал опроса:</b> {html.escape(poll)} с",
            f"🎲 <b>Задержка:</b> {html.escape(delay_min)}–{html.escape(delay_max)} мс",
            f"🚦 <b>Лимит:</b> {html.escape(rate_display)} запрос/с",
            "",
            "<b>📡 Каналы</b>",
            *channel_lines,
        ]
        await self._api.send_message(
            ctx.chat_id,
            "\n".join(lines),
            parse_mode="HTML",
        )

    async def cmd_claim(self, ctx: CommandContext) -> None:
        if self._store.has_admins():
            await self._api.send_message(ctx.chat_id, "Администратор уже назначен")
            return
        self._store.add_admin(ctx.user_id)
        self._on_change()
        await self._api.send_message(ctx.chat_id, "Вы назначены администратором")

    async def cmd_admins(self, ctx: CommandContext) -> None:
        admins = self._store.list_admins()
        if admins:
            lines = ["Администраторы:", *[str(admin) for admin in admins]]
        else:
            lines = ["Список пуст"]
        await self._api.send_message(ctx.chat_id, "\n".join(lines))

    async def cmd_grant(self, ctx: CommandContext) -> None:
        try:
            user_id = int(ctx.args)
        except ValueError:
            await self._api.send_message(ctx.chat_id, "Укажите ID пользователя")
            return
        self._store.add_admin(user_id)
        self._on_change()
        await self._api.send_message(ctx.chat_id, f"Выдан доступ {user_id}")

    async def cmd_revoke(self, ctx: CommandContext) -> None:
        try:
            user_id = int(ctx.args)
        except ValueError:
            await self._api.send_message(ctx.chat_id, "Укажите ID пользователя")
            return
        self._store.remove_admin(user_id)
        self._on_change()
        await self._api.send_message(ctx.chat_id, f"Доступ отозван у {user_id}")

    # ------------------------------------------------------------------
    # Core configuration commands
    # ------------------------------------------------------------------
    async def cmd_set_discord_token(self, ctx: CommandContext) -> None:
        if not ctx.args:
            await self._api.send_message(ctx.chat_id, "Нужно передать токен")
            return
        self._store.set_setting("discord.token", ctx.args.strip())
        self._on_change()
        await self._api.send_message(ctx.chat_id, "Токен Discord обновлён")

    async def cmd_set_fallback_chat(self, ctx: CommandContext) -> None:
        if not ctx.args:
            await self._api.send_message(ctx.chat_id, "Укажите chat_id")
            return
        self._store.set_setting("telegram.fallback_chat", ctx.args.strip())
        self._on_change()
        await self._api.send_message(ctx.chat_id, "Fallback чат обновлён")

    async def cmd_set_proxy(self, ctx: CommandContext) -> None:
        parts = ctx.args.split()
        if not parts:
            await self._api.send_message(
                ctx.chat_id,
                "Использование: /set_proxy <url|clear> [логин] [пароль]",
            )
            return
        action = parts[0].lower()
        if action == "clear":
            self._store.delete_setting("proxy.discord.url")
            self._store.delete_setting("proxy.discord.login")
            self._store.delete_setting("proxy.discord.password")
            self._store.delete_setting("proxy.discord")
            message = "Прокси отключён"
        else:
            url = parts[0]
            self._store.set_setting("proxy.discord.url", url)
            if len(parts) >= 2:
                self._store.set_setting("proxy.discord.login", parts[1])
            else:
                self._store.delete_setting("proxy.discord.login")
            if len(parts) >= 3:
                self._store.set_setting("proxy.discord.password", parts[2])
            else:
                self._store.delete_setting("proxy.discord.password")
            self._store.delete_setting("proxy.discord")
            message = "Прокси обновлён"
        self._on_change()
        await self._api.send_message(ctx.chat_id, message)

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
            await self._api.send_message(ctx.chat_id, "Использование: /set_delay <min_ms> <max_ms>")
            return
        try:
            min_ms = int(parts[0])
            max_ms = int(parts[1])
        except ValueError:
            await self._api.send_message(ctx.chat_id, "Значения должны быть целыми")
            return
        if min_ms < 0 or max_ms < min_ms:
            await self._api.send_message(ctx.chat_id, "Неверный диапазон")
            return
        self._store.set_setting("runtime.delay_min", str(min_ms))
        self._store.set_setting("runtime.delay_max", str(max_ms))
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
        if len(parts) < 2:
            await self._api.send_message(
                ctx.chat_id,
                "Использование: /add_channel <discord_id> <telegram_chat> [метка]",
            )
            return
        discord_id, telegram_chat = parts[0], parts[1]
        label = " ".join(parts[2:]) if len(parts) > 2 else discord_id
        if self._store.get_channel(discord_id):
            await self._api.send_message(ctx.chat_id, "Канал уже существует")
            return
        self._store.add_channel(discord_id, telegram_chat, label)
        self._on_change()
        await self._api.send_message(ctx.chat_id, f"Связка {discord_id} → {telegram_chat} создана")

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
            await self._api.send_message(ctx.chat_id, "Список пуст")
            return
        lines = [
            "Каналы:",
            *[
                f"{record.discord_id} → {record.telegram_chat_id} [{record.label}]"
                for record in channels
            ],
        ]
        await self._api.send_message(ctx.chat_id, "\n".join(lines))

    async def cmd_set_header(self, ctx: CommandContext) -> None:
        await self._set_format_option(ctx, "header")

    async def cmd_set_footer(self, ctx: CommandContext) -> None:
        await self._set_format_option(ctx, "footer")

    async def cmd_set_chip(self, ctx: CommandContext) -> None:
        await self._set_format_option(ctx, "chip")

    async def cmd_set_parse_mode(self, ctx: CommandContext) -> None:
        await self._set_format_option(
            ctx,
            "parse_mode",
            allowed={"markdownv2", "markdown", "html", "text"},
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

    async def cmd_add_filter(self, ctx: CommandContext) -> None:
        parts = ctx.args.split(maxsplit=2)
        if len(parts) < 3:
            await self._api.send_message(
                ctx.chat_id,
                "Использование: /add_filter <discord_id|all> <тип> <значение>",
            )
            return
        target_key, filter_type, value = parts
        channel_ids = self._resolve_channel_ids(target_key)
        if not channel_ids:
            await self._api.send_message(ctx.chat_id, "Канал не найден")
            return
        for channel_id in channel_ids:
            self._store.add_filter(channel_id, filter_type, value)
        self._on_change()
        await self._api.send_message(ctx.chat_id, "Фильтр добавлен")

    async def cmd_clear_filter(self, ctx: CommandContext) -> None:
        parts = ctx.args.split(maxsplit=2)
        if len(parts) < 2:
            await self._api.send_message(
                ctx.chat_id,
                "Использование: /clear_filter <discord_id|all> <тип> [значение]",
            )
            return
        target_key, filter_type = parts[0], parts[1]
        value = parts[2] if len(parts) == 3 else None
        channel_ids = self._resolve_channel_ids(target_key)
        if not channel_ids:
            await self._api.send_message(ctx.chat_id, "Канал не найден")
            return
        removed = 0
        for channel_id in channel_ids:
            removed += self._store.remove_filter(channel_id, filter_type, value)
        self._on_change()
        await self._api.send_message(ctx.chat_id, f"Удалено {removed} записей")

    async def cmd_add_replace(self, ctx: CommandContext) -> None:
        target, pattern, replacement = self._parse_replace_args(ctx)
        if target is None:
            await self._api.send_message(
                ctx.chat_id,
                "Использование: /add_replace <discord_id|all> шаблон => замена",
            )
            return
        for channel_id in target:
            self._store.add_replacement(channel_id, pattern, replacement)
        self._on_change()
        await self._api.send_message(ctx.chat_id, "Замена сохранена")

    async def cmd_clear_replace(self, ctx: CommandContext) -> None:
        parts = ctx.args.split(maxsplit=1)
        if not parts:
            await self._api.send_message(
                ctx.chat_id,
                "Использование: /clear_replace <discord_id|all> [шаблон]",
            )
            return
        target_key = parts[0]
        pattern = parts[1] if len(parts) == 2 else None
        channel_ids = self._resolve_channel_ids(target_key)
        if not channel_ids:
            await self._api.send_message(ctx.chat_id, "Канал не найден")
            return
        removed = 0
        for channel_id in channel_ids:
            removed += self._store.remove_replacement(channel_id, pattern)
        self._on_change()
        await self._api.send_message(ctx.chat_id, f"Удалено {removed} замен")

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

        if option == "disable_preview":
            value = "true" if value.lower() in {"true", "on", "1", "yes"} else "false"
        elif option == "max_length":
            try:
                int(value)
            except ValueError:
                await self._api.send_message(ctx.chat_id, "Введите целое число")
                return
        elif option == "parse_mode":
            value = value.lower()

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

    def _parse_replace_args(self, ctx: CommandContext) -> tuple[list[int] | None, str, str]:
        parts = ctx.args.split(maxsplit=1)
        if len(parts) < 2:
            return (None, "", "")
        target_key, rest = parts
        if "=>" not in rest:
            return (None, "", "")
        pattern, replacement = [segment.strip() for segment in rest.split("=>", 1)]
        ids = self._resolve_channel_ids(target_key)
        if not ids or not pattern:
            return (None, "", "")
        return (ids, pattern, replacement)

    async def _ensure_commands_registered(self) -> None:
        if self._commands_registered:
            return
        await self._api.set_my_commands((info.name, info.summary) for info in BOT_COMMANDS)
        self._commands_registered = True


async def send_formatted(
    api: TelegramAPIProtocol,
    chat_id: str,
    message: FormattedTelegramMessage,
) -> None:
    await api.send_message(
        chat_id,
        message.text,
        parse_mode=message.parse_mode,
        disable_preview=message.disable_preview,
    )
    for extra in message.extra_messages:
        await api.send_message(
            chat_id,
            extra,
            parse_mode=message.parse_mode,
            disable_preview=message.disable_preview,
        )

"""Telegram bot facade for configuration and message delivery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

import aiohttp

from .config_store import ConfigStore
from .models import FormattedTelegramMessage

_API_BASE = "https://api.telegram.org"


class TelegramAPI:
    """Lightweight Telegram Bot API wrapper."""

    def __init__(self, token: str, session: aiohttp.ClientSession):
        self._token = token
        self._session = session
        self._proxy: str | None = None

    def set_proxy(self, proxy: str | None) -> None:
        self._proxy = proxy

    async def get_updates(self, offset: int | None = None, timeout: int = 30) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        url = f"{_API_BASE}/bot{self._token}/getUpdates"
        try:
            async with self._session.get(url, params=params, timeout=timeout + 5, proxy=self._proxy) as resp:
                payload = await resp.json(content_type=None)
        except aiohttp.ClientError:
            return []
        if not payload.get("ok"):
            return []
        return list(payload.get("result") or [])

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
            async with self._session.post(url, json=data, proxy=self._proxy, timeout=15) as resp:
                await resp.read()
        except aiohttp.ClientError:
            return

    async def answer_callback_query(self, callback_id: str, text: str) -> None:
        url = f"{_API_BASE}/bot{self._token}/answerCallbackQuery"
        data = {"callback_query_id": callback_id, "text": text[:200]}
        try:
            async with self._session.post(url, json=data, proxy=self._proxy, timeout=10) as resp:
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


class TelegramController:
    """Interactive Telegram bot that manages runtime configuration."""

    def __init__(
        self,
        api: TelegramAPI,
        store: ConfigStore,
        *,
        on_change: Callable[[], None],
    ) -> None:
        self._api = api
        self._store = store
        self._offset = 0
        self._running = True
        self._on_change = on_change

    async def run(self) -> None:
        while self._running:
            updates = await self._api.get_updates(self._offset, timeout=25)
            for update in updates:
                self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
                await self._handle_update(update)

    async def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") or update.get("edited_message")
        if not message:
            return
        text = str(message.get("text") or "").strip()
        if not text.startswith("/"):
            return
        command, _, args = text.partition(" ")
        command = command.split("@")[0][1:].lower()
        ctx = CommandContext(
            chat_id=int(message["chat"]["id"]),
            user_id=int(message["from"]["id"]),
            username=str(message["from"].get("username") or message["from"].get("first_name") or "user"),
            args=args.strip(),
            message=message,
        )
        await self._dispatch(command, ctx)

    async def _dispatch(self, command: str, ctx: CommandContext) -> None:
        handler = getattr(self, f"cmd_{command}", None)
        if handler is None:
            await self._api.send_message(ctx.chat_id, f"Неизвестная команда: {command}")
            return
        requires_admin = command not in {"start", "help", "claim", "status"}
        if requires_admin and ctx.user_id not in self._store.list_admins():
            await self._api.send_message(ctx.chat_id, "Команда доступна только администраторам")
            return
        await handler(ctx)

    # ------------------------------------------------------------------
    # Basic commands
    # ------------------------------------------------------------------
    async def cmd_start(self, ctx: CommandContext) -> None:
        await self._api.send_message(
            ctx.chat_id,
            "Forward Monitor готов. Используйте /help для списка команд.",
        )

    async def cmd_help(self, ctx: CommandContext) -> None:
        commands = [
            "/claim — стать администратором (если список пуст)",
            "/admins — показать администраторов",
            "/grant <id> — выдать права",
            "/revoke <id> — отобрать права",
            "/status — текущая конфигурация",
            "/set_discord_token <token>",
            "/add_channel <discord_id> <telegram_chat> [метка]",
            "/remove_channel <discord_id>",
            "/list_channels",
            "/set_header <discord_id|all> <текст>",
            "/set_footer <discord_id|all> <текст>",
            "/set_chip <discord_id|all> <текст>",
            "/set_parse_mode <discord_id|all> <markdownv2|markdown|html|text>",
            "/set_disable_preview <discord_id|all> <on|off>",
            "/set_max_length <discord_id|all> <число>",
            "/set_attachments <discord_id|all> <summary|links>",
            "/add_filter <discord_id|all> <тип> <значение>",
            "/clear_filter <discord_id|all> <тип> [значение]",
            "/add_replace <discord_id|all> шаблон => замена",
            "/clear_replace <discord_id|all> [шаблон]",
            "/set_proxy <discord|telegram|clear> [url]",
            "/set_user_agent <desktop|mobile> <значение>",
            "/set_mobile_ratio <0-1>",
            "/set_poll <секунды>",
            "/set_delay <min_ms> <max_ms>",
            "/set_rate <discord|telegram> <в_секунду>",
            "/set_fallback_chat <chat_id>",
        ]
        await self._api.send_message(ctx.chat_id, "\n".join(commands))

    async def cmd_status(self, ctx: CommandContext) -> None:
        discord_token = "установлен" if self._store.get_setting("discord.token") else "нет"
        fallback = self._store.get_setting("telegram.fallback_chat") or "не задан"
        proxies = {
            "discord": self._store.get_setting("proxy.discord") or "-",
            "telegram": self._store.get_setting("proxy.telegram") or "-",
        }
        ua = {
            "desktop": self._store.get_setting("ua.discord.desktop") or "по умолчанию",
            "mobile": self._store.get_setting("ua.discord.mobile") or "по умолчанию",
            "ratio": self._store.get_setting("ua.discord.mobile_ratio") or "0",
        }
        poll = self._store.get_setting("runtime.poll", "2.0")
        delay_min = self._store.get_setting("runtime.delay_min", "0")
        delay_max = self._store.get_setting("runtime.delay_max", "0")
        rate_d = self._store.get_setting("runtime.discord_rate", "4")
        rate_t = self._store.get_setting("runtime.telegram_rate", "25")
        channels = self._store.list_channels()
        lines = [
            f"Discord токен: {discord_token}",
            f"Fallback чат: {fallback}",
            f"Прокси Discord: {proxies['discord']}",
            f"Прокси Telegram: {proxies['telegram']}",
            f"User-Agent desktop: {ua['desktop']}",
            f"User-Agent mobile: {ua['mobile']} (ratio {ua['ratio']})",
            f"Опрос Discord: {poll} c",
            f"Пауза: {delay_min}-{delay_max} мс",
            f"Лимиты: Discord {rate_d}/c, Telegram {rate_t}/c",
            f"Каналы: {len(channels)}",
        ]
        await self._api.send_message(ctx.chat_id, "\n".join(lines))

    async def cmd_claim(self, ctx: CommandContext) -> None:
        if self._store.has_admins():
            await self._api.send_message(ctx.chat_id, "Администратор уже назначен")
            return
        self._store.add_admin(ctx.user_id)
        self._on_change()
        await self._api.send_message(ctx.chat_id, "Вы назначены администратором")

    async def cmd_admins(self, ctx: CommandContext) -> None:
        admins = self._store.list_admins()
        lines = ["Администраторы:", *[str(admin) for admin in admins]] if admins else ["Список пуст"]
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
            await self._api.send_message(ctx.chat_id, "Использование: /set_proxy <discord|telegram|clear> [url]")
            return
        target = parts[0].lower()
        if target == "clear":
            self._store.delete_setting("proxy.discord")
            self._store.delete_setting("proxy.telegram")
        elif target in {"discord", "telegram"}:
            if len(parts) < 2:
                await self._api.send_message(ctx.chat_id, "Нужно указать URL прокси")
                return
            self._store.set_setting(f"proxy.{target}", parts[1])
        else:
            await self._api.send_message(ctx.chat_id, "Неизвестная цель")
            return
        self._on_change()
        await self._api.send_message(ctx.chat_id, "Прокси обновлены")

    async def cmd_set_user_agent(self, ctx: CommandContext) -> None:
        parts = ctx.args.split(maxsplit=1)
        if len(parts) < 2:
            await self._api.send_message(ctx.chat_id, "Использование: /set_user_agent <desktop|mobile> <ua>")
            return
        target, value = parts[0].lower(), parts[1].strip()
        if target not in {"desktop", "mobile"}:
            await self._api.send_message(ctx.chat_id, "Только desktop или mobile")
            return
        self._store.set_setting(f"ua.discord.{target}", value)
        self._on_change()
        await self._api.send_message(ctx.chat_id, "User-Agent сохранён")

    async def cmd_set_mobile_ratio(self, ctx: CommandContext) -> None:
        try:
            value = float(ctx.args)
        except ValueError:
            await self._api.send_message(ctx.chat_id, "Укажите число от 0 до 1")
            return
        value = max(0.0, min(1.0, value))
        self._store.set_setting("ua.discord.mobile_ratio", f"{value:.3f}")
        self._on_change()
        await self._api.send_message(ctx.chat_id, "Доля мобильных запросов обновлена")

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
        parts = ctx.args.split()
        if len(parts) != 2:
            await self._api.send_message(ctx.chat_id, "Использование: /set_rate <discord|telegram> <в_секунду>")
            return
        target, value_str = parts
        try:
            value = float(value_str)
        except ValueError:
            await self._api.send_message(ctx.chat_id, "Неверное число")
            return
        if target not in {"discord", "telegram"}:
            await self._api.send_message(ctx.chat_id, "Неверная цель")
            return
        self._store.set_setting(f"runtime.{target}_rate", f"{max(0.1, value):.2f}")
        self._on_change()
        await self._api.send_message(ctx.chat_id, "Лимит обновлён")

    # ------------------------------------------------------------------
    # Channel management
    # ------------------------------------------------------------------
    async def cmd_add_channel(self, ctx: CommandContext) -> None:
        parts = ctx.args.split()
        if len(parts) < 2:
            await self._api.send_message(ctx.chat_id, "Использование: /add_channel <discord_id> <telegram_chat> [метка]")
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
            *[f"{record.discord_id} → {record.telegram_chat_id} [{record.label}]" for record in channels],
        ]
        await self._api.send_message(ctx.chat_id, "\n".join(lines))

    async def cmd_set_header(self, ctx: CommandContext) -> None:
        await self._set_format_option(ctx, "header")

    async def cmd_set_footer(self, ctx: CommandContext) -> None:
        await self._set_format_option(ctx, "footer")

    async def cmd_set_chip(self, ctx: CommandContext) -> None:
        await self._set_format_option(ctx, "chip")

    async def cmd_set_parse_mode(self, ctx: CommandContext) -> None:
        await self._set_format_option(ctx, "parse_mode", allowed={"markdownv2", "markdown", "html", "text"})

    async def cmd_set_disable_preview(self, ctx: CommandContext) -> None:
        await self._set_format_option(ctx, "disable_preview", allowed={"on", "off"})

    async def cmd_set_max_length(self, ctx: CommandContext) -> None:
        await self._set_format_option(ctx, "max_length")

    async def cmd_set_attachments(self, ctx: CommandContext) -> None:
        await self._set_format_option(ctx, "attachments_style", allowed={"summary", "links"})

    async def cmd_add_filter(self, ctx: CommandContext) -> None:
        parts = ctx.args.split(maxsplit=2)
        if len(parts) < 3:
            await self._api.send_message(ctx.chat_id, "Использование: /add_filter <discord_id|all> <тип> <значение>")
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
            await self._api.send_message(ctx.chat_id, "Использование: /clear_filter <discord_id|all> <тип> [значение]")
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
            await self._api.send_message(ctx.chat_id, "Использование: /add_replace <discord_id|all> шаблон => замена")
            return
        for channel_id in target:
            self._store.add_replacement(channel_id, pattern, replacement)
        self._on_change()
        await self._api.send_message(ctx.chat_id, "Замена сохранена")

    async def cmd_clear_replace(self, ctx: CommandContext) -> None:
        parts = ctx.args.split(maxsplit=1)
        if not parts:
            await self._api.send_message(ctx.chat_id, "Использование: /clear_replace <discord_id|all> [шаблон]")
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


async def send_formatted(api: TelegramAPI, chat_id: str, message: FormattedTelegramMessage) -> None:
    await api.send_message(chat_id, message.text, parse_mode=message.parse_mode, disable_preview=message.disable_preview)
    for extra in message.extra_messages:
        await api.send_message(chat_id, extra, parse_mode=message.parse_mode, disable_preview=message.disable_preview)

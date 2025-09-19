from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Sequence, Set

import aiohttp

from .config import (
    ChannelMapping,
    MessageCustomization,
    MessageFilters,
    MonitorConfig,
)
from .discord_client import DiscordAPIError, DiscordClient
from .formatter import (
    AttachmentInfo,
    build_attachments,
    clean_discord_content,
    format_announcement_message,
    format_pinned_message,
)
from .state import MonitorState
from .telegram_client import TelegramClient

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ChannelContext:
    """Pre-computed filters and customisation for a Discord channel."""

    mapping: ChannelMapping
    filters: MessageFilters
    customization: MessageCustomization


def _channel_contexts(
    global_filters: MessageFilters,
    global_customization: MessageCustomization,
    channel_mappings: Iterable[ChannelMapping],
) -> List[ChannelContext]:
    contexts: List[ChannelContext] = []
    for mapping in channel_mappings:
        contexts.append(
            ChannelContext(
                mapping=mapping,
                filters=global_filters.combine(mapping.filters),
                customization=global_customization.combine(mapping.customization),
            )
        )
    return contexts


_MIME_PREFIX_CATEGORIES: Dict[str, str] = {
    "image/": "image",
    "video/": "video",
    "audio/": "audio",
}

_MIME_TYPE_CATEGORIES: Dict[str, str] = {
    "application/pdf": "file",
    "text/plain": "file",
}

_EXTENSION_CATEGORIES: Dict[str, str] = {
    "jpg": "image",
    "jpeg": "image",
    "png": "image",
    "gif": "image",
    "bmp": "image",
    "webp": "image",
    "mp4": "video",
    "mov": "video",
    "mkv": "video",
    "webm": "video",
    "mp3": "audio",
    "wav": "audio",
    "ogg": "audio",
    "flac": "audio",
    "m4a": "audio",
    "pdf": "file",
    "txt": "file",
    "doc": "file",
    "docx": "file",
    "xls": "file",
    "xlsx": "file",
    "csv": "file",
    "zip": "file",
}

_TYPE_ALIASES: Dict[str, Set[str]] = {
    "file": {"document"},
}


async def run_monitor(config: MonitorConfig, *, once: bool = False) -> None:
    """Start the monitoring loop."""

    state = MonitorState(config.state_file)

    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        discord = DiscordClient(config.discord_token, session)
        telegram = TelegramClient(config.telegram_token, session)

        while True:
            try:
                announcement_contexts = _channel_contexts(
                    config.filters, config.customization, config.announcement_channels
                )
                await _sync_announcements(
                    announcement_contexts,
                    discord,
                    telegram,
                    state,
                    config.min_message_delay,
                    config.max_message_delay,
                )
                pin_contexts = _channel_contexts(
                    config.filters, config.customization, config.pinned_channels
                )
                await _sync_pins(
                    pin_contexts,
                    discord,
                    telegram,
                    state,
                    config.min_message_delay,
                    config.max_message_delay,
                )
            except Exception:  # pragma: no cover - runtime error path
                LOGGER.exception("Monitoring iteration failed")
            finally:
                state.save()

            if once:
                break

            await asyncio.sleep(config.poll_interval)


async def _sync_announcements(
    contexts: Sequence[ChannelContext],
    discord: DiscordClient,
    telegram: TelegramClient,
    state: MonitorState,
    min_delay: float,
    max_delay: float,
) -> None:
    for context in contexts:
        channel_id = context.mapping.discord_channel_id
        last_seen = state.get_last_message_id(channel_id)
        try:
            messages = await discord.fetch_messages(channel_id, after=last_seen)
        except DiscordAPIError as exc:
            if exc.status == 404:
                LOGGER.error(
                    "Discord channel %s was not found when fetching messages. "
                    "Check the configuration for this channel.",
                    channel_id,
                )
                continue
            raise
        if not messages:
            continue

        for message in messages:
            await _forward_message(
                context=context,
                channel_id=channel_id,
                message=message,
                telegram=telegram,
                formatter=format_announcement_message,
                min_delay=min_delay,
                max_delay=max_delay,
            )
        state.update_last_message_id(channel_id, messages[-1]["id"])


async def _sync_pins(
    contexts: Sequence[ChannelContext],
    discord: DiscordClient,
    telegram: TelegramClient,
    state: MonitorState,
    min_delay: float,
    max_delay: float,
) -> None:
    for context in contexts:
        channel_id = context.mapping.discord_channel_id
        try:
            pins = await discord.fetch_pins(channel_id)
        except DiscordAPIError as exc:
            if exc.status == 404:
                LOGGER.error(
                    "Discord channel %s was not found when fetching pinned messages. "
                    "Check the configuration for this channel.",
                    channel_id,
                )
                continue
            raise
        known_pins = {str(pin_id) for pin_id in state.get_known_pins(channel_id)}
        message_items: List[tuple[str, dict]] = []
        current_pin_ids: Set[str] = set()
        for pin in pins:
            message_id = pin.get("id")
            if message_id is None:
                continue
            message_id_text = str(message_id)
            message_items.append((message_id_text, pin))
            current_pin_ids.add(message_id_text)

        new_pin_ids = current_pin_ids - known_pins
        if new_pin_ids:
            for message_id, message in message_items:
                if message_id in new_pin_ids:
                    await _forward_message(
                        context=context,
                        channel_id=channel_id,
                        message=message,
                        telegram=telegram,
                        formatter=format_pinned_message,
                        min_delay=min_delay,
                        max_delay=max_delay,
                    )

        state.set_known_pins(channel_id, current_pin_ids)


async def _forward_message(
    *,
    context: ChannelContext,
    channel_id: int,
    message: dict,
    telegram: TelegramClient,
    formatter: Callable[[int, dict, str, Sequence[AttachmentInfo]], str],
    min_delay: float,
    max_delay: float,
) -> None:
    clean_content = clean_discord_content(message)
    attachments = build_attachments(message)

    if not _should_forward(message, clean_content, attachments, context.filters):
        return

    customised_content = context.customization.apply(clean_content)
    text = formatter(channel_id, message, customised_content, attachments)
    chat_id = context.mapping.telegram_chat_id
    await telegram.send_message(chat_id, text)
    await _sleep_with_jitter(min_delay, max_delay)
    await _send_attachments(telegram, chat_id, attachments, min_delay, max_delay)


async def _sleep_with_jitter(min_delay: float, max_delay: float) -> None:
    if max_delay <= 0:
        return
    if min_delay > max_delay:
        min_delay, max_delay = max_delay, min_delay
    delay = random.uniform(min_delay, max_delay)
    if delay > 0:
        await asyncio.sleep(delay)


def _should_forward(
    message: dict,
    clean_content: str,
    attachments: Sequence[AttachmentInfo],
    filters: MessageFilters,
) -> bool:
    text_blocks = [clean_content]
    text_blocks.extend(attachment.filename or "" for attachment in attachments)
    aggregate_text = " ".join(block for block in text_blocks if block).casefold()

    if filters.whitelist:
        whitelist = {item.casefold() for item in filters.whitelist}
        if not any(keyword in aggregate_text for keyword in whitelist):
            return False

    if filters.blacklist:
        blacklist = {item.casefold() for item in filters.blacklist}
        if any(keyword in aggregate_text for keyword in blacklist):
            return False

    author = message.get("author") or {}
    author_values = _author_identifiers(author)

    if filters.allowed_senders:
        allowed = {str(item).casefold() for item in filters.allowed_senders}
        if author_values.isdisjoint(allowed):
            return False

    if filters.blocked_senders:
        blocked = {str(item).casefold() for item in filters.blocked_senders}
        if author_values.intersection(blocked):
            return False

    message_types = _message_types(clean_content, attachments)

    if filters.allowed_types:
        allowed_types = {item.casefold() for item in filters.allowed_types}
        if not message_types.intersection(allowed_types):
            return False

    if filters.blocked_types:
        blocked_types = {item.casefold() for item in filters.blocked_types}
        if message_types.intersection(blocked_types):
            return False

    return True


def _author_identifiers(author: dict) -> Set[str]:
    identifiers = set()
    for key in ("id", "username", "global_name"):
        value = author.get(key)
        if value is not None:
            text = str(value).strip()
            if text:
                identifiers.add(text.casefold())
    return identifiers


def _message_types(clean_content: str, attachments: Sequence[AttachmentInfo]) -> Set[str]:
    types: Set[str] = set()
    if clean_content.strip():
        types.add("text")

    if attachments:
        types.add("attachment")
        for attachment in attachments:
            category = _attachment_category(attachment)
            types.add(category)
            types.update(_TYPE_ALIASES.get(category, set()))

    return types


def _attachment_category(attachment: AttachmentInfo) -> str:
    content_type = (attachment.content_type or "").lower()
    if content_type:
        for prefix, category in _MIME_PREFIX_CATEGORIES.items():
            if content_type.startswith(prefix):
                return category
        if content_type in _MIME_TYPE_CATEGORIES:
            return _MIME_TYPE_CATEGORIES[content_type]

    filename = (attachment.filename or "").lower()
    extension = filename.rsplit(".", 1)[-1] if "." in filename else ""
    if extension in _EXTENSION_CATEGORIES:
        return _EXTENSION_CATEGORIES[extension]

    return "other"


async def _send_attachments(
    telegram: TelegramClient,
    chat_id: str,
    attachments: Sequence[AttachmentInfo],
    min_delay: float,
    max_delay: float,
) -> None:
    for attachment in attachments:
        category = _attachment_category(attachment)
        caption = _attachment_caption(attachment)
        try:
            await _send_single_attachment(telegram, chat_id, attachment.url, category, caption)
        finally:
            await _sleep_with_jitter(min_delay, max_delay)


async def _send_single_attachment(
    telegram: TelegramClient,
    chat_id: str,
    url: str,
    category: str,
    caption: str | None,
) -> None:
    if category == "image":
        await telegram.send_photo(chat_id, url, caption=caption)
    elif category == "video":
        await telegram.send_video(chat_id, url, caption=caption)
    elif category == "audio":
        await telegram.send_audio(chat_id, url, caption=caption)
    else:
        await telegram.send_document(chat_id, url, caption=caption)


def _attachment_caption(attachment: AttachmentInfo) -> str | None:
    filename = attachment.filename or ""
    if not filename:
        return None

    if len(filename) <= 1024:
        return filename

    return filename[:1021] + "..."

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

import aiohttp

from .config import (
    ChannelMapping,
    MessageCustomization,
    MessageFilters,
    MonitorConfig,
    PreparedCustomization,
    PreparedFilters,
)
from .discord_client import DiscordAPIError, DiscordClient
from .formatter import (
    AttachmentInfo,
    FormattedMessage,
    build_attachments,
    clean_discord_content,
    extract_embed_text,
    format_announcement_message,
)
from .state import MonitorState
from .telegram_client import TelegramClient

DiscordMessage = dict[str, Any]


class AnnouncementFormatter(Protocol):
    def __call__(
        self,
        channel_id: int,
        message: Mapping[str, Any],
        content: str,
        attachments: Sequence[AttachmentInfo],
        *,
        embed_text: str | None = None,
        channel_label: str | None = None,
    ) -> FormattedMessage:
        """Format a message ready to be sent to Telegram."""


class DiscordService(Protocol):
    async def fetch_messages(
        self,
        channel_id: int,
        *,
        after: str | None = ...,
        limit: int = ...,
    ) -> list[DiscordMessage]:
        """Retrieve recent messages from a Discord channel."""


class TelegramSender(Protocol):
    async def send_message(self, chat_id: str, text: str) -> Any: ...

    async def send_photo(
        self,
        chat_id: str,
        photo: str,
        *,
        caption: str | None = ...,
    ) -> Any: ...

    async def send_video(
        self,
        chat_id: str,
        video: str,
        *,
        caption: str | None = ...,
    ) -> Any: ...

    async def send_audio(
        self,
        chat_id: str,
        audio: str,
        *,
        caption: str | None = ...,
    ) -> Any: ...

    async def send_document(
        self,
        chat_id: str,
        document: str,
        *,
        caption: str | None = ...,
    ) -> Any: ...


class StateStore(Protocol):
    def get_last_message_id(self, channel_id: int) -> str | None: ...

    def update_last_message_id(self, channel_id: int, message_id: str) -> None: ...


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ChannelContext:
    """Pre-computed filters and customisation for a Discord channel."""

    mapping: ChannelMapping
    filters: PreparedFilters
    customization: PreparedCustomization


def _channel_contexts(
    global_filters: MessageFilters,
    global_customization: MessageCustomization,
    channel_mappings: Iterable[ChannelMapping],
) -> list[ChannelContext]:
    contexts: list[ChannelContext] = []
    for mapping in channel_mappings:
        combined_filters = global_filters.combine(mapping.filters).prepare()
        combined_customization = global_customization.combine(mapping.customization).prepare()
        contexts.append(
            ChannelContext(
                mapping=mapping,
                filters=combined_filters,
                customization=combined_customization,
            )
        )
    return contexts


_MIME_PREFIX_CATEGORIES: dict[str, str] = {
    "image/": "image",
    "video/": "video",
    "audio/": "audio",
}

_MIME_TYPE_CATEGORIES: dict[str, str] = {
    "application/pdf": "file",
    "text/plain": "file",
}

_EXTENSION_CATEGORIES: dict[str, str] = {
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

_TYPE_ALIASES: dict[str, set[str]] = {
    "file": {"document"},
}


async def run_monitor(config: MonitorConfig, *, once: bool = False) -> None:
    """Start the monitoring loop."""

    state = MonitorState(config.state_file)

    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        discord = DiscordClient(config.discord_token, session)
        telegram = TelegramClient(config.telegram_token, session)

        announcement_contexts = _channel_contexts(
            config.filters, config.customization, config.announcement_channels
        )

        while True:
            try:
                await _sync_announcements(
                    announcement_contexts,
                    discord,
                    telegram,
                    state,
                    config.min_message_delay,
                    config.max_message_delay,
                )
            except asyncio.CancelledError:
                state.save()
                raise
            except Exception:  # pragma: no cover - runtime error path
                LOGGER.exception("Monitoring iteration failed")
            finally:
                state.save()

            if once:
                break

            await asyncio.sleep(config.poll_interval)


async def _sync_announcements(
    contexts: Sequence[ChannelContext],
    discord: DiscordService,
    telegram: TelegramSender,
    state: StateStore,
    min_delay: float,
    max_delay: float,
) -> None:
    fetch_jobs: list[asyncio.Task[list[DiscordMessage]]] = []
    context_meta: list[tuple[ChannelContext, int]] = []

    for context in contexts:
        channel_id = context.mapping.discord_channel_id
        last_seen = state.get_last_message_id(channel_id)
        fetch_jobs.append(
            asyncio.create_task(_fetch_all_messages(discord, channel_id, last_seen, limit=100))
        )
        context_meta.append((context, channel_id))

    if not fetch_jobs:
        return

    results = await asyncio.gather(*fetch_jobs, return_exceptions=True)

    gathered: list[list[DiscordMessage] | BaseException]
    gathered = list(results)

    for (context, channel_id), result in zip(context_meta, gathered, strict=True):
        if isinstance(result, BaseException):
            if isinstance(result, DiscordAPIError) and result.status == 404:
                LOGGER.error(
                    "Discord channel %s was not found when fetching messages. "
                    "Check the configuration for this channel.",
                    channel_id,
                )
                continue
            raise result

        messages: list[DiscordMessage] = result
        if not messages:
            continue

        for message in messages:
            try:
                await _forward_message(
                    context=context,
                    channel_id=channel_id,
                    message=message,
                    telegram=telegram,
                    formatter=format_announcement_message,
                    min_delay=min_delay,
                    max_delay=max_delay,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception(
                    "Failed to forward message %s from channel %s",
                    message.get("id"),
                    channel_id,
                )
                continue

        state.update_last_message_id(channel_id, messages[-1]["id"])


async def _fetch_all_messages(
    discord: DiscordService,
    channel_id: int,
    last_seen: str | None,
    *,
    limit: int,
) -> list[DiscordMessage]:
    messages: list[DiscordMessage] = []
    cursor = last_seen
    while True:
        batch = await discord.fetch_messages(channel_id, after=cursor, limit=limit)
        if not batch:
            break
        messages.extend(batch)
        if len(batch) < limit:
            break
        cursor = batch[-1]["id"]
    return messages


async def _forward_message(
    *,
    context: ChannelContext,
    channel_id: int,
    message: Mapping[str, Any],
    telegram: TelegramSender,
    formatter: AnnouncementFormatter,
    min_delay: float,
    max_delay: float,
) -> None:
    clean_content = clean_discord_content(message)
    embed_text = extract_embed_text(message)
    combined_content = "\n\n".join(section for section in (clean_content, embed_text) if section)

    attachments = build_attachments(message)
    attachment_categories = tuple(_attachment_category(attachment) for attachment in attachments)

    should_forward, reason = _should_forward(
        message,
        clean_content,
        embed_text,
        attachments,
        attachment_categories,
        context.filters,
    )
    if not should_forward:
        message_id = message.get("id")
        if reason:
            LOGGER.debug(
                "Skipping message %s from channel %s because of %s",
                message_id,
                channel_id,
                reason,
            )
        else:
            LOGGER.debug(
                "Skipping message %s from channel %s due to filters",
                message_id,
                channel_id,
            )
        return

    customised_content = context.customization.apply(combined_content)
    channel_label = context.mapping.display_name
    formatted = formatter(
        channel_id,
        message,
        customised_content,
        attachments,
        embed_text="",
        channel_label=channel_label,
    )
    chat_id = context.mapping.telegram_chat_id
    await telegram.send_message(chat_id, formatted.text)
    await _sleep_with_jitter(min_delay, max_delay)
    for extra_text in formatted.extra_messages:
        if not extra_text:
            continue
        await telegram.send_message(chat_id, extra_text)
        await _sleep_with_jitter(min_delay, max_delay)
    await _send_attachments(
        telegram,
        chat_id,
        attachments,
        attachment_categories,
        min_delay,
        max_delay,
    )


async def _sleep_with_jitter(min_delay: float, max_delay: float) -> None:
    if max_delay <= 0:
        return
    if min_delay > max_delay:
        min_delay, max_delay = max_delay, min_delay
    delay = random.uniform(min_delay, max_delay)
    if delay > 0:
        await asyncio.sleep(delay)


def _should_forward(
    message: Mapping[str, Any],
    clean_content: str,
    embed_text: str,
    attachments: Sequence[AttachmentInfo],
    attachment_categories: Sequence[str],
    filters: PreparedFilters,
) -> tuple[bool, str | None]:
    aggregate_text: str | None = None
    if filters.requires_text:
        aggregate_text = _aggregate_text(clean_content, embed_text, attachments)

    if filters.whitelist:
        assert aggregate_text is not None
        if not any(keyword in aggregate_text for keyword in filters.whitelist):
            return False, "whitelist"

    if filters.blacklist:
        if aggregate_text is None:
            aggregate_text = _aggregate_text(clean_content, embed_text, attachments)
        if any(keyword in aggregate_text for keyword in filters.blacklist):
            return False, "blacklist"

    author = message.get("author") or {}
    author_values = _author_identifiers(author)
    member = message.get("member") or {}
    nickname = member.get("nick")
    if nickname is not None:
        nick_text = str(nickname).strip()
        if nick_text:
            author_values.add(nick_text.casefold())

    if filters.allowed_senders:
        if author_values.isdisjoint(filters.allowed_senders):
            return False, "allowed_senders"

    if filters.blocked_senders:
        if author_values.intersection(filters.blocked_senders):
            return False, "blocked_senders"

    message_types: set[str] | None = None
    if filters.requires_types:
        message_types = _message_types(
            clean_content, embed_text, attachments, attachment_categories
        )

    if filters.allowed_types:
        assert message_types is not None
        if not message_types.intersection(filters.allowed_types):
            return False, "allowed_types"

    if filters.blocked_types:
        if message_types is None:
            message_types = _message_types(
                clean_content, embed_text, attachments, attachment_categories
            )
        if message_types.intersection(filters.blocked_types):
            return False, "blocked_types"

    return True, None


def _author_identifiers(author: Mapping[str, Any]) -> set[str]:
    identifiers: set[str] = set()
    for key in ("id", "username", "global_name"):
        value = author.get(key)
        if value is not None:
            text = str(value).strip()
            if text:
                identifiers.add(text.casefold())
    return identifiers


def _aggregate_text(
    clean_content: str,
    embed_text: str,
    attachments: Sequence[AttachmentInfo],
) -> str:
    text_blocks: list[str] = [clean_content, embed_text]
    for attachment in attachments:
        if attachment.filename:
            text_blocks.append(attachment.filename)
        if attachment.url:
            parsed = urlparse(attachment.url)
            domain = parsed.netloc or parsed.path or attachment.url
            if domain:
                text_blocks.append(domain)
    return " ".join(block for block in text_blocks if block).casefold()


def _message_types(
    clean_content: str,
    embed_text: str,
    attachments: Sequence[AttachmentInfo],
    attachment_categories: Sequence[str],
) -> set[str]:
    types: set[str] = set()
    if clean_content.strip() or embed_text.strip():
        types.add("text")

    if attachments:
        types.add("attachment")
        for category in attachment_categories:
            types.add(category)
            types.update(_TYPE_ALIASES.get(category, set[str]()))

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
    telegram: TelegramSender,
    chat_id: str,
    attachments: Sequence[AttachmentInfo],
    attachment_categories: Sequence[str],
    min_delay: float,
    max_delay: float,
) -> None:
    for attachment, category in zip(attachments, attachment_categories):
        caption = _attachment_caption(attachment)
        try:
            await _send_single_attachment(telegram, chat_id, attachment.url, category, caption)
        finally:
            await _sleep_with_jitter(min_delay, max_delay)


async def _send_single_attachment(
    telegram: TelegramSender,
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

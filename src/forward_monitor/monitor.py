from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Protocol
from urllib.parse import urlparse

import aiohttp

from .config import (
    ChannelMapping,
    CustomisedText,
    FormattingProfile,
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
from .networking import ProxyPool, SoftRateLimiter, UserAgentProvider
from .state import MonitorState
from .structured_logging import log_event
from .telegram_client import TelegramClient
from .types import DiscordMessage

DiscordMessageMapping = DiscordMessage

MODULE_LOGGER = logging.getLogger(__name__)


class AnnouncementFormatter(Protocol):
    def __call__(
        self,
        channel_id: int,
        message: Mapping[str, Any],
        content: CustomisedText,
        attachments: Sequence[AttachmentInfo],
        *,
        embed_text: str | None = None,
        channel_label: str | None = None,
        formatting: FormattingProfile | None = None,
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
    async def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        parse_mode: str | None = ...,
        disable_web_page_preview: bool | None = ...,
    ) -> Any: ...

    async def send_photo(
        self,
        chat_id: str,
        photo: str,
        *,
        caption: str | None = ...,
        parse_mode: str | None = ...,
    ) -> Any: ...

    async def send_video(
        self,
        chat_id: str,
        video: str,
        *,
        caption: str | None = ...,
        parse_mode: str | None = ...,
    ) -> Any: ...

    async def send_audio(
        self,
        chat_id: str,
        audio: str,
        *,
        caption: str | None = ...,
        parse_mode: str | None = ...,
    ) -> Any: ...

    async def send_document(
        self,
        chat_id: str,
        document: str,
        *,
        caption: str | None = ...,
        parse_mode: str | None = ...,
    ) -> Any: ...


class StateStore(Protocol):
    def get_last_message_id(self, channel_id: int) -> str | None: ...

    def update_last_message_id(self, channel_id: int, message_id: str) -> None: ...


_DISCORD_FETCH_LIMIT = 100
_MAX_MESSAGES_PER_CHANNEL = 1000
_MAX_FETCH_SECONDS = 5.0
@dataclass(frozen=True, slots=True)
class ChannelContext:
    """Pre-computed filters and customisation for a Discord channel."""

    mapping: ChannelMapping
    filters: PreparedFilters
    customization: PreparedCustomization
    formatting: FormattingProfile


@dataclass(slots=True)
class _ChannelFetchResult:
    context: ChannelContext
    messages: list[DiscordMessage]
    truncated: bool
    timed_out: bool


def _channel_contexts(
    global_filters: MessageFilters,
    global_customization: MessageCustomization,
    global_formatting: FormattingProfile,
    channel_mappings: Iterable[ChannelMapping],
) -> list[ChannelContext]:
    contexts: list[ChannelContext] = []
    for mapping in channel_mappings:
        combined_filters = global_filters.combine(mapping.filters).prepare()
        combined_customization = (
            global_customization.combine(mapping.customization).prepare()
        )
        combined_formatting = global_formatting.merge(mapping.formatting)
        contexts.append(
            ChannelContext(
                mapping=mapping,
                filters=combined_filters,
                customization=combined_customization,
                formatting=combined_formatting,
            )
        )
    return contexts


async def run_monitor(config: MonitorConfig, *, once: bool = False) -> None:
    """Start the monitoring loop."""

    state = MonitorState(config.runtime.state_file)

    timeout = aiohttp.ClientTimeout(total=60)
    connector = aiohttp.TCPConnector(limit=64, ttl_dns_cache=300)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        discord_rate_limiter = SoftRateLimiter(
            config.discord.rate_limit, name="discord"
        )
        telegram_rate_limiter = SoftRateLimiter(
            config.telegram.rate_limit, name="telegram"
        )
        user_agents = config.network.user_agents
        discord_user_agents = UserAgentProvider(user_agents)
        telegram_user_agents = UserAgentProvider(user_agents)
        discord_proxy = ProxyPool(
            config.network.proxy_for_service("discord"), name="discord"
        )
        telegram_proxy = ProxyPool(
            config.network.proxy_for_service("telegram"), name="telegram"
        )

        discord = DiscordClient(
            config.discord.token,
            session,
            rate_limiter=discord_rate_limiter,
            proxy_pool=discord_proxy,
            user_agents=discord_user_agents,
            token_type=config.discord.token_type,
        )
        telegram = TelegramClient(
            config.telegram.token,
            session,
            rate_limiter=telegram_rate_limiter,
            proxy_pool=telegram_proxy,
            user_agents=telegram_user_agents,
            default_disable_preview=config.telegram.formatting.disable_link_preview,
            default_parse_mode=config.telegram.formatting.parse_mode,
        )

        announcement_contexts = _channel_contexts(
            config.defaults.filters,
            config.defaults.customization,
            config.defaults.formatting,
            config.channels,
        )

        iteration = 0
        while True:
            iteration += 1
            iteration_start = perf_counter()
            try:
                fetched_messages, forwarded_messages = await _sync_announcements(
                    announcement_contexts,
                    discord,
                    telegram,
                    state,
                    config.runtime.min_delay,
                    config.runtime.max_delay,
                )
                log_event(
                    "monitor_iteration_complete",
                    level=logging.INFO,
                    discord_channel_id=None,
                    discord_message_id=None,
                    telegram_chat_id=None,
                    attempt=iteration,
                    outcome="success",
                    latency_ms=(perf_counter() - iteration_start) * 1000,
                    extra={
                        "context_count": len(announcement_contexts),
                        "fetched_messages": fetched_messages,
                        "forwarded_messages": forwarded_messages,
                    },
                )
            except asyncio.CancelledError:
                state.save()
                raise
            except (
                DiscordAPIError,
                RuntimeError,
                aiohttp.ClientError,
                asyncio.TimeoutError,
            ) as exc:
                log_event(
                    "monitor_iteration_failed",
                    level=logging.ERROR,
                    discord_channel_id=None,
                    discord_message_id=None,
                    telegram_chat_id=None,
                    attempt=iteration,
                    outcome="failure",
                    latency_ms=(perf_counter() - iteration_start) * 1000,
                    extra={"error": type(exc).__name__},
                )
            finally:
                state.save()

            if once:
                break

            await asyncio.sleep(config.runtime.poll_interval)


async def _sync_announcements(
    contexts: Sequence[ChannelContext],
    discord: DiscordService,
    telegram: TelegramSender,
    state: StateStore,
    min_delay: float,
    max_delay: float,
) -> tuple[int, int]:
    if not contexts:
        return 0, 0

    tasks: list[asyncio.Task[_ChannelFetchResult]] = []
    total_fetched = 0
    total_forwarded = 0
    async with asyncio.TaskGroup() as group:
        for context in contexts:
            channel_id = context.mapping.discord_channel_id
            last_seen = state.get_last_message_id(channel_id)
            tasks.append(
                group.create_task(
                    _fetch_channel_messages(
                        context,
                        discord,
                        last_seen,
                        limit=_DISCORD_FETCH_LIMIT,
                        max_messages=_MAX_MESSAGES_PER_CHANNEL,
                        max_duration=_MAX_FETCH_SECONDS,
                    )
                )
            )

    for task in tasks:
        result = task.result()
        context = result.context
        channel_id = context.mapping.discord_channel_id
        messages = result.messages
        if not messages:
            continue

        total_fetched += len(messages)
        last_processed_id: str | None = None

        for message in messages:
            message_id = str(message.get("id", "")).strip()
            if not message_id:
                continue
            try:
                forwarded = await _forward_message(
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
            except RuntimeError:
                last_processed_id = message_id
                MODULE_LOGGER.warning(
                    "Failed to forward message %s from channel %s; skipping",
                    message_id,
                    channel_id,
                )
                continue
            else:
                last_processed_id = message_id
                if forwarded:
                    total_forwarded += 1

        if last_processed_id is not None:
            state.update_last_message_id(channel_id, last_processed_id)

    return total_fetched, total_forwarded


async def _fetch_channel_messages(
    context: ChannelContext,
    discord: DiscordService,
    last_seen: str | None,
    *,
    limit: int,
    max_messages: int,
    max_duration: float,
) -> _ChannelFetchResult:
    channel_id = context.mapping.discord_channel_id
    start = perf_counter()
    try:
        messages, truncated, timed_out = await _fetch_all_messages(
            discord,
            channel_id,
            last_seen,
            limit=limit,
            max_messages=max_messages,
            max_duration=max_duration,
        )
    except DiscordAPIError as exc:
        if exc.status == 404:
            log_event(
                "discord_channel_not_found",
                level=logging.ERROR,
                discord_channel_id=channel_id,
                discord_message_id=None,
                telegram_chat_id=context.mapping.telegram_chat_id,
                attempt=1,
                outcome="missing",
                latency_ms=(perf_counter() - start) * 1000,
                extra={},
            )
            return _ChannelFetchResult(context, [], False, False)
        raise

    latency_ms = (perf_counter() - start) * 1000
    log_event(
        "discord_fetch_complete",
        level=logging.DEBUG,
        discord_channel_id=channel_id,
        discord_message_id=None,
        telegram_chat_id=context.mapping.telegram_chat_id,
        attempt=1,
        outcome="success",
        latency_ms=latency_ms,
        extra={
            "message_count": len(messages),
            "truncated": truncated,
            "timed_out": timed_out,
        },
    )
    return _ChannelFetchResult(context, messages, truncated, timed_out)


async def _fetch_all_messages(
    discord: DiscordService,
    channel_id: int,
    last_seen: str | None,
    *,
    limit: int,
    max_messages: int,
    max_duration: float,
) -> tuple[list[DiscordMessage], bool, bool]:
    messages: list[DiscordMessage] = []
    cursor = last_seen
    truncated = False
    timed_out = False
    start = perf_counter()

    while True:
        if len(messages) >= max_messages:
            truncated = True
            break

        if perf_counter() - start >= max_duration:
            timed_out = True
            break

        batch = await discord.fetch_messages(channel_id, after=cursor, limit=limit)
        if not batch:
            break

        messages.extend(batch)
        if len(messages) >= max_messages:
            truncated = True
            messages[:] = messages[:max_messages]
            break

        if len(batch) < limit:
            break

        cursor = batch[-1]["id"]

    return messages, truncated, timed_out


async def _forward_message(
    *,
    context: ChannelContext,
    channel_id: int,
    message: DiscordMessageMapping,
    telegram: TelegramSender,
    formatter: AnnouncementFormatter,
    min_delay: float,
    max_delay: float,
) -> bool:
    start = perf_counter()
    message_id = str(message.get("id", "")) or None
    clean_content = clean_discord_content(message)
    embed_text = extract_embed_text(message)
    attachments = build_attachments(message)
    attachment_categories = tuple(
        _attachment_category(attachment) for attachment in attachments
    )

    should_forward, reason = _should_forward(
        message,
        clean_content,
        embed_text,
        attachments,
        attachment_categories,
        context.filters,
    )
    if not should_forward:
        log_event(
            "message_filtered",
            level=logging.DEBUG,
            discord_channel_id=channel_id,
            discord_message_id=message_id,
            telegram_chat_id=context.mapping.telegram_chat_id,
            attempt=1,
            outcome="skipped",
            latency_ms=(perf_counter() - start) * 1000,
            extra={"reason": reason},
        )
        MODULE_LOGGER.debug(
            "Skipping message %s from channel %s because of %s",
            message_id,
            channel_id,
            reason or "filters",
        )
        return False

    combined_content = "\n\n".join(
        section for section in (clean_content, embed_text) if section
    )
    customised_content = context.customization.render(combined_content)
    channel_label = context.mapping.display_name

    formatted = formatter(
        channel_id,
        message,
        customised_content,
        attachments,
        embed_text="",
        channel_label=channel_label,
        formatting=context.formatting,
    )

    chat_id = context.mapping.telegram_chat_id
    try:
        await telegram.send_message(
            chat_id,
            formatted.text,
            parse_mode=formatted.parse_mode,
            disable_web_page_preview=formatted.disable_preview,
        )
        await _sleep_with_jitter(min_delay, max_delay)
        for extra_text in formatted.extra_messages:
            if not extra_text:
                continue
            await telegram.send_message(
                chat_id,
                extra_text,
                parse_mode=formatted.parse_mode,
                disable_web_page_preview=formatted.disable_preview,
            )
            await _sleep_with_jitter(min_delay, max_delay)
        await _send_attachments(
            telegram,
            chat_id,
            attachments,
            attachment_categories,
            min_delay,
            max_delay,
            parse_mode=formatted.parse_mode,
        )
    except RuntimeError as exc:
        log_event(
            "telegram_forward_failed",
            level=logging.ERROR,
            discord_channel_id=channel_id,
            discord_message_id=message_id,
            telegram_chat_id=chat_id,
            attempt=1,
            outcome="failure",
            latency_ms=(perf_counter() - start) * 1000,
            extra={"error": str(exc)},
        )
        raise

    log_event(
        "message_forwarded",
        level=logging.INFO,
        discord_channel_id=channel_id,
        discord_message_id=message_id,
        telegram_chat_id=chat_id,
        attempt=1,
        outcome="success",
        latency_ms=(perf_counter() - start) * 1000,
        extra={
            "attachment_count": len(attachments),
            "extra_messages": len(formatted.extra_messages),
        },
    )

    return True


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
        domain = _attachment_domain(attachment)
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
    parse_mode: str | None,
) -> None:
    for attachment, category in zip(attachments, attachment_categories):
        caption = _attachment_caption(attachment)
        try:
            await _send_single_attachment(
                telegram, chat_id, attachment.url, category, caption, parse_mode
            )
        finally:
            await _sleep_with_jitter(min_delay, max_delay)


async def _send_single_attachment(
    telegram: TelegramSender,
    chat_id: str,
    url: str,
    category: str,
    caption: str | None,
    parse_mode: str | None,
) -> None:
    if category == "image":
        await telegram.send_photo(chat_id, url, caption=caption, parse_mode=parse_mode)
    elif category == "video":
        await telegram.send_video(chat_id, url, caption=caption, parse_mode=parse_mode)
    elif category == "audio":
        await telegram.send_audio(chat_id, url, caption=caption, parse_mode=parse_mode)
    else:
        await telegram.send_document(chat_id, url, caption=caption, parse_mode=parse_mode)


def _attachment_caption(attachment: AttachmentInfo) -> str | None:
    filename = attachment.filename or ""
    if not filename:
        return None

    if len(filename) <= 1024:
        return filename

    return filename[:1021] + "..."


def _attachment_domain(attachment: AttachmentInfo) -> str | None:
    if attachment.domain:
        return attachment.domain
    if attachment.url:
        parsed = urlparse(attachment.url)
        domain = parsed.netloc or parsed.path
        if domain:
            return domain
    return None

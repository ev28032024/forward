from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Protocol

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
from .structured_logging import log_event
from .telegram_client import TelegramClient
from .types import DiscordMessage

DiscordMessageMapping = DiscordMessage


def _log_startup_user_agent(service: str, provider: UserAgentProvider) -> None:
    user_agent = provider.preview()
    log_event(
        "startup_user_agent",
        level=logging.INFO,
        discord_channel_id=None,
        discord_message_id=None,
        telegram_chat_id=None,
        attempt=None,
        outcome="selected",
        latency_ms=None,
        extra={"service": service, "user_agent": user_agent},
    )


async def _verify_startup_proxies(
    session: aiohttp.ClientSession,
    proxies: Sequence[tuple[str, ProxyPool]],
) -> None:
    for service, proxy_pool in proxies:
        if not proxy_pool.has_proxies():
            continue
        endpoints = tuple(proxy_pool.endpoints())
        if not endpoints:
            continue

        failed: list[str] = []
        for endpoint in endpoints:
            healthy = await proxy_pool.ensure_healthy(endpoint, session)
            if not healthy:
                failed.append(endpoint)

        if failed:
            log_event(
                "proxy_startup_check",
                level=logging.ERROR,
                discord_channel_id=None,
                discord_message_id=None,
                telegram_chat_id=None,
                attempt=None,
                outcome="failure",
                latency_ms=None,
                extra={"service": service, "failed_proxies": failed},
            )
            raise RuntimeError(
                f"Proxy health check failed for {service}: {', '.join(failed)}"
            )

        log_event(
            "proxy_startup_check",
            level=logging.INFO,
            discord_channel_id=None,
            discord_message_id=None,
            telegram_chat_id=None,
            attempt=None,
            outcome="success",
            latency_ms=None,
            extra={"service": service, "proxy_count": len(endpoints)},
        )


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


_DISCORD_FETCH_LIMIT = 100
_DISCORD_CONCURRENCY_LIMIT = 8


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


class _RunState:
    """Tracks which messages have already been forwarded during a run."""

    __slots__ = ("_initialised", "_last_message_ids")

    def __init__(self) -> None:
        self._initialised: set[int] = set()
        self._last_message_ids: dict[int, str] = {}

    def last_seen(self, channel_id: int) -> str | None:
        return self._last_message_ids.get(channel_id)

    def is_initialised(self, channel_id: int) -> bool:
        return channel_id in self._initialised

    def baseline(self, channel_id: int, last_message_id: str | None) -> None:
        self._initialised.add(channel_id)
        if last_message_id:
            normalized = last_message_id.strip()
            if normalized:
                self._last_message_ids[channel_id] = normalized

    def mark_forwarded(self, channel_id: int, message_id: str) -> None:
        normalized = message_id.strip()
        if not normalized:
            return
        self._initialised.add(channel_id)
        self._last_message_ids[channel_id] = normalized


@dataclass(slots=True)
class _FilterContext:
    """Cache expensive filter inputs so `_should_forward` stays linear."""

    message: Mapping[str, Any]
    clean_content: str
    embed_text: str
    attachments: Sequence[AttachmentInfo]
    attachment_categories: Sequence[str]
    _aggregate_text: str | None = None
    _message_types: set[str] | None = None
    _author_values: set[str] | None = None

    def ensure_text(self) -> None:
        self.aggregate_text()

    def aggregate_text(self) -> str:
        if self._aggregate_text is None:
            self._aggregate_text = _aggregate_text(
                self.clean_content, self.embed_text, self.attachments
            )
        return self._aggregate_text

    def text_contains_any(self, keywords: Iterable[str]) -> bool:
        text = self.aggregate_text()
        return any(keyword in text for keyword in keywords)

    def author_values(self) -> set[str]:
        if self._author_values is None:
            author = self.message.get("author") or {}
            values = _author_identifiers(author)
            member = self.message.get("member") or {}
            nickname = member.get("nick")
            if nickname is not None:
                nick_text = str(nickname).strip()
                if nick_text:
                    values.add(nick_text.casefold())
            self._author_values = values
        return self._author_values

    def ensure_types(self) -> None:
        self.message_types()

    def message_types(self) -> set[str]:
        if self._message_types is None:
            self._message_types = _message_types(
                self.clean_content,
                self.embed_text,
                self.attachments,
                self.attachment_categories,
            )
        return self._message_types


@dataclass(slots=True)
class _ForwardingPayload:
    """Prepared Telegram payload so formatting is separated from sending."""

    message_id: str | None
    chat_id: str
    formatted: FormattedMessage
    attachments: Sequence[AttachmentInfo]
    attachment_categories: Sequence[str]


def _channel_contexts(
    global_filters: MessageFilters,
    global_customization: MessageCustomization,
    global_formatting: FormattingProfile,
    channel_mappings: Iterable[ChannelMapping],
) -> list[ChannelContext]:
    contexts: list[ChannelContext] = []
    for mapping in channel_mappings:
        combined_filters = global_filters.combine(mapping.filters).prepare()
        combined_customization = global_customization.combine(mapping.customization).prepare()
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

    run_state = _RunState()

    timeout = aiohttp.ClientTimeout(total=60)
    connector = aiohttp.TCPConnector(limit=64, ttl_dns_cache=300)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        discord_rate_limiter = SoftRateLimiter(config.discord.rate_limit, name="discord")
        discord_api_semaphore = asyncio.Semaphore(_DISCORD_CONCURRENCY_LIMIT)
        telegram_rate_limiter = SoftRateLimiter(config.telegram.rate_limit, name="telegram")
        user_agents = config.network.user_agents
        discord_user_agents = UserAgentProvider(user_agents)
        telegram_user_agents = UserAgentProvider(user_agents)
        _log_startup_user_agent("discord", discord_user_agents)
        _log_startup_user_agent("telegram", telegram_user_agents)
        discord_proxy = ProxyPool(config.network.proxy_for_service("discord"), name="discord")
        telegram_proxy = ProxyPool(config.network.proxy_for_service("telegram"), name="telegram")

        await _verify_startup_proxies(
            session,
            (("discord", discord_proxy), ("telegram", telegram_proxy)),
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
                    run_state,
                    config.runtime.min_delay,
                    config.runtime.max_delay,
                    config.runtime.max_messages_per_channel,
                    config.runtime.max_fetch_seconds,
                    discord_api_semaphore,
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
                raise
            except (
                asyncio.TimeoutError,
                DiscordAPIError,
                RuntimeError,
                aiohttp.ClientError,
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
            if once:
                break

            await asyncio.sleep(config.runtime.poll_interval)


async def _sync_announcements(
    contexts: Sequence[ChannelContext],
    discord: DiscordService,
    telegram: TelegramSender,
    state: _RunState,
    min_delay: float,
    max_delay: float,
    max_messages: int,
    max_fetch_seconds: float,
    api_semaphore: asyncio.Semaphore,
) -> tuple[int, int]:
    if not contexts:
        return 0, 0

    results: dict[int, _ChannelFetchResult] = {}
    total_fetched = 0
    total_forwarded = 0

    async def _fetch_and_store(context: ChannelContext) -> None:
        channel_id = context.mapping.discord_channel_id
        last_seen = state.last_seen(channel_id)
        result = await _fetch_channel_messages(
            context,
            discord,
            last_seen,
            limit=_DISCORD_FETCH_LIMIT,
            max_messages=max_messages,
            max_duration_seconds=max_fetch_seconds,
            api_semaphore=api_semaphore,
        )
        results[channel_id] = result

    async with asyncio.TaskGroup() as group:
        for context in contexts:
            group.create_task(_fetch_and_store(context))

    for context in contexts:
        channel_id = context.mapping.discord_channel_id
        result = results.get(channel_id)
        if result is None:
            continue
        messages = result.messages

        if not state.is_initialised(channel_id):
            baseline_id: str | None = None
            if messages:
                last_message = messages[-1]
                baseline_id = str(last_message.get("id", "")).strip() or None
            state.baseline(channel_id, baseline_id)
            continue

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
            except RuntimeError as exc:
                last_processed_id = message_id
                log_event(
                    "telegram_forward_failed",
                    level=logging.WARNING,
                    discord_channel_id=channel_id,
                    discord_message_id=message_id,
                    telegram_chat_id=context.mapping.telegram_chat_id,
                    attempt=1,
                    outcome="failure",
                    latency_ms=None,
                    extra={"error": type(exc).__name__},
                )
                continue
            else:
                last_processed_id = message_id
                if forwarded:
                    total_forwarded += 1

        if last_processed_id is not None:
            state.mark_forwarded(channel_id, last_processed_id)

    return total_fetched, total_forwarded


async def _fetch_channel_messages(
    context: ChannelContext,
    discord: DiscordService,
    last_seen: str | None,
    *,
    limit: int,
    max_messages: int,
    max_duration_seconds: float,
    api_semaphore: asyncio.Semaphore,
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
            max_duration_seconds=max_duration_seconds,
            api_semaphore=api_semaphore,
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
    max_duration_seconds: float,
    api_semaphore: asyncio.Semaphore,
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

        if perf_counter() - start >= max_duration_seconds:
            timed_out = True
            break

        async with api_semaphore:
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
    payload, reason, message_id = _prepare_forward_payload(
        context=context,
        message=message,
        formatter=formatter,
    )

    if payload is None:
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
        return False

    try:
        await _dispatch_forwarding(
            telegram=telegram,
            payload=payload,
            min_delay=min_delay,
            max_delay=max_delay,
        )
    except RuntimeError as exc:
        log_event(
            "telegram_forward_failed",
            level=logging.ERROR,
            discord_channel_id=channel_id,
            discord_message_id=message_id,
            telegram_chat_id=payload.chat_id,
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
        telegram_chat_id=payload.chat_id,
        attempt=1,
        outcome="success",
        latency_ms=(perf_counter() - start) * 1000,
        extra={
            "attachment_count": len(payload.attachments),
            "extra_messages": len(payload.formatted.extra_messages),
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
    context = _FilterContext(
        message=message,
        clean_content=clean_content,
        embed_text=embed_text,
        attachments=attachments,
        attachment_categories=attachment_categories,
    )

    reason = (
        _check_text_filters(filters, context)
        or _check_sender_filters(filters, context)
        or _check_type_filters(filters, context)
    )

    if reason is not None:
        return False, reason

    return True, None


def _check_text_filters(filters: PreparedFilters, context: _FilterContext) -> str | None:
    if filters.requires_text:
        context.ensure_text()

    if filters.whitelist and not context.text_contains_any(filters.whitelist):
        return "whitelist"

    if filters.blacklist and context.text_contains_any(filters.blacklist):
        return "blacklist"

    return None


def _check_sender_filters(filters: PreparedFilters, context: _FilterContext) -> str | None:
    if not (filters.allowed_senders or filters.blocked_senders):
        return None

    author_values = context.author_values()

    if filters.allowed_senders and author_values.isdisjoint(filters.allowed_senders):
        return "allowed_senders"

    if filters.blocked_senders and author_values.intersection(filters.blocked_senders):
        return "blocked_senders"

    return None


def _check_type_filters(filters: PreparedFilters, context: _FilterContext) -> str | None:
    if filters.requires_types:
        context.ensure_types()

    if filters.allowed_types:
        if not filters.requires_types:
            raise AssertionError
        if not context.message_types().intersection(filters.allowed_types):
            return "allowed_types"

    if filters.blocked_types and context.message_types().intersection(filters.blocked_types):
        return "blocked_types"

    return None


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
        domain = _attachment_domain_text(attachment)
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


def _attachment_domain_text(attachment: AttachmentInfo) -> str | None:
    return attachment.domain or attachment.url


async def _dispatch_forwarding(
    *,
    telegram: TelegramSender,
    payload: _ForwardingPayload,
    min_delay: float,
    max_delay: float,
) -> None:
    formatted = payload.formatted
    await telegram.send_message(
        payload.chat_id,
        formatted.text,
        parse_mode=formatted.parse_mode,
        disable_web_page_preview=formatted.disable_preview,
    )
    await _sleep_with_jitter(min_delay, max_delay)

    for extra_text in formatted.extra_messages:
        if not extra_text:
            continue
        await telegram.send_message(
            payload.chat_id,
            extra_text,
            parse_mode=formatted.parse_mode,
            disable_web_page_preview=formatted.disable_preview,
        )
        await _sleep_with_jitter(min_delay, max_delay)

    await _send_attachments(
        telegram,
        payload.chat_id,
        payload.attachments,
        payload.attachment_categories,
        min_delay,
        max_delay,
        parse_mode=formatted.parse_mode,
    )


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


def _prepare_forward_payload(
    *,
    context: ChannelContext,
    message: DiscordMessageMapping,
    formatter: AnnouncementFormatter,
) -> tuple[_ForwardingPayload | None, str | None, str | None]:
    message_id = str(message.get("id", "")).strip() or None
    clean_content = clean_discord_content(message)
    embed_text = extract_embed_text(message)
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
        return None, reason, message_id

    combined_content = "\n\n".join(section for section in (clean_content, embed_text) if section)
    customised_content = context.customization.render(combined_content)
    channel_label = context.mapping.display_name

    formatted = formatter(
        context.mapping.discord_channel_id,
        message,
        customised_content,
        attachments,
        embed_text="",
        channel_label=channel_label,
        formatting=context.formatting,
    )

    payload = _ForwardingPayload(
        message_id=message_id,
        chat_id=context.mapping.telegram_chat_id,
        formatted=formatted,
        attachments=attachments,
        attachment_categories=attachment_categories,
    )
    return payload, None, message_id

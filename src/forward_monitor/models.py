"""Data models used across the forwarding service."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence


@dataclass(slots=True)
class ReplacementRule:
    """Simple find/replace transformation."""

    pattern: str
    replacement: str


@dataclass(slots=True)
class FormattingOptions:
    """Configuration affecting Telegram output."""

    parse_mode: str = "MarkdownV2"
    disable_preview: bool = True
    max_length: int = 3500
    ellipsis: str = "…"
    attachments_style: str = "summary"
    header: str = ""
    footer: str = ""
    chip: str = ""


@dataclass(slots=True)
class FilterConfig:
    """Allow and deny lists for Discord content."""

    whitelist: set[str] = field(default_factory=set)
    blacklist: set[str] = field(default_factory=set)
    allowed_senders: set[str] = field(default_factory=set)
    blocked_senders: set[str] = field(default_factory=set)
    allowed_types: set[str] = field(default_factory=set)
    blocked_types: set[str] = field(default_factory=set)

    def merge(self, other: "FilterConfig") -> "FilterConfig":
        return FilterConfig(
            whitelist=self.whitelist | other.whitelist,
            blacklist=self.blacklist | other.blacklist,
            allowed_senders=self.allowed_senders | other.allowed_senders,
            blocked_senders=self.blocked_senders | other.blocked_senders,
            allowed_types=self.allowed_types | other.allowed_types,
            blocked_types=self.blocked_types | other.blocked_types,
        )


@dataclass(slots=True)
class ChannelConfig:
    """Runtime representation of a Discord → Telegram mapping."""

    discord_id: str
    telegram_chat_id: str
    label: str
    formatting: FormattingOptions
    filters: FilterConfig
    replacements: Sequence[ReplacementRule]
    last_message_id: str | None
    active: bool = True
    storage_id: int | None = None

    def with_updates(
        self,
        *,
        formatting: FormattingOptions | None = None,
        filters: FilterConfig | None = None,
        replacements: Iterable[ReplacementRule] | None = None,
        last_message_id: str | None = None,
    ) -> "ChannelConfig":
        return ChannelConfig(
            discord_id=self.discord_id,
            telegram_chat_id=self.telegram_chat_id,
            label=self.label,
            formatting=formatting or self.formatting,
            filters=filters or self.filters,
            replacements=tuple(replacements or self.replacements),
            last_message_id=(
                last_message_id if last_message_id is not None else self.last_message_id
            ),
            active=self.active,
            storage_id=self.storage_id,
        )


@dataclass(slots=True)
class RuntimeOptions:
    """Tunable behaviour of the monitor loop."""

    poll_interval: float = 2.0
    min_delay_ms: int = 0
    max_delay_ms: int = 0
    rate_per_second: float = 8.0


@dataclass(slots=True)
class NetworkOptions:
    """Proxy and client identity overrides."""

    discord_proxy_url: str | None = None
    discord_proxy_login: str | None = None
    discord_proxy_password: str | None = None
    discord_user_agent: str | None = None


@dataclass(slots=True)
class DiscordMessage:
    """Subset of the Discord payload used by the forwarder."""

    id: str
    channel_id: str
    author_id: str
    author_name: str
    content: str
    attachments: Sequence[Mapping[str, Any]]
    embeds: Sequence[Mapping[str, Any]]
    timestamp: str | None = None
    edited_timestamp: str | None = None


@dataclass(slots=True)
class FormattedTelegramMessage:
    """Outgoing Telegram payload produced by the formatter."""

    text: str
    extra_messages: Sequence[str]
    parse_mode: str | None
    disable_preview: bool

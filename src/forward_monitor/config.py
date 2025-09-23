from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, MutableMapping, cast

from yaml import safe_load

TokenType = Literal["auto", "bot", "user", "bearer"]

DEFAULT_POLL_INTERVAL: Final[int] = 300
DEFAULT_STATE_FILE: Final[Path] = Path("monitor_state.json")
DEFAULT_MIN_MESSAGE_DELAY: Final[float] = 0.5
DEFAULT_MAX_MESSAGE_DELAY: Final[float] = 2.0

_DEFAULT_TELEGRAM_MESSAGE_LIMIT: Final[int] = 3500
_TELEGRAM_MAX_LIMIT: Final[int] = 4096

_DEFAULT_DESKTOP_AGENTS: Final[tuple[str, ...]] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_4) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/16.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:123.0) Gecko/20100101 Firefox/123.0",
)

_DEFAULT_MOBILE_AGENTS: Final[tuple[str, ...]] = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Mobile Safari/537.36",
)

_VALID_MESSAGE_TYPES: Final[frozenset[str]] = frozenset(
    {
        "text",
        "attachment",
        "image",
        "video",
        "audio",
        "file",
        "document",
        "other",
    }
)

__all__ = [
    "DEFAULT_POLL_INTERVAL",
    "DEFAULT_STATE_FILE",
    "DEFAULT_MIN_MESSAGE_DELAY",
    "DEFAULT_MAX_MESSAGE_DELAY",
    "TokenType",
    "MessageFilters",
    "PreparedFilters",
    "MessageCustomization",
    "PreparedCustomization",
    "CustomisedText",
    "FormattingProfile",
    "RateLimitSettings",
    "ProxyPoolSettings",
    "UserAgentSettings",
    "ChannelDefaults",
    "ChannelMapping",
    "MonitorRuntime",
    "DiscordSettings",
    "TelegramSettings",
    "NetworkSettings",
    "MonitorConfig",
]


@dataclass(frozen=True, slots=True)
class RateLimitSettings:
    """Soft rate-limit policy used when communicating with external services."""

    per_second: float | None = None
    per_minute: int | None = 60
    concurrency: int = 4
    jitter_min_ms: int = 40
    jitter_max_ms: int = 160
    cooldown_seconds: float = 30.0

    def merge(self, other: RateLimitSettings | None) -> RateLimitSettings:
        if other is None:
            return self
        return RateLimitSettings(
            per_second=_first_non_null(other.per_second, self.per_second),
            per_minute=_first_non_null(other.per_minute, self.per_minute),
            concurrency=max(1, _first_non_null(other.concurrency, self.concurrency)),
            jitter_min_ms=max(0, _first_non_null(other.jitter_min_ms, self.jitter_min_ms)),
            jitter_max_ms=max(
                _first_non_null(other.jitter_max_ms, self.jitter_max_ms),
                _first_non_null(other.jitter_min_ms, self.jitter_min_ms),
            ),
            cooldown_seconds=max(
                0.0,
                float(
                    _first_non_null(
                        other.cooldown_seconds,
                        self.cooldown_seconds,
                    )
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class ProxyPoolSettings:
    """Definition of an outbound proxy pool."""

    endpoints: tuple[str, ...] = ()
    health_check_url: str | None = None
    health_check_timeout: float = 5.0
    recovery_seconds: float = 180.0

    def merge(self, other: ProxyPoolSettings | None) -> ProxyPoolSettings:
        if other is None:
            return self
        endpoints = _merge_sequences(self.endpoints, other.endpoints)
        health_check_url = other.health_check_url or self.health_check_url
        timeout = float(
            _first_non_null(other.health_check_timeout, self.health_check_timeout)
        )
        recovery = float(_first_non_null(other.recovery_seconds, self.recovery_seconds))
        return ProxyPoolSettings(
            endpoints=endpoints,
            health_check_url=health_check_url,
            health_check_timeout=max(timeout, 0.1),
            recovery_seconds=max(recovery, 1.0),
        )

    def normalised(self) -> ProxyPoolSettings:
        cleaned = tuple({endpoint.strip(): None for endpoint in self.endpoints if endpoint}.keys())
        return ProxyPoolSettings(
            endpoints=cleaned,
            health_check_url=(self.health_check_url or "").strip() or None,
            health_check_timeout=self.health_check_timeout,
            recovery_seconds=self.recovery_seconds,
        )


@dataclass(frozen=True, slots=True)
class UserAgentSettings:
    """Pools of desktop and mobile user-agent strings."""

    desktop: tuple[str, ...] = _DEFAULT_DESKTOP_AGENTS
    mobile: tuple[str, ...] = _DEFAULT_MOBILE_AGENTS
    mobile_ratio: float = 0.35

    def normalised(self) -> UserAgentSettings:
        mobile_ratio = min(max(float(self.mobile_ratio), 0.0), 1.0)
        desktop = _deduplicate(self.desktop, fallback=_DEFAULT_DESKTOP_AGENTS)
        mobile = _deduplicate(self.mobile, fallback=_DEFAULT_MOBILE_AGENTS)
        return UserAgentSettings(desktop=desktop, mobile=mobile, mobile_ratio=mobile_ratio)


@dataclass(frozen=True, slots=True)
class FormattingProfile:
    """Controls Telegram specific formatting options."""

    parse_mode: str = "HTML"
    disable_link_preview: bool = True
    max_length: int = _DEFAULT_TELEGRAM_MESSAGE_LIMIT
    ellipsis: str = "…"
    attachments_style: Literal["compact", "minimal"] = "minimal"
    provided: frozenset[str] = field(
        default_factory=frozenset, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if self.provided:
            return
        explicit: set[str] = set()
        if self.parse_mode != "HTML":
            explicit.add("parse_mode")
        if self.disable_link_preview is not True:
            explicit.add("disable_link_preview")
        if self.max_length != _DEFAULT_TELEGRAM_MESSAGE_LIMIT:
            explicit.add("max_length")
        if self.ellipsis != "…":
            explicit.add("ellipsis")
        if self.attachments_style != "minimal":
            explicit.add("attachments_style")
        if explicit:
            object.__setattr__(self, "provided", frozenset(explicit))

    def merge(self, other: FormattingProfile | None) -> FormattingProfile:
        if other is None:
            return self
        values = {
            "parse_mode": self.parse_mode,
            "disable_link_preview": self.disable_link_preview,
            "max_length": self.max_length,
            "ellipsis": self.ellipsis,
            "attachments_style": self.attachments_style,
        }
        explicit = set(self.provided)

        if "parse_mode" in other.provided:
            values["parse_mode"] = (other.parse_mode or self.parse_mode or "").strip() or "HTML"
            explicit.add("parse_mode")
        if "disable_link_preview" in other.provided:
            disable = bool(
                _first_non_null(other.disable_link_preview, self.disable_link_preview)
            )
            values["disable_link_preview"] = disable
            explicit.add("disable_link_preview")
        if "max_length" in other.provided:
            max_length = max(
                100,
                min(
                    int(_first_non_null(other.max_length, self.max_length)),
                    _TELEGRAM_MAX_LIMIT,
                ),
            )
            values["max_length"] = max_length
            explicit.add("max_length")
        if "ellipsis" in other.provided:
            ellipsis = (other.ellipsis or self.ellipsis or "").strip() or "…"
            values["ellipsis"] = ellipsis
            explicit.add("ellipsis")
        if "attachments_style" in other.provided:
            values["attachments_style"] = other.attachments_style or self.attachments_style
            explicit.add("attachments_style")

        return FormattingProfile(**values, provided=frozenset(explicit))


@dataclass(frozen=True, slots=True)
class MessageFilters:
    """Keyword, sender and attachment filters applied to forwarded messages."""

    whitelist: Sequence[str] = ()
    blacklist: Sequence[str] = ()
    allowed_senders: Sequence[str] = ()
    blocked_senders: Sequence[str] = ()
    allowed_types: Sequence[str] = ()
    blocked_types: Sequence[str] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "whitelist", _freeze_sequence(self.whitelist))
        object.__setattr__(self, "blacklist", _freeze_sequence(self.blacklist))
        object.__setattr__(self, "allowed_senders", _freeze_sequence(self.allowed_senders))
        object.__setattr__(self, "blocked_senders", _freeze_sequence(self.blocked_senders))
        object.__setattr__(self, "allowed_types", _freeze_sequence(self.allowed_types))
        object.__setattr__(self, "blocked_types", _freeze_sequence(self.blocked_types))

    def combine(self, other: MessageFilters | None) -> MessageFilters:
        if other is None:
            return MessageFilters(
                whitelist=self.whitelist,
                blacklist=self.blacklist,
                allowed_senders=self.allowed_senders,
                blocked_senders=self.blocked_senders,
                allowed_types=self.allowed_types,
                blocked_types=self.blocked_types,
            )
        return MessageFilters(
            whitelist=_merge_sequences(self.whitelist, other.whitelist),
            blacklist=_merge_sequences(self.blacklist, other.blacklist),
            allowed_senders=_merge_sequences(self.allowed_senders, other.allowed_senders),
            blocked_senders=_merge_sequences(self.blocked_senders, other.blocked_senders),
            allowed_types=_merge_sequences(self.allowed_types, other.allowed_types),
            blocked_types=_merge_sequences(self.blocked_types, other.blocked_types),
        )

    def prepare(self) -> PreparedFilters:
        whitelist = tuple(_casefold_unique(self.whitelist))
        blacklist = tuple(_casefold_unique(self.blacklist))
        allowed_senders = frozenset(_casefold_unique(self.allowed_senders))
        blocked_senders = frozenset(_casefold_unique(self.blocked_senders))
        allowed_types = frozenset(_casefold_unique(self.allowed_types))
        blocked_types = frozenset(_casefold_unique(self.blocked_types))
        return PreparedFilters(
            whitelist=whitelist,
            blacklist=blacklist,
            allowed_senders=allowed_senders,
            blocked_senders=blocked_senders,
            allowed_types=allowed_types,
            blocked_types=blocked_types,
            requires_text=bool(whitelist or blacklist),
            requires_types=bool(allowed_types or blocked_types),
        )


@dataclass(frozen=True, slots=True)
class PreparedFilters:
    whitelist: tuple[str, ...]
    blacklist: tuple[str, ...]
    allowed_senders: frozenset[str]
    blocked_senders: frozenset[str]
    allowed_types: frozenset[str]
    blocked_types: frozenset[str]
    requires_text: bool
    requires_types: bool


@dataclass(frozen=True, slots=True)
class MessageCustomization:
    """Textual customisation applied before Telegram formatting."""

    chips: Sequence[str] = ()
    headers: Sequence[str] = ()
    footers: Sequence[str] = ()
    replacements: Sequence[tuple[str, str]] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "chips", _freeze_sequence(self.chips))
        object.__setattr__(self, "headers", _freeze_sequence(self.headers))
        object.__setattr__(self, "footers", _freeze_sequence(self.footers))
        object.__setattr__(self, "replacements", _freeze_replacements(self.replacements))

    def combine(self, other: MessageCustomization | None) -> MessageCustomization:
        if other is None:
            return MessageCustomization(
                chips=self.chips,
                headers=self.headers,
                footers=self.footers,
                replacements=self.replacements,
            )
        merged_replacements: MutableMapping[str, str] = dict(self.replacements)
        merged_replacements.update(dict(other.replacements))
        return MessageCustomization(
            chips=_merge_sequences(self.chips, other.chips),
            headers=_merge_sequences(self.headers, other.headers),
            footers=_merge_sequences(self.footers, other.footers),
            replacements=tuple(merged_replacements.items()),
        )

    def prepare(self) -> PreparedCustomization:
        chips = tuple(_unique_non_empty(self.chips))
        headers = tuple(_unique_non_empty(self.headers))
        footers = tuple(_unique_non_empty(self.footers))
        replacements = tuple((find, replace) for find, replace in self.replacements if find)
        return PreparedCustomization(
            chips=chips,
            headers=headers,
            footers=footers,
            replacements=replacements,
        )


@dataclass(frozen=True, slots=True)
class CustomisedText:
    chips: tuple[str, ...]
    header_lines: tuple[str, ...]
    body_lines: tuple[str, ...]
    footer_lines: tuple[str, ...]

    def as_string(self) -> str:
        segments: list[str] = []
        if self.header_lines:
            segments.append("\n".join(self.header_lines))
        if self.body_lines:
            segments.append("\n".join(self.body_lines))
        if self.footer_lines:
            segments.append("\n".join(self.footer_lines))
        return "\n\n".join(segment for segment in segments if segment)


@dataclass(frozen=True, slots=True)
class PreparedCustomization:
    chips: tuple[str, ...]
    headers: tuple[str, ...]
    footers: tuple[str, ...]
    replacements: tuple[tuple[str, str], ...]

    def render(self, content: str) -> CustomisedText:
        text = content or ""
        for find, replace in self.replacements:
            text = text.replace(find, replace)
        body_lines = _normalise_multiline(text)
        return CustomisedText(
            chips=self.chips,
            header_lines=self.headers,
            body_lines=body_lines,
            footer_lines=self.footers,
        )


@dataclass(frozen=True, slots=True)
class ChannelDefaults:
    filters: MessageFilters = field(default_factory=MessageFilters)
    customization: MessageCustomization = field(default_factory=MessageCustomization)
    formatting: FormattingProfile = field(default_factory=FormattingProfile)


@dataclass(frozen=True, slots=True)
class ChannelMapping:
    discord_channel_id: int
    telegram_chat_id: str
    display_name: str | None = None
    filters: MessageFilters = field(default_factory=MessageFilters)
    customization: MessageCustomization = field(default_factory=MessageCustomization)
    formatting: FormattingProfile = field(default_factory=FormattingProfile)

    def __post_init__(self) -> None:
        object.__setattr__(self, "telegram_chat_id", self.telegram_chat_id.strip())
        if self.display_name is not None:
            object.__setattr__(self, "display_name", self.display_name.strip() or None)


@dataclass(frozen=True, slots=True)
class MonitorRuntime:
    poll_interval: int = DEFAULT_POLL_INTERVAL
    state_file: Path = DEFAULT_STATE_FILE
    min_delay: float = DEFAULT_MIN_MESSAGE_DELAY
    max_delay: float = DEFAULT_MAX_MESSAGE_DELAY


@dataclass(frozen=True, slots=True)
class DiscordSettings:
    token: str
    token_type: TokenType = "auto"
    rate_limit: RateLimitSettings = field(default_factory=RateLimitSettings)


@dataclass(frozen=True, slots=True)
class TelegramSettings:
    token: str
    default_chat: str
    rate_limit: RateLimitSettings = field(default_factory=lambda: RateLimitSettings(per_second=0.9, per_minute=25, concurrency=1))
    formatting: FormattingProfile = field(default_factory=FormattingProfile)


@dataclass(frozen=True, slots=True)
class NetworkSettings:
    user_agents: UserAgentSettings = field(default_factory=UserAgentSettings)
    default_proxy: ProxyPoolSettings = field(default_factory=ProxyPoolSettings)
    discord_proxy: ProxyPoolSettings = field(default_factory=ProxyPoolSettings)
    telegram_proxy: ProxyPoolSettings = field(default_factory=ProxyPoolSettings)

    def proxy_for_service(self, service: str) -> ProxyPoolSettings:
        base = self.default_proxy.normalised()
        if service == "discord":
            return base.merge(self.discord_proxy).normalised()
        if service == "telegram":
            return base.merge(self.telegram_proxy).normalised()
        return base


@dataclass(frozen=True, slots=True)
class MonitorConfig:
    discord: DiscordSettings
    telegram: TelegramSettings
    runtime: MonitorRuntime
    defaults: ChannelDefaults
    channels: tuple[ChannelMapping, ...]
    network: NetworkSettings

    @classmethod
    def from_file(cls, path: Path) -> MonitorConfig:
        raw = _load_yaml(path)
        path = path.expanduser().resolve()

        discord_section = _expect_mapping(raw.get("discord"), "discord")
        telegram_section = _expect_mapping(raw.get("telegram"), "telegram")
        forward_section = _expect_mapping(raw.get("forward"), "forward", default={})
        runtime_section = _expect_mapping(raw.get("runtime"), "runtime", default={})
        network_section = _expect_mapping(raw.get("network"), "network", default={})

        discord = _parse_discord_settings(discord_section)
        telegram = _parse_telegram_settings(telegram_section)
        defaults = _parse_defaults(forward_section.get("defaults"))
        merged_formatting = telegram.formatting.merge(defaults.formatting)
        defaults = ChannelDefaults(
            filters=defaults.filters,
            customization=defaults.customization,
            formatting=merged_formatting,
        )

        channels = tuple(
            _parse_channel_mapping(entry, telegram.default_chat, defaults)
            for entry in _expect_sequence(forward_section.get("channels"), "forward.channels")
        )

        runtime = _parse_runtime(runtime_section, path)
        network = _parse_network_settings(network_section)

        return cls(
            discord=discord,
            telegram=telegram,
            runtime=runtime,
            defaults=defaults,
            channels=channels,
            network=network,
        )


def _parse_discord_settings(raw: Mapping[str, Any]) -> DiscordSettings:
    token = _require_string(raw, "discord.token", raw.get("token"))
    token_type = _normalize_token_type(raw.get("token_type"))
    rate_limit = _parse_rate_limit(raw.get("rate_limit"), fallback=RateLimitSettings(per_second=4.0, per_minute=60, concurrency=4))
    return DiscordSettings(token=token, token_type=token_type, rate_limit=rate_limit)


def _parse_telegram_settings(raw: Mapping[str, Any]) -> TelegramSettings:
    token = _require_string(raw, "telegram.token", raw.get("token"))
    chat = _require_string(raw, "telegram.chat", raw.get("chat") or raw.get("chat_id"))
    base_formatting = _parse_formatting(raw.get("formatting"))
    rate_limit = _parse_rate_limit(raw.get("rate_limit"), fallback=RateLimitSettings(per_second=0.8, per_minute=25, concurrency=1, jitter_min_ms=60, jitter_max_ms=200))
    formatting = FormattingProfile().merge(base_formatting)
    return TelegramSettings(token=token, default_chat=chat, rate_limit=rate_limit, formatting=formatting)


def _parse_defaults(raw: Any) -> ChannelDefaults:
    if raw is None:
        return ChannelDefaults()
    if not isinstance(raw, Mapping):
        raise ValueError("Configuration field 'forward.defaults' must be a mapping")
    filters = _parse_filters(raw.get("filters"), context="forward.defaults.filters")
    customization = _parse_customization(raw.get("text") or raw.get("customization"))
    formatting = _parse_formatting(raw.get("formatting"))
    return ChannelDefaults(
        filters=filters,
        customization=customization,
        formatting=FormattingProfile().merge(formatting),
    )


def _parse_channel_mapping(
    raw: Any,
    default_chat: str,
    defaults: ChannelDefaults,
) -> ChannelMapping:
    if not isinstance(raw, Mapping):
        raise ValueError("Each entry in 'forward.channels' must be a mapping")
    discord_id = _coerce_channel_id(raw.get("discord"), "forward.channels.discord")
    telegram_chat_raw = raw.get("telegram") or raw.get("chat") or default_chat
    telegram_chat = _require_string(raw, "forward.channels.telegram", telegram_chat_raw)
    display_name = _optional_string(raw.get("name") or raw.get("label"))
    filter_context = f"forward.channels[{discord_id}].filters"
    filters = defaults.filters.combine(
        _parse_filters(raw.get("filters"), context=filter_context)
    )
    customization = defaults.customization.combine(
        _parse_customization(raw.get("text") or raw.get("customization"))
    )
    formatting = defaults.formatting.merge(_parse_formatting(raw.get("formatting")))
    return ChannelMapping(
        discord_channel_id=discord_id,
        telegram_chat_id=telegram_chat,
        display_name=display_name,
        filters=filters,
        customization=customization,
        formatting=formatting,
    )


def _parse_runtime(raw: Mapping[str, Any], config_path: Path) -> MonitorRuntime:
    poll_interval = int(_first_non_null(raw.get("poll_every"), DEFAULT_POLL_INTERVAL))
    if poll_interval < 0:
        raise ValueError("Configuration field 'runtime.poll_every' cannot be negative")

    delays_raw = raw.get("delays")
    if delays_raw is None:
        delays_map: Mapping[str, Any] = {}
    elif isinstance(delays_raw, Mapping):
        delays_map = delays_raw
    else:
        raise ValueError("Configuration field 'runtime.delays' must be a mapping if provided")

    min_delay = float(_first_non_null(delays_map.get("min"), DEFAULT_MIN_MESSAGE_DELAY))
    max_delay = float(_first_non_null(delays_map.get("max"), DEFAULT_MAX_MESSAGE_DELAY))
    if min_delay < 0:
        raise ValueError("Configuration field 'runtime.delays.min' cannot be negative")
    if max_delay < min_delay:
        raise ValueError("Configuration field 'runtime.delays.max' must be greater than or equal to runtime.delays.min")
    state_raw = raw.get("state_file")
    state_file = _resolve_state_file(config_path, state_raw, DEFAULT_STATE_FILE)
    return MonitorRuntime(
        poll_interval=poll_interval,
        state_file=state_file,
        min_delay=min_delay,
        max_delay=max_delay,
    )


def _parse_network_settings(raw: Mapping[str, Any]) -> NetworkSettings:
    proxies = raw.get("proxies")
    if proxies is None:
        proxies_mapping: Mapping[str, Any] = {}
    elif isinstance(proxies, Mapping):
        proxies_mapping = proxies
    else:
        raise ValueError("Configuration field 'network.proxies' must be a mapping")

    default_proxy = _parse_proxy_pool(proxies_mapping.get("pool"))
    discord_proxy = _parse_proxy_pool(proxies_mapping.get("discord"))
    telegram_proxy = _parse_proxy_pool(proxies_mapping.get("telegram"))

    if isinstance(proxies_mapping.get("healthcheck"), Mapping):
        health_map = cast(Mapping[str, Any], proxies_mapping.get("healthcheck"))
        base = ProxyPoolSettings(
            health_check_url=_optional_string(health_map.get("url")),
            health_check_timeout=float(health_map.get("timeout", 5.0)),
            recovery_seconds=float(health_map.get("cooldown", 180.0)),
        )
        default_proxy = default_proxy.merge(base)
        discord_proxy = discord_proxy.merge(base)
        telegram_proxy = telegram_proxy.merge(base)

    user_agents = _parse_user_agents(raw.get("user_agents"))

    return NetworkSettings(
        user_agents=user_agents.normalised(),
        default_proxy=default_proxy.normalised(),
        discord_proxy=discord_proxy.normalised(),
        telegram_proxy=telegram_proxy.normalised(),
    )


def _parse_proxy_pool(raw: Any) -> ProxyPoolSettings:
    if raw is None:
        return ProxyPoolSettings()
    if isinstance(raw, Mapping):
        endpoints = raw.get("pool") or raw.get("endpoints") or raw.get("urls")
        health_url = raw.get("healthcheck") or raw.get("health_check")
        timeout = raw.get("timeout")
        recovery = raw.get("cooldown") or raw.get("recovery")
        settings = ProxyPoolSettings(
            endpoints=tuple(_to_string_list(endpoints)),
            health_check_url=_optional_string(health_url),
            health_check_timeout=float(timeout) if timeout is not None else 5.0,
            recovery_seconds=float(recovery) if recovery is not None else 180.0,
        )
        return settings.normalised()
    if isinstance(raw, Sequence):
        return ProxyPoolSettings(endpoints=tuple(_to_string_list(raw))).normalised()
    if isinstance(raw, str):
        return ProxyPoolSettings(endpoints=(raw.strip(),)).normalised()
    raise ValueError("Proxy configuration must be a mapping, list or string")


def _parse_user_agents(raw: Any) -> UserAgentSettings:
    if raw is None:
        return UserAgentSettings().normalised()
    if not isinstance(raw, Mapping):
        raise ValueError("Configuration field 'network.user_agents' must be a mapping")
    desktop = tuple(_to_string_list(raw.get("desktop"))) or _DEFAULT_DESKTOP_AGENTS
    mobile = tuple(_to_string_list(raw.get("mobile"))) or _DEFAULT_MOBILE_AGENTS
    ratio_raw = raw.get("mobile_ratio") or raw.get("mobile_share")
    ratio = float(ratio_raw) if ratio_raw is not None else 0.35
    return UserAgentSettings(desktop=desktop, mobile=mobile, mobile_ratio=ratio).normalised()


def _parse_rate_limit(raw: Any, *, fallback: RateLimitSettings) -> RateLimitSettings:
    if raw is None:
        return fallback
    if not isinstance(raw, Mapping):
        raise ValueError("Rate limit settings must be provided as a mapping")
    per_second = raw.get("per_second") or raw.get("rps")
    per_minute = raw.get("per_minute") or raw.get("rpm")
    concurrency = raw.get("concurrency")
    jitter = raw.get("jitter_ms") or raw.get("jitter")
    if isinstance(jitter, Sequence) and not isinstance(jitter, str):
        jitter_min, jitter_max = (float(jitter[0]), float(jitter[-1]))
    else:
        jitter_value = float(jitter) if jitter is not None else None
        jitter_min = jitter_max = jitter_value if jitter_value is not None else None
    cooldown = raw.get("cooldown") or raw.get("cooldown_seconds")
    settings = RateLimitSettings(
        per_second=float(per_second) if per_second is not None else fallback.per_second,
        per_minute=int(per_minute) if per_minute is not None else fallback.per_minute,
        concurrency=int(concurrency) if concurrency is not None else fallback.concurrency,
        jitter_min_ms=int(jitter_min) if jitter_min is not None else fallback.jitter_min_ms,
        jitter_max_ms=int(jitter_max) if jitter_max is not None else fallback.jitter_max_ms,
        cooldown_seconds=float(cooldown) if cooldown is not None else fallback.cooldown_seconds,
    )
    return fallback.merge(settings)


def _parse_filters(raw: Any, *, context: str) -> MessageFilters:
    if raw is None:
        return MessageFilters()
    if not isinstance(raw, Mapping):
        raise ValueError("Filter definitions must be mappings")
    allowed_types_raw = tuple(_to_string_list(raw.get("allowed_types")))
    blocked_types_raw = tuple(_to_string_list(raw.get("blocked_types")))
    allowed_types = _validate_message_types(
        allowed_types_raw, f"{context}.allowed_types"
    )
    blocked_types = _validate_message_types(
        blocked_types_raw, f"{context}.blocked_types"
    )
    return MessageFilters(
        whitelist=tuple(_to_string_list(raw.get("whitelist"))),
        blacklist=tuple(_to_string_list(raw.get("blacklist"))),
        allowed_senders=tuple(_to_string_list(raw.get("allowed_senders"))),
        blocked_senders=tuple(_to_string_list(raw.get("blocked_senders"))),
        allowed_types=allowed_types,
        blocked_types=blocked_types,
    )


def _parse_customization(raw: Any) -> MessageCustomization:
    if raw is None:
        return MessageCustomization()
    if not isinstance(raw, Mapping):
        raise ValueError("Customization options must be provided as a mapping")
    chips = _to_string_list(raw.get("chips") or raw.get("tags"))
    headers = _to_string_list(raw.get("header")) + _to_string_list(raw.get("headers"))
    footers = _to_string_list(raw.get("footer")) + _to_string_list(raw.get("footers"))
    replacements = _parse_replacements(raw.get("replace") or raw.get("replacements"))
    return MessageCustomization(
        chips=tuple(chips),
        headers=tuple(headers),
        footers=tuple(footers),
        replacements=tuple(replacements.items()),
    )


def _parse_formatting(raw: Any) -> FormattingProfile | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("Formatting options must be provided as a mapping")

    parse_mode = _optional_string(
        raw.get("parse_mode") or raw.get("mode") or raw.get("parseMode")
    )
    disable_preview = raw.get("disable_preview")
    if disable_preview is None:
        disable_preview = raw.get("disable_link_preview")
    max_length = raw.get("max_length") or raw.get("limit")
    ellipsis = _optional_string(raw.get("ellipsis") or raw.get("truncate_suffix"))
    attachments = _optional_string(
        raw.get("attachments_style") or raw.get("attachments")
    )

    kwargs: dict[str, Any] = {}
    if parse_mode is not None:
        kwargs["parse_mode"] = parse_mode
    if disable_preview is not None:
        kwargs["disable_link_preview"] = bool(disable_preview)
    if max_length is not None:
        kwargs["max_length"] = int(max_length)
    if ellipsis is not None:
        kwargs["ellipsis"] = ellipsis
    if attachments is not None:
        normalised = attachments.lower()
        if normalised not in {"compact", "minimal"}:
            raise ValueError(
                "Formatting attachments style must be either 'compact' or 'minimal'"
            )
        style_literal: Literal["compact", "minimal"] = cast(
            Literal["compact", "minimal"], normalised
        )
        kwargs["attachments_style"] = style_literal

    if not kwargs:
        return None
    provided = frozenset(kwargs.keys())
    return FormattingProfile(**kwargs, provided=provided)


def _validate_message_types(values: Sequence[str], label: str) -> tuple[str, ...]:
    if not values:
        return ()
    cleaned: list[str] = []
    invalid: list[str] = []
    for value in values:
        text = value.strip()
        if not text:
            continue
        normalised = text.casefold()
        if normalised not in _VALID_MESSAGE_TYPES:
            invalid.append(value)
            continue
        cleaned.append(text)
    if invalid:
        expected = ", ".join(sorted(_VALID_MESSAGE_TYPES))
        formatted = ", ".join(invalid)
        raise ValueError(
            f"Unsupported message type(s) in '{label}': {formatted}. "
            f"Allowed values: {expected}"
        )
    return tuple(cleaned)


def _parse_replacements(raw: Any) -> dict[str, str]:
    if raw is None:
        return {}
    replacements: dict[str, str] = {}
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            if value is None:
                continue
            replacements[str(key)] = str(value)
        return replacements
    if isinstance(raw, Sequence) and not isinstance(raw, str):
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            find = _optional_string(item.get("find") or item.get("from") or item.get("search"))
            replace = _optional_string(item.get("replace") or item.get("to") or item.get("with"))
            if find is None:
                continue
            replacements[find] = replace or ""
        return replacements
    raise ValueError("Replacement rules must be a mapping or list of mappings")


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")
    with path.open("r", encoding="utf-8") as file:
        data = safe_load(file) or {}
    if not isinstance(data, Mapping):
        raise ValueError("Configuration file must contain a mapping at the top level")
    return dict(data)


def _expect_mapping(raw: Any, field: str, *, default: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    if raw is None:
        return default or {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"Configuration field '{field}' must be a mapping")
    return cast(Mapping[str, Any], raw)


def _expect_sequence(raw: Any, field: str) -> Sequence[Any]:
    if raw is None:
        return ()
    if isinstance(raw, Sequence) and not isinstance(raw, str):
        return raw
    raise ValueError(f"Configuration field '{field}' must be a sequence")


def _require_string(raw: Mapping[str, Any], field: str, value: Any) -> str:
    text = _optional_string(value)
    if not text:
        raise ValueError(f"Configuration field '{field}' must be a non-empty string")
    return text


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_token_type(raw: Any) -> TokenType:
    candidate = str(raw or "auto").strip().lower()
    allowed: tuple[TokenType, ...] = ("auto", "bot", "user", "bearer")
    if candidate not in allowed:
        allowed_str = ", ".join(allowed)
        raise ValueError(
            f"Configuration field 'discord.token_type' must be one of: {allowed_str}"
        )
    return cast(TokenType, candidate)


def _coerce_channel_id(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"Configuration field '{field}' must contain integers for Discord channel IDs")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"Configuration field '{field}' must contain integers for Discord channel IDs")
        try:
            return int(text)
        except ValueError as exc:
            raise ValueError(
                f"Configuration field '{field}' must contain integers for Discord channel IDs"
            ) from exc
    raise ValueError(
        f"Configuration field '{field}' must contain integers for Discord channel IDs"
    )


def _resolve_state_file(config_path: Path, raw_value: Any, default: Path) -> Path:
    candidate = Path(str(raw_value)).expanduser() if raw_value else default.expanduser()
    if not candidate.is_absolute():
        candidate = (config_path.parent / candidate).resolve()
    return candidate


def _freeze_sequence(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(str(value) for value in values if value is not None)


def _freeze_replacements(values: Iterable[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    frozen: list[tuple[str, str]] = []
    for find, replace in values:
        frozen.append((str(find), str(replace)))
    return tuple(frozen)


def _merge_sequences(first: Iterable[str], second: Iterable[str]) -> tuple[str, ...]:
    result: dict[str, None] = {}
    for item in list(first) + list(second):
        if item is None:
            continue
        text = str(item).strip()
        if not text:
            continue
        if text in result:
            continue
        result[text] = None
    return tuple(result.keys())


def _casefold_unique(values: Iterable[str]) -> Iterator[str]:
    seen: dict[str, None] = {}
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        lowered = text.casefold()
        if lowered in seen:
            continue
        seen[lowered] = None
        yield lowered


def _unique_non_empty(values: Iterable[str]) -> Iterator[str]:
    seen: dict[str, None] = {}
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        if text in seen:
            continue
        seen[text] = None
        yield text


def _normalise_multiline(text: str) -> tuple[str, ...]:
    lines = text.splitlines()
    cleaned: list[str] = []
    blank = False
    for line in lines:
        stripped = line.rstrip()
        if stripped:
            cleaned.append(stripped)
            blank = False
            continue
        if not blank:
            cleaned.append("")
        blank = True
    while cleaned and not cleaned[-1]:
        cleaned.pop()
    return tuple(cleaned)


def _deduplicate(values: Sequence[str], *, fallback: tuple[str, ...]) -> tuple[str, ...]:
    if not values:
        return fallback
    return tuple(dict.fromkeys(str(value).strip() for value in values if value and str(value).strip()))


def _to_string_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        return [text] if text else []
    if isinstance(raw, Sequence):
        items: list[str] = []
        for value in raw:
            if value is None:
                continue
            text = str(value).strip()
            if text:
                items.append(text)
        return items
    raise ValueError("List configuration values must be strings or lists of strings")


def _first_non_null(candidate: Any, default: Any) -> Any:
    return candidate if candidate is not None else default

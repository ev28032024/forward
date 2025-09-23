from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import chain
from pathlib import Path
from typing import Any, Final, cast

from yaml import safe_load

from .discord_client import TokenType

DEFAULT_POLL_INTERVAL: Final[int] = 300
DEFAULT_STATE_FILE: Final[Path] = Path("monitor_state.json")
DEFAULT_MIN_MESSAGE_DELAY: Final[float] = 0.5
DEFAULT_MAX_MESSAGE_DELAY: Final[float] = 2.0
_ALLOWED_TOKEN_TYPES: tuple[TokenType, ...] = ("auto", "bot", "user", "bearer")


def _normalize_token_type(value: object) -> TokenType:
    candidate = "auto" if value is None else str(value).strip().lower()
    if not candidate:
        candidate = "auto"

    if candidate not in _ALLOWED_TOKEN_TYPES:
        allowed = ", ".join(_ALLOWED_TOKEN_TYPES)
        raise ValueError(
            f"Configuration field 'discord_token_type' must be one of: {allowed}"
        )

    return cast(TokenType, candidate)

SUPPORTED_MESSAGE_TYPES: frozenset[str] = frozenset(
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
    "ChannelMapping",
    "MessageFilters",
    "PreparedFilters",
    "MessageCustomization",
    "PreparedCustomization",
    "MonitorConfig",
]


@dataclass(frozen=True, slots=True)
class MessageFilters:
    """Keyword and sender based filters for forwarded messages."""

    whitelist: Sequence[str] = ()
    blacklist: Sequence[str] = ()
    allowed_senders: Sequence[str] = ()
    blocked_senders: Sequence[str] = ()
    allowed_types: Sequence[str] = ()
    blocked_types: Sequence[str] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "whitelist", _freeze_sequence(self.whitelist))
        object.__setattr__(self, "blacklist", _freeze_sequence(self.blacklist))
        object.__setattr__(
            self, "allowed_senders", _freeze_sequence(self.allowed_senders)
        )
        object.__setattr__(
            self, "blocked_senders", _freeze_sequence(self.blocked_senders)
        )
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
        """Materialise frequently used filter data in an efficient form."""

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
    """Immutable, case-folded filters ready for matching."""

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
    """Rules that customise the textual part of forwarded messages."""

    headers: Sequence[str] = ()
    footers: Sequence[str] = ()
    replacements: Sequence[tuple[str, str]] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", _freeze_sequence(self.headers))
        object.__setattr__(self, "footers", _freeze_sequence(self.footers))
        object.__setattr__(self, "replacements", _freeze_replacements(self.replacements))

    def combine(self, other: MessageCustomization | None) -> MessageCustomization:
        if other is None:
            return MessageCustomization(
                headers=self.headers,
                footers=self.footers,
                replacements=self.replacements,
            )

        merged_replacements: dict[str, str] = dict(self.replacements)
        merged_replacements.update(dict(other.replacements))

        return MessageCustomization(
            headers=_merge_sequences(self.headers, other.headers),
            footers=_merge_sequences(self.footers, other.footers),
            replacements=tuple(merged_replacements.items()),
        )

    def prepare(self) -> PreparedCustomization:
        header_text = "\n".join(segment for segment in self.headers if segment)
        footer_text = "\n".join(segment for segment in self.footers if segment)
        return PreparedCustomization(
            header_text=header_text,
            footer_text=footer_text,
            replacements=tuple(self.replacements),
        )

    def apply(self, content: str) -> str:
        return self.prepare().apply(content)


@dataclass(frozen=True, slots=True)
class PreparedCustomization:
    """Pre-processed representation of customisation rules."""

    header_text: str
    footer_text: str
    replacements: tuple[tuple[str, str], ...]

    def apply(self, content: str) -> str:
        if not (self.header_text or self.footer_text or self.replacements):
            return content or ""

        result = content or ""
        for find, replace in self.replacements:
            if not find:
                continue
            result = result.replace(find, replace)

        segments: list[str] = []
        if self.header_text:
            segments.append(self.header_text)
        if result:
            segments.append(result)
        if self.footer_text:
            segments.append(self.footer_text)

        if not segments:
            return ""
        if len(segments) == 1:
            return segments[0]
        return "\n\n".join(segments)


@dataclass(frozen=True, slots=True)
class ChannelMapping:
    """Relationship between a Discord channel and a Telegram chat."""

    discord_channel_id: int
    telegram_chat_id: str
    display_name: str | None = None
    filters: MessageFilters = field(default_factory=MessageFilters)
    customization: MessageCustomization = field(default_factory=MessageCustomization)

    def __post_init__(self) -> None:
        object.__setattr__(self, "telegram_chat_id", self.telegram_chat_id.strip())
        if self.display_name is not None:
            object.__setattr__(
                self,
                "display_name",
                self.display_name.strip() or None,
            )


@dataclass(frozen=True, slots=True)
class MonitorConfig:
    """Configuration for the Discord to Telegram monitor."""

    discord_token: str
    telegram_token: str
    telegram_chat_id: str
    discord_token_type: TokenType = "auto"
    announcement_channels: Sequence[ChannelMapping] = ()
    poll_interval: int = DEFAULT_POLL_INTERVAL
    state_file: Path = DEFAULT_STATE_FILE
    min_message_delay: float = DEFAULT_MIN_MESSAGE_DELAY
    max_message_delay: float = DEFAULT_MAX_MESSAGE_DELAY
    filters: MessageFilters = field(default_factory=MessageFilters)
    customization: MessageCustomization = field(default_factory=MessageCustomization)

    def __post_init__(self) -> None:
        object.__setattr__(self, "discord_token", self.discord_token.strip())
        object.__setattr__(
            self,
            "discord_token_type",
            _normalize_token_type(self.discord_token_type),
        )
        object.__setattr__(self, "telegram_token", self.telegram_token.strip())
        object.__setattr__(self, "telegram_chat_id", self.telegram_chat_id.strip())
        object.__setattr__(
            self,
            "announcement_channels",
            tuple(self.announcement_channels),
        )

    @classmethod
    def from_file(cls, path: Path) -> MonitorConfig:
        path = path.expanduser()
        data = _load_yaml(path)
        path = path.resolve()
        try:
            discord_token = str(data["discord_token"]).strip()
            telegram_token = str(data["telegram_token"]).strip()
            telegram_chat_id = str(data["telegram_chat_id"]).strip()
        except KeyError as exc:  # pragma: no cover - defensive programming
            missing = exc.args[0]
            raise ValueError(f"Missing required configuration key: {missing}") from exc

        token_type = _normalize_token_type(data.get("discord_token_type", "auto"))

        if not discord_token:
            raise ValueError("Configuration field 'discord_token' must not be empty")
        if not telegram_token:
            raise ValueError("Configuration field 'telegram_token' must not be empty")
        if not telegram_chat_id:
            raise ValueError("Configuration field 'telegram_chat_id' must not be empty")

        filters = _parse_filters(data.get("filters"))
        customization = _parse_customization(data.get("customization"))

        announcement_channels = tuple(
            _coerce_channel_mappings(
                data.get("announcement_channels", []),
                "announcement_channels",
                telegram_chat_id,
            )
        )

        poll_interval = int(data.get("poll_interval", DEFAULT_POLL_INTERVAL))
        if poll_interval < 0:
            raise ValueError("Configuration field 'poll_interval' cannot be negative")
        state_file = _resolve_state_file(path, data.get("state_file"), DEFAULT_STATE_FILE)
        min_message_delay = float(data.get("min_message_delay", DEFAULT_MIN_MESSAGE_DELAY))
        max_message_delay = float(data.get("max_message_delay", DEFAULT_MAX_MESSAGE_DELAY))

        if min_message_delay < 0:
            raise ValueError("Configuration field 'min_message_delay' cannot be negative")
        if max_message_delay < min_message_delay:
            raise ValueError(
                "Configuration field 'max_message_delay' must be greater than or equal to "
                "'min_message_delay'",
            )

        return cls(
            discord_token=discord_token,
            discord_token_type=token_type,
            telegram_token=telegram_token,
            telegram_chat_id=telegram_chat_id,
            announcement_channels=announcement_channels,
            poll_interval=poll_interval,
            state_file=state_file,
            min_message_delay=min_message_delay,
            max_message_delay=max_message_delay,
            filters=filters,
            customization=customization,
        )


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")

    with path.open("r", encoding="utf-8") as file:
        data = safe_load(file) or {}

    if not isinstance(data, Mapping):  # pragma: no cover - configuration error path
        raise ValueError("Configuration file must contain a mapping at the top level")
    return dict(data)


def _coerce_channel_mappings(
    value: Iterable[object], field_name: str, default_chat_id: str
) -> list[ChannelMapping]:
    if value is None:
        return []

    if not isinstance(value, list):
        raise ValueError(f"Configuration field '{field_name}' must be a list")

    mappings: list[ChannelMapping] = []
    for item in value:
        if isinstance(item, Mapping):
            discord_raw = item.get("discord_channel_id") or item.get("discord")
            telegram_raw = item.get("telegram_chat_id") or item.get("telegram")
            if discord_raw is None:
                raise ValueError(
                    f"Configuration field '{field_name}' entries must specify 'discord_channel_id'"
                )
            channel_id = _coerce_channel_id(discord_raw, field_name)
            fallback_chat = default_chat_id
            telegram_chat_source = fallback_chat if telegram_raw is None else telegram_raw
            telegram_chat = str(telegram_chat_source).strip()
            if not telegram_chat:
                raise ValueError(
                    "Configuration field "
                    f"'{field_name}' entries must specify a non-empty 'telegram_chat_id'",
                )
            filters = _parse_filters(item.get("filters"))
            customization = _parse_customization(item.get("customization"))
            display_name_raw = item.get("display_name")
            display_name = None
            if display_name_raw is not None:
                display_name = str(display_name_raw).strip() or None
        else:
            channel_id = _coerce_channel_id(item, field_name)
            telegram_chat = str(default_chat_id).strip()
            if not telegram_chat:
                raise ValueError(
                    "Configuration field "
                    f"'{field_name}' requires a non-empty fallback 'telegram_chat_id'",
                )
            filters = MessageFilters()
            customization = MessageCustomization()
            display_name = None

        mappings.append(
            ChannelMapping(
                channel_id,
                telegram_chat,
                display_name,
                filters,
                customization,
            )
        )

    return mappings


def _coerce_channel_id(value: object, field_name: str) -> int:
    if isinstance(value, bool):  # pragma: no cover - configuration error path
        raise ValueError(
            "Configuration field "
            f"'{field_name}' must contain integers for Discord channel IDs; got {value!r}",
        )
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(
                "Configuration field "
                f"'{field_name}' must contain integers for Discord channel IDs; got {value!r}",
            )
        try:
            return int(text)
        except ValueError as exc:  # pragma: no cover - configuration error path
            raise ValueError(
                "Configuration field "
                f"'{field_name}' must contain integers for Discord channel IDs; got {value!r}",
            ) from exc
    raise ValueError(
        "Configuration field "
        f"'{field_name}' must contain integers for Discord channel IDs; got {value!r}",
    )


def _parse_filters(raw: object) -> MessageFilters:
    if raw is None:
        return MessageFilters()

    if not isinstance(raw, Mapping):
        raise ValueError("Configuration field 'filters' must be a mapping if provided")

    allowed_types = _to_string_list(raw.get("allowed_types"))
    blocked_types = _to_string_list(raw.get("blocked_types"))
    _validate_message_types(allowed_types, "filters.allowed_types")
    _validate_message_types(blocked_types, "filters.blocked_types")

    return MessageFilters(
        whitelist=tuple(_to_string_list(raw.get("whitelist"))),
        blacklist=tuple(_to_string_list(raw.get("blacklist"))),
        allowed_senders=tuple(_to_string_list(raw.get("allowed_senders"))),
        blocked_senders=tuple(_to_string_list(raw.get("blocked_senders"))),
        allowed_types=tuple(allowed_types),
        blocked_types=tuple(blocked_types),
    )


def _parse_customization(raw: object) -> MessageCustomization:
    if raw is None:
        return MessageCustomization()

    if not isinstance(raw, Mapping):
        raise ValueError("Configuration field 'customization' must be a mapping if provided")

    headers = _to_string_list(raw.get("header")) + _to_string_list(raw.get("headers"))
    footers = _to_string_list(raw.get("footer")) + _to_string_list(raw.get("footers"))
    replacements = _parse_replacements(raw.get("replace") or raw.get("replacements"))

    return MessageCustomization(
        headers=tuple(headers),
        footers=tuple(footers),
        replacements=tuple(replacements.items()),
    )


def _parse_replacements(raw: object) -> dict[str, str]:
    if raw is None:
        return {}

    replacements: dict[str, str] = {}

    if isinstance(raw, Mapping):
        for key, value in raw.items():
            if value is None:
                continue
            replacements[str(key)] = str(value)
        return replacements

    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            find_value = None
            for candidate in ("find", "from", "search"):
                if candidate in item:
                    find_value = item[candidate]
                    break
            if find_value is None:
                continue
            replace_value = None
            for candidate in ("replace", "to"):
                if candidate in item:
                    replace_value = item[candidate]
                    break
            if replace_value is None:
                continue
            replacements[str(find_value)] = str(replace_value)
        return replacements

    raise ValueError("Replacement rules must be provided as a mapping or a list of mappings")


def _validate_message_types(values: Iterable[str], field_name: str) -> None:
    invalid = [value for value in values if value.casefold() not in SUPPORTED_MESSAGE_TYPES]
    if invalid:
        supported = ", ".join(sorted(SUPPORTED_MESSAGE_TYPES))
        raise ValueError(
            "Configuration field "
            f"'{field_name}' contains unsupported message types: {', '.join(invalid)}. "
            f"Supported types: {supported}",
        )


def _to_string_list(raw: object) -> list[str]:
    if raw is None:
        return []

    if isinstance(raw, str):
        value = raw.strip()
        return [value] if value else []

    if isinstance(raw, list):
        items: list[str] = []
        for entry in raw:
            if entry is None:
                continue
            text = str(entry).strip()
            if text:
                items.append(text)
        return items

    raise ValueError("List configuration values must be strings or lists of strings")


def _merge_sequences(first: Iterable[str], second: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(item) for item in chain(first, second)))


def _casefold_unique(values: Iterable[object]) -> Iterator[str]:
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


def _resolve_state_file(config_path: Path, raw_value: object, default: Path) -> Path:
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

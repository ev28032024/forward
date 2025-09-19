from __future__ import annotations

from dataclasses import dataclass, field
from itertools import chain
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, Iterator, List, Tuple

from yaml import safe_load


DEFAULT_POLL_INTERVAL = 300
DEFAULT_STATE_FILE = Path("monitor_state.json")
DEFAULT_MIN_MESSAGE_DELAY = 0.5
DEFAULT_MAX_MESSAGE_DELAY = 2.0

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


@dataclass(slots=True)
class ChannelMapping:
    """Relationship between a Discord channel and a Telegram chat."""

    discord_channel_id: int
    telegram_chat_id: str
    filters: "MessageFilters" = field(default_factory=lambda: MessageFilters())
    customization: "MessageCustomization" = field(
        default_factory=lambda: MessageCustomization()
    )


@dataclass(slots=True)
class MessageFilters:
    """Keyword and sender based filters for forwarded messages."""

    whitelist: List[str] = field(default_factory=list)
    blacklist: List[str] = field(default_factory=list)
    allowed_senders: List[str] = field(default_factory=list)
    blocked_senders: List[str] = field(default_factory=list)
    allowed_types: List[str] = field(default_factory=list)
    blocked_types: List[str] = field(default_factory=list)

    def combine(self, other: "MessageFilters" | None) -> "MessageFilters":
        if other is None:
            return MessageFilters(
                whitelist=list(self.whitelist),
                blacklist=list(self.blacklist),
                allowed_senders=list(self.allowed_senders),
                blocked_senders=list(self.blocked_senders),
                allowed_types=list(self.allowed_types),
                blocked_types=list(self.blocked_types),
            )

        return MessageFilters(
            whitelist=_merge_lists(self.whitelist, other.whitelist),
            blacklist=_merge_lists(self.blacklist, other.blacklist),
            allowed_senders=_merge_lists(self.allowed_senders, other.allowed_senders),
            blocked_senders=_merge_lists(self.blocked_senders, other.blocked_senders),
            allowed_types=_merge_lists(self.allowed_types, other.allowed_types),
            blocked_types=_merge_lists(self.blocked_types, other.blocked_types),
        )

    def prepare(self) -> "PreparedFilters":
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


@dataclass(slots=True)
class PreparedFilters:
    """Immutable, case-folded filters ready for matching."""

    whitelist: Tuple[str, ...]
    blacklist: Tuple[str, ...]
    allowed_senders: FrozenSet[str]
    blocked_senders: FrozenSet[str]
    allowed_types: FrozenSet[str]
    blocked_types: FrozenSet[str]
    requires_text: bool
    requires_types: bool


@dataclass(slots=True)
class MessageCustomization:
    """Rules that customise the textual part of forwarded messages."""

    headers: List[str] = field(default_factory=list)
    footers: List[str] = field(default_factory=list)
    replacements: Dict[str, str] = field(default_factory=dict)

    _prepared: "PreparedCustomization" | None = field(
        init=False, default=None, repr=False
    )

    def combine(self, other: "MessageCustomization" | None) -> "MessageCustomization":
        if other is None:
            return MessageCustomization(
                headers=list(self.headers),
                footers=list(self.footers),
                replacements=dict(self.replacements),
            )

        combined_replacements: Dict[str, str] = dict(self.replacements)
        combined_replacements.update(other.replacements)

        return MessageCustomization(
            headers=_merge_lists(self.headers, other.headers),
            footers=_merge_lists(self.footers, other.footers),
            replacements=combined_replacements,
        )

    def prepare(self) -> "PreparedCustomization":
        if self._prepared is None:
            header_text = "\n".join(segment for segment in self.headers if segment)
            footer_text = "\n".join(segment for segment in self.footers if segment)
            replacements = tuple(
                (find, replace) for find, replace in self.replacements.items()
            )
            self._prepared = PreparedCustomization(
                header_text=header_text,
                footer_text=footer_text,
                replacements=replacements,
            )
        return self._prepared

    def apply(self, content: str) -> str:
        return self.prepare().apply(content)


@dataclass(slots=True)
class PreparedCustomization:
    """Pre-processed representation of customisation rules."""

    header_text: str
    footer_text: str
    replacements: Tuple[Tuple[str, str], ...]

    def apply(self, content: str) -> str:
        if not (self.header_text or self.footer_text or self.replacements):
            return content or ""

        result = content or ""
        for find, replace in self.replacements:
            result = result.replace(find, replace)

        parts: List[str] = []
        if self.header_text:
            parts.append(self.header_text)
        if result:
            parts.append(result)
        if self.footer_text:
            parts.append(self.footer_text)

        if not parts:
            return ""
        if len(parts) == 1:
            return parts[0]
        return "\n\n".join(parts)


@dataclass(slots=True)
class MonitorConfig:
    """Configuration for the Discord to Telegram monitor."""

    discord_token: str
    telegram_token: str
    telegram_chat_id: str
    announcement_channels: List[ChannelMapping] = field(default_factory=list)
    poll_interval: int = DEFAULT_POLL_INTERVAL
    state_file: Path = DEFAULT_STATE_FILE
    min_message_delay: float = DEFAULT_MIN_MESSAGE_DELAY
    max_message_delay: float = DEFAULT_MAX_MESSAGE_DELAY
    filters: MessageFilters = field(default_factory=MessageFilters)
    customization: MessageCustomization = field(default_factory=MessageCustomization)

    @classmethod
    def from_file(cls, path: Path) -> "MonitorConfig":
        path = path.expanduser()
        data = _load_yaml(path)
        path = path.resolve()
        try:
            discord_token = data["discord_token"]
            telegram_token = data["telegram_token"]
            telegram_chat_id = str(data["telegram_chat_id"])
        except KeyError as exc:  # pragma: no cover - defensive programming
            missing = exc.args[0]
            raise ValueError(f"Missing required configuration key: {missing}") from exc

        filters = _parse_filters(data.get("filters"))
        customization = _parse_customization(data.get("customization"))

        announcement_channels = _coerce_channel_mappings(
            data.get("announcement_channels", []),
            "announcement_channels",
            telegram_chat_id,
        )
        poll_interval = int(data.get("poll_interval", DEFAULT_POLL_INTERVAL))
        state_file = _resolve_state_file(path, data.get("state_file"), DEFAULT_STATE_FILE)
        min_message_delay = float(
            data.get("min_message_delay", DEFAULT_MIN_MESSAGE_DELAY)
        )
        max_message_delay = float(
            data.get("max_message_delay", DEFAULT_MAX_MESSAGE_DELAY)
        )

        if min_message_delay < 0:
            raise ValueError("Configuration field 'min_message_delay' cannot be negative")
        if max_message_delay < min_message_delay:
            raise ValueError(
                "Configuration field 'max_message_delay' must be greater than or equal to 'min_message_delay'"
            )

        return cls(
            discord_token=discord_token,
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


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")

    with path.open("r", encoding="utf-8") as file:
        data = safe_load(file) or {}

    if not isinstance(data, dict):  # pragma: no cover - configuration error path
        raise ValueError("Configuration file must contain a mapping at the top level")
    return data


def _coerce_channel_mappings(
    value: Iterable[object], field_name: str, default_chat_id: str
) -> List[ChannelMapping]:
    if value is None:
        return []

    if not isinstance(value, list):
        raise ValueError(f"Configuration field '{field_name}' must be a list")

    mappings: List[ChannelMapping] = []
    for item in value:
        if isinstance(item, dict):
            discord_raw = item.get("discord_channel_id") or item.get("discord")
            telegram_raw = item.get("telegram_chat_id") or item.get("telegram")
            if discord_raw is None:
                raise ValueError(
                    f"Configuration field '{field_name}' entries must specify 'discord_channel_id'"
                )
            channel_id = _coerce_channel_id(discord_raw, field_name)
            telegram_chat = str(telegram_raw or default_chat_id)
            filters = _parse_filters(item.get("filters"))
            customization = _parse_customization(item.get("customization"))
        else:
            channel_id = _coerce_channel_id(item, field_name)
            telegram_chat = str(default_chat_id)
            filters = MessageFilters()
            customization = MessageCustomization()

        mappings.append(ChannelMapping(channel_id, telegram_chat, filters, customization))

    return mappings


def _coerce_channel_id(value: object, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:  # pragma: no cover - configuration error path
        raise ValueError(
            f"Configuration field '{field_name}' must contain integers for Discord channel IDs; got {value!r}"
        ) from exc


def _parse_filters(raw: object) -> MessageFilters:
    if raw is None:
        return MessageFilters()

    if not isinstance(raw, dict):
        raise ValueError("Configuration field 'filters' must be a mapping if provided")

    return MessageFilters(
        whitelist=_to_string_list(raw.get("whitelist")),
        blacklist=_to_string_list(raw.get("blacklist")),
        allowed_senders=_to_string_list(raw.get("allowed_senders")),
        blocked_senders=_to_string_list(raw.get("blocked_senders")),
        allowed_types=_to_string_list(raw.get("allowed_types")),
        blocked_types=_to_string_list(raw.get("blocked_types")),
    )


def _parse_customization(raw: object) -> MessageCustomization:
    if raw is None:
        return MessageCustomization()

    if not isinstance(raw, dict):
        raise ValueError("Configuration field 'customization' must be a mapping if provided")

    headers = _to_string_list(raw.get("header")) + _to_string_list(raw.get("headers"))
    footers = _to_string_list(raw.get("footer")) + _to_string_list(raw.get("footers"))
    replacements = _parse_replacements(raw.get("replace") or raw.get("replacements"))

    return MessageCustomization(headers=headers, footers=footers, replacements=replacements)


def _parse_replacements(raw: object) -> Dict[str, str]:
    if raw is None:
        return {}

    replacements: Dict[str, str] = {}

    if isinstance(raw, dict):
        for key, value in raw.items():
            if value is None:
                continue
            replacements[str(key)] = str(value)
        return replacements

    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            find = item.get("find") or item.get("from") or item.get("search")
            replace = item.get("replace") or item.get("to")
            if find is None or replace is None:
                continue
            replacements[str(find)] = str(replace)
        return replacements

    raise ValueError("Replacement rules must be provided as a mapping or a list of mappings")


def _to_string_list(raw: object) -> List[str]:
    if raw is None:
        return []

    if isinstance(raw, str):
        value = raw.strip()
        return [value] if value else []

    if isinstance(raw, list):
        items: List[str] = []
        for entry in raw:
            if entry is None:
                continue
            text = str(entry).strip()
            if text:
                items.append(text)
        return items

    raise ValueError("List configuration values must be strings or lists of strings")


def _merge_lists(first: Iterable[str], second: Iterable[str]) -> List[str]:
    return list(dict.fromkeys(str(item) for item in chain(first, second)))


def _casefold_unique(values: Iterable[object]) -> Iterator[str]:
    seen: Dict[str, None] = {}
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


def _resolve_state_file(
    config_path: Path, raw_value: object, default: Path
) -> Path:
    candidate = Path(str(raw_value)).expanduser() if raw_value else default.expanduser()
    if not candidate.is_absolute():
        candidate = (config_path.parent / candidate).resolve()
    return candidate

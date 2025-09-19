from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List

import yaml


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


@dataclass(slots=True)
class MessageCustomization:
    """Rules that customise the textual part of forwarded messages."""

    headers: List[str] = field(default_factory=list)
    footers: List[str] = field(default_factory=list)
    replacements: Dict[str, str] = field(default_factory=dict)

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

    def apply(self, content: str) -> str:
        result = content or ""
        for find, replace in self.replacements.items():
            result = result.replace(find, replace)

        parts: List[str] = []
        header_text = "\n".join([segment for segment in self.headers if segment])
        footer_text = "\n".join([segment for segment in self.footers if segment])

        if header_text:
            parts.append(header_text)
        if result:
            parts.append(result)
        if footer_text:
            parts.append(footer_text)

        return "\n\n".join(parts) if parts else ""


@dataclass(slots=True)
class MonitorConfig:
    """Configuration for the Discord to Telegram monitor."""

    discord_token: str
    telegram_token: str
    telegram_chat_id: str
    announcement_channels: List[ChannelMapping] = field(default_factory=list)
    pinned_channels: List[ChannelMapping] = field(default_factory=list)
    poll_interval: int = 300
    state_file: Path = Path("monitor_state.json")
    min_message_delay: float = 0.5
    max_message_delay: float = 2.0
    filters: MessageFilters = field(default_factory=MessageFilters)
    customization: MessageCustomization = field(default_factory=MessageCustomization)

    @classmethod
    def from_file(cls, path: Path) -> "MonitorConfig":
        data = _load_yaml(path)
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
        pinned_channels = _coerce_channel_mappings(
            data.get("pinned_channels", []),
            "pinned_channels",
            telegram_chat_id,
        )
        poll_interval = int(data.get("poll_interval", cls.poll_interval))
        state_file_raw = data.get("state_file")
        state_file = Path(state_file_raw) if state_file_raw else cls.state_file
        min_message_delay = float(data.get("min_message_delay", cls.min_message_delay))
        max_message_delay = float(data.get("max_message_delay", cls.max_message_delay))

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
            pinned_channels=pinned_channels,
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
        data = yaml.safe_load(file) or {}

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
    seen: Dict[str, None] = {}
    for item in list(first) + list(second):
        key = str(item)
        if key not in seen:
            seen[key] = None
    return list(seen.keys())

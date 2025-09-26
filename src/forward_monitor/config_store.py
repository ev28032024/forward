"""SQLite backed storage for Forward Monitor configuration."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

from .models import ChannelConfig, FilterConfig, FormattingOptions, ReplacementRule

_DB_PRAGMA = (
    "PRAGMA journal_mode=WAL;"
    "PRAGMA synchronous=NORMAL;"
    "PRAGMA foreign_keys=ON;"
)


@dataclass(slots=True)
class ChannelRecord:
    """Raw channel record loaded from SQLite."""

    id: int
    discord_id: str
    telegram_chat_id: str
    label: str
    active: bool
    last_message_id: str | None


class ConfigStore:
    """Persisted settings and channel mappings."""

    def __init__(self, path: Path):
        self._path = path
        self._conn = sqlite3.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        self._setup()

    # ------------------------------------------------------------------
    # Schema initialisation
    # ------------------------------------------------------------------
    def _setup(self) -> None:
        with closing(self._conn.cursor()) as cur:
            for statement in _DB_PRAGMA.split(";"):
                if statement.strip():
                    cur.execute(statement)
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS admins (
                    user_id INTEGER PRIMARY KEY
                );

                CREATE TABLE IF NOT EXISTS channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    discord_id TEXT NOT NULL UNIQUE,
                    telegram_chat_id TEXT NOT NULL,
                    label TEXT DEFAULT '',
                    active INTEGER DEFAULT 1,
                    last_message_id TEXT
                );

                CREATE TABLE IF NOT EXISTS channel_options (
                    channel_id INTEGER NOT NULL,
                    option_key TEXT NOT NULL,
                    option_value TEXT NOT NULL,
                    PRIMARY KEY (channel_id, option_key)
                );

                CREATE TABLE IF NOT EXISTS filters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id INTEGER NOT NULL,
                    filter_type TEXT NOT NULL,
                    value TEXT NOT NULL,
                    UNIQUE(channel_id, filter_type, value)
                );

                CREATE TABLE IF NOT EXISTS replacements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id INTEGER NOT NULL,
                    pattern TEXT NOT NULL,
                    replacement TEXT NOT NULL,
                    UNIQUE(channel_id, pattern)
                );
                """
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Basic settings
    # ------------------------------------------------------------------
    def set_setting(self, key: str, value: str) -> None:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._conn.commit()

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with closing(self._conn.cursor()) as cur:
            cur.execute("SELECT value FROM settings WHERE key=?", (key,))
            row = cur.fetchone()
        return row["value"] if row else default

    def delete_setting(self, key: str) -> None:
        with closing(self._conn.cursor()) as cur:
            cur.execute("DELETE FROM settings WHERE key=?", (key,))
            self._conn.commit()

    def iter_settings(self, prefix: str | None = None) -> Iterator[tuple[str, str]]:
        query = "SELECT key, value FROM settings"
        params: tuple[str, ...] = ()
        if prefix:
            query += " WHERE key LIKE ?"
            params = (f"{prefix}%",)
        with closing(self._conn.cursor()) as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
        for row in rows:
            yield str(row["key"]), str(row["value"])

    # ------------------------------------------------------------------
    # Admins
    # ------------------------------------------------------------------
    def list_admins(self) -> list[int]:
        with closing(self._conn.cursor()) as cur:
            cur.execute("SELECT user_id FROM admins ORDER BY user_id ASC")
            rows = cur.fetchall()
        return [int(row["user_id"]) for row in rows]

    def add_admin(self, user_id: int) -> None:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "INSERT OR IGNORE INTO admins(user_id) VALUES(?)",
                (int(user_id),),
            )
            self._conn.commit()

    def remove_admin(self, user_id: int) -> None:
        with closing(self._conn.cursor()) as cur:
            cur.execute("DELETE FROM admins WHERE user_id=?", (int(user_id),))
            self._conn.commit()

    def has_admins(self) -> bool:
        with closing(self._conn.cursor()) as cur:
            cur.execute("SELECT 1 FROM admins LIMIT 1")
            row = cur.fetchone()
        return bool(row)

    # ------------------------------------------------------------------
    # Channels
    # ------------------------------------------------------------------
    def add_channel(self, discord_id: str, telegram_chat_id: str, label: str = "") -> ChannelRecord:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "INSERT INTO channels(discord_id, telegram_chat_id, label) VALUES(?, ?, ?)",
                (discord_id, telegram_chat_id, label),
            )
            channel_id = cur.lastrowid
            self._conn.commit()
        return ChannelRecord(
            id=channel_id,
            discord_id=discord_id,
            telegram_chat_id=telegram_chat_id,
            label=label,
            active=True,
            last_message_id=None,
        )

    def remove_channel(self, discord_id: str) -> bool:
        with closing(self._conn.cursor()) as cur:
            cur.execute("DELETE FROM channels WHERE discord_id=?", (discord_id,))
            deleted = cur.rowcount > 0
            self._conn.commit()
        return deleted

    def list_channels(self) -> list[ChannelRecord]:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "SELECT id, discord_id, telegram_chat_id, label, active, last_message_id"
                " FROM channels ORDER BY discord_id"
            )
            rows = cur.fetchall()
        return [
            ChannelRecord(
                id=int(row["id"]),
                discord_id=str(row["discord_id"]),
                telegram_chat_id=str(row["telegram_chat_id"]),
                label=str(row["label"] or ""),
                active=bool(row["active"]),
                last_message_id=str(row["last_message_id"]) if row["last_message_id"] else None,
            )
            for row in rows
        ]

    def get_channel(self, discord_id: str) -> ChannelRecord | None:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "SELECT id, discord_id, telegram_chat_id, label, active, last_message_id"
                " FROM channels WHERE discord_id=?",
                (discord_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return ChannelRecord(
            id=int(row["id"]),
            discord_id=str(row["discord_id"]),
            telegram_chat_id=str(row["telegram_chat_id"]),
            label=str(row["label"] or ""),
            active=bool(row["active"]),
            last_message_id=str(row["last_message_id"]) if row["last_message_id"] else None,
        )

    def set_channel_option(self, channel_id: int, option_key: str, option_value: str) -> None:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "INSERT INTO channel_options(channel_id, option_key, option_value) VALUES(?, ?, ?)"
                " ON CONFLICT(channel_id, option_key) DO UPDATE SET option_value=excluded.option_value",
                (channel_id, option_key, option_value),
            )
            self._conn.commit()

    def delete_channel_option(self, channel_id: int, option_key: str) -> None:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "DELETE FROM channel_options WHERE channel_id=? AND option_key=?",
                (channel_id, option_key),
            )
            self._conn.commit()

    def iter_channel_options(self, channel_id: int) -> Iterator[tuple[str, str]]:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "SELECT option_key, option_value FROM channel_options WHERE channel_id=?",
                (channel_id,),
            )
            rows = cur.fetchall()
        for row in rows:
            yield str(row["option_key"]), str(row["option_value"])

    def set_last_message(self, channel_id: int, message_id: str) -> None:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "UPDATE channels SET last_message_id=? WHERE id=?",
                (message_id, channel_id),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Filters & replacements
    # ------------------------------------------------------------------
    def add_filter(self, channel_id: int, filter_type: str, value: str) -> None:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "INSERT OR IGNORE INTO filters(channel_id, filter_type, value) VALUES(?, ?, ?)",
                (channel_id, filter_type, value),
            )
            self._conn.commit()

    def remove_filter(self, channel_id: int, filter_type: str, value: str | None = None) -> int:
        with closing(self._conn.cursor()) as cur:
            if value is None:
                cur.execute(
                    "DELETE FROM filters WHERE channel_id=? AND filter_type=?",
                    (channel_id, filter_type),
                )
            else:
                cur.execute(
                    "DELETE FROM filters WHERE channel_id=? AND filter_type=? AND value=?",
                    (channel_id, filter_type, value),
                )
            removed = cur.rowcount
            self._conn.commit()
        return removed

    def iter_filters(self, channel_id: int) -> Iterator[tuple[str, str]]:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "SELECT filter_type, value FROM filters WHERE channel_id=?",
                (channel_id,),
            )
            rows = cur.fetchall()
        for row in rows:
            yield str(row["filter_type"]), str(row["value"])

    def add_replacement(self, channel_id: int, pattern: str, replacement: str) -> None:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "INSERT INTO replacements(channel_id, pattern, replacement) VALUES(?, ?, ?)"
                " ON CONFLICT(channel_id, pattern) DO UPDATE SET replacement=excluded.replacement",
                (channel_id, pattern, replacement),
            )
            self._conn.commit()

    def remove_replacement(self, channel_id: int, pattern: str | None = None) -> int:
        with closing(self._conn.cursor()) as cur:
            if pattern is None:
                cur.execute("DELETE FROM replacements WHERE channel_id=?", (channel_id,))
            else:
                cur.execute(
                    "DELETE FROM replacements WHERE channel_id=? AND pattern=?",
                    (channel_id, pattern),
                )
            removed = cur.rowcount
            self._conn.commit()
        return removed

    def iter_replacements(self, channel_id: int) -> Iterator[ReplacementRule]:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "SELECT pattern, replacement FROM replacements WHERE channel_id=?",
                (channel_id,),
            )
            rows = cur.fetchall()
        for row in rows:
            yield ReplacementRule(pattern=str(row["pattern"]), replacement=str(row["replacement"]))

    # ------------------------------------------------------------------
    # Composite loads
    # ------------------------------------------------------------------
    def load_channel_configurations(self) -> list[ChannelConfig]:
        defaults = self._load_default_options()
        default_filters = self._load_filter_config(0)
        default_replacements = list(self.iter_replacements(0))
        configs: list[ChannelConfig] = []
        for record in self.list_channels():
            formatting = defaults["formatting"].copy()
            channel_options = dict(self.iter_channel_options(record.id))
            channel_formatting = _formatting_from_options(formatting, channel_options)
            filters = default_filters.merge(self._load_filter_config(record.id))
            replacements = [*default_replacements, *self.iter_replacements(record.id)]
            configs.append(
                ChannelConfig(
                    discord_id=record.discord_id,
                    telegram_chat_id=record.telegram_chat_id,
                    label=record.label or record.discord_id,
                    formatting=channel_formatting,
                    filters=filters,
                    replacements=tuple(replacements),
                    last_message_id=record.last_message_id,
                    active=record.active,
                    storage_id=record.id,
                )
            )
        return configs

    def _load_default_options(self) -> dict[str, dict[str, str]]:
        settings: dict[str, dict[str, str]] = {"formatting": {}}
        for key, value in self.iter_settings("formatting."):
            settings.setdefault("formatting", {})[key.removeprefix("formatting.")] = value
        return settings

    def _load_filter_config(self, channel_id: int) -> FilterConfig:
        filters = FilterConfig()
        for filter_type, value in self.iter_filters(channel_id):
            target = _filter_target(filters, filter_type)
            if target is not None:
                target.add(value)
        return filters

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def close(self) -> None:
        self._conn.close()


def _filter_target(filters: FilterConfig, filter_type: str) -> set[str] | None:
    mapping = {
        "whitelist": filters.whitelist,
        "blacklist": filters.blacklist,
        "allowed_senders": filters.allowed_senders,
        "blocked_senders": filters.blocked_senders,
        "allowed_types": filters.allowed_types,
        "blocked_types": filters.blocked_types,
    }
    return mapping.get(filter_type)


def _formatting_from_options(
    base: dict[str, str],
    overrides: dict[str, str],
) -> FormattingOptions:
    options = {**base, **{k.removeprefix("formatting."): v for k, v in overrides.items() if k.startswith("formatting.")}}
    return FormattingOptions(
        parse_mode=options.get("parse_mode", "MarkdownV2"),
        disable_preview=options.get("disable_preview", "true").lower() == "true",
        max_length=int(options.get("max_length", "3500")),
        ellipsis=options.get("ellipsis", "…"),
        attachments_style=options.get("attachments_style", "summary"),
        header=options.get("header", ""),
        footer=options.get("footer", ""),
        chip=options.get("chip", ""),
    )

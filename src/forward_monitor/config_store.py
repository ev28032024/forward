"""SQLite backed storage for Forward Monitor configuration."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit, urlunsplit

from .models import ChannelConfig, FilterConfig, FormattingOptions, NetworkOptions
from .utils import normalize_username

_DB_PRAGMA = "PRAGMA journal_mode=WAL;" "PRAGMA synchronous=NORMAL;" "PRAGMA foreign_keys=ON;"


@dataclass(slots=True)
class AdminRecord:
    """Admin identity stored in the configuration."""

    user_id: int | None
    username: str | None


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
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER UNIQUE,
                    username TEXT UNIQUE COLLATE NOCASE
                );

                CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT UNIQUE COLLATE NOCASE
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

                """
            )
            self._migrate_admins(cur)
            self._conn.commit()

    def _migrate_admins(self, cur: sqlite3.Cursor) -> None:
        cur.execute("PRAGMA table_info(admins)")
        columns = {str(row[1]) for row in cur.fetchall()}
        expected = {"id", "user_id", "username"}
        if columns == expected:
            return
        if columns == {"user_id"}:
            cur.executescript(
                """
                ALTER TABLE admins RENAME TO admins_legacy;
                CREATE TABLE admins (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER UNIQUE,
                    username TEXT UNIQUE COLLATE NOCASE
                );
                INSERT INTO admins(user_id)
                SELECT user_id FROM admins_legacy;
                DROP TABLE admins_legacy;
                """
            )
            return
        if not columns:
            cur.executescript(
                """
                DROP TABLE IF EXISTS admins;
                CREATE TABLE admins (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER UNIQUE,
                    username TEXT UNIQUE COLLATE NOCASE
                );
                """
            )
            return
        raise RuntimeError("Unsupported admins schema detected")

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
    # Network options helpers
    # ------------------------------------------------------------------
    def load_network_options(self) -> NetworkOptions:
        proxy_url = self.get_setting("proxy.discord.url")
        proxy_login = self.get_setting("proxy.discord.login")
        proxy_password = self.get_setting("proxy.discord.password")

        legacy_proxy = None
        if not proxy_url:
            legacy_proxy = self.get_setting("proxy.discord")
            if legacy_proxy:
                parsed = urlsplit(legacy_proxy)
                if parsed.username or parsed.password:
                    proxy_login = proxy_login or parsed.username or None
                    proxy_password = proxy_password or parsed.password or None
                    host = parsed.hostname or ""
                    if parsed.port:
                        host = f"{host}:{parsed.port}"
                    components = (
                        parsed.scheme,
                        host,
                        parsed.path,
                        parsed.query,
                        parsed.fragment,
                    )
                    proxy_url = urlunsplit(components)
                else:
                    proxy_url = legacy_proxy

        if legacy_proxy and proxy_url and not self.get_setting("proxy.discord.url"):
            self.set_setting("proxy.discord.url", proxy_url)
            if proxy_login:
                self.set_setting("proxy.discord.login", proxy_login)
            if proxy_password:
                self.set_setting("proxy.discord.password", proxy_password)
            self.delete_setting("proxy.discord")

        return NetworkOptions(
            discord_proxy_url=proxy_url,
            discord_proxy_login=proxy_login,
            discord_proxy_password=proxy_password,
            discord_user_agent=self.get_setting("ua.discord"),
        )

    # ------------------------------------------------------------------
    # Admins
    # ------------------------------------------------------------------
    def list_admins(self) -> list[AdminRecord]:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "SELECT user_id, username FROM admins ORDER BY "
                "COALESCE(username, CAST(user_id AS TEXT)) COLLATE NOCASE"
            )
            rows = cur.fetchall()
        return [
            AdminRecord(
                user_id=int(row["user_id"]) if row["user_id"] is not None else None,
                username=str(row["username"]) if row["username"] else None,
            )
            for row in rows
        ]

    def add_admin(self, user_id: int | None = None, username: str | None = None) -> None:
        normalized = normalize_username(username)
        with closing(self._conn.cursor()) as cur:
            if user_id is not None:
                cur.execute(
                    "INSERT INTO admins(user_id, username) VALUES(?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
                    (int(user_id), normalized),
                )
            elif normalized is not None:
                cur.execute(
                    "INSERT INTO admins(username) VALUES(?) "
                    "ON CONFLICT(username) DO UPDATE SET username=excluded.username",
                    (normalized,),
                )
            else:
                raise ValueError("Either user_id or username must be provided")
            self._conn.commit()

    def remove_admin(self, identifier: int | str) -> bool:
        with closing(self._conn.cursor()) as cur:
            if isinstance(identifier, int):
                cur.execute("DELETE FROM admins WHERE user_id=?", (int(identifier),))
            else:
                normalized = normalize_username(identifier)
                cur.execute(
                    "DELETE FROM admins WHERE username=? COLLATE NOCASE",
                    (normalized,),
                )
            deleted = cur.rowcount > 0
            self._conn.commit()
        return deleted

    def has_admins(self) -> bool:
        with closing(self._conn.cursor()) as cur:
            cur.execute("SELECT 1 FROM admins LIMIT 1")
            row = cur.fetchone()
        return bool(row)

    def remember_user(self, user_id: int, username: str | None) -> None:
        normalized = normalize_username(username)
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "INSERT INTO user_profiles(user_id, username) VALUES(?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
                (int(user_id), normalized),
            )
            if normalized is not None:
                cur.execute(
                    "INSERT INTO user_profiles(user_id, username) VALUES(?, ?) "
                    "ON CONFLICT(username) DO UPDATE SET user_id=excluded.user_id, "
                    "username=excluded.username",
                    (int(user_id), normalized),
                )
                cur.execute(
                    "UPDATE admins SET user_id=COALESCE(user_id, ?), username=? "
                    "WHERE username=? COLLATE NOCASE",
                    (int(user_id), normalized, normalized),
                )
                cur.execute(
                    "UPDATE admins SET username=? WHERE user_id=?",
                    (normalized, int(user_id)),
                )
            self._conn.commit()

    def resolve_user_id(self, username: str) -> int | None:
        normalized = normalize_username(username)
        if normalized is None:
            return None
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "SELECT user_id FROM user_profiles WHERE username=? COLLATE NOCASE",
                (normalized,),
            )
            row = cur.fetchone()
        return int(row["user_id"]) if row and row["user_id"] is not None else None

    # ------------------------------------------------------------------
    # Channels
    # ------------------------------------------------------------------
    def add_channel(self, discord_id: str, telegram_chat_id: str, label: str = "") -> ChannelRecord:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "INSERT INTO channels(discord_id, telegram_chat_id, label) VALUES(?, ?, ?)",
                (discord_id, telegram_chat_id, label),
            )
            channel_id_raw = cur.lastrowid
            if channel_id_raw is None:
                raise RuntimeError("Failed to insert channel record")
            channel_id = int(channel_id_raw)
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
                """
                INSERT INTO channel_options(channel_id, option_key, option_value)
                VALUES(?, ?, ?)
                ON CONFLICT(channel_id, option_key)
                    DO UPDATE SET option_value=excluded.option_value
                """,
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
    # Filters
    # ------------------------------------------------------------------
    def add_filter(self, channel_id: int, filter_type: str, value: str) -> bool:
        filter_type_key = filter_type.strip().lower()
        prepared = normalize_filter_value(filter_type_key, value)
        if prepared is None:
            raise ValueError("invalid filter value")
        stored_value, compare_key = prepared
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "SELECT id, value FROM filters WHERE channel_id=? AND filter_type=?",
                (channel_id, filter_type_key),
            )
            rows = cur.fetchall()
            for row in rows:
                existing = normalize_filter_value(filter_type_key, str(row["value"]))
                if existing and existing[1] == compare_key:
                    return False
            cur.execute(
                "INSERT INTO filters(channel_id, filter_type, value) VALUES(?, ?, ?)",
                (channel_id, filter_type_key, stored_value),
            )
            self._conn.commit()
        return True

    def remove_filter(self, channel_id: int, filter_type: str, value: str | None = None) -> int:
        filter_type_key = filter_type.strip().lower()
        with closing(self._conn.cursor()) as cur:
            if value is None:
                cur.execute(
                    "DELETE FROM filters WHERE channel_id=? AND filter_type=?",
                    (channel_id, filter_type_key),
                )
                removed = cur.rowcount
            else:
                prepared = normalize_filter_value(filter_type_key, value)
                if prepared is None:
                    return 0
                _, compare_key = prepared
                cur.execute(
                    "SELECT id, value FROM filters WHERE channel_id=? AND filter_type=?",
                    (channel_id, filter_type_key),
                )
                rows = cur.fetchall()
                matched_ids = [
                    int(row["id"])
                    for row in rows
                    if (
                        existing := normalize_filter_value(
                            filter_type_key, str(row["value"])
                        )
                    )
                    and existing[1] == compare_key
                ]
                for entry_id in matched_ids:
                    cur.execute("DELETE FROM filters WHERE id=?", (entry_id,))
                removed = len(matched_ids)
            self._conn.commit()
        return removed

    def clear_filters(self, channel_id: int) -> int:
        with closing(self._conn.cursor()) as cur:
            cur.execute("DELETE FROM filters WHERE channel_id=?", (channel_id,))
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

    def get_filter_config(self, channel_id: int) -> FilterConfig:
        return self._load_filter_config(channel_id)

    # ------------------------------------------------------------------
    # Composite loads
    # ------------------------------------------------------------------
    def load_channel_configurations(self) -> list[ChannelConfig]:
        defaults = self._load_default_options()
        default_filters = self._load_filter_config(0)
        configs: list[ChannelConfig] = []
        for record in self.list_channels():
            formatting = defaults["formatting"].copy()
            channel_options = dict(self.iter_channel_options(record.id))
            channel_formatting = _formatting_from_options(formatting, channel_options)
            filters = default_filters.merge(self._load_filter_config(record.id))
            configs.append(
                ChannelConfig(
                    discord_id=record.discord_id,
                    telegram_chat_id=record.telegram_chat_id,
                    label=record.label or record.discord_id,
                    formatting=channel_formatting,
                    filters=filters,
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
        seen: dict[str, set[str]] = {}
        for filter_type_raw, value in self.iter_filters(channel_id):
            filter_type = filter_type_raw.strip().lower()
            normalized = normalize_filter_value(filter_type, value)
            if not normalized:
                continue
            stored_value, compare_key = normalized
            seen.setdefault(filter_type, set())
            if compare_key in seen[filter_type]:
                continue
            seen[filter_type].add(compare_key)
            target = _filter_target(filters, filter_type)
            if target is None:
                continue
            if filter_type in _TEXT_FILTER_TYPES:
                target.add(value.strip())
            else:
                target.add(stored_value)
        return filters

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def close(self) -> None:
        self._conn.close()


_TEXT_FILTER_TYPES = {"whitelist", "blacklist"}
_SENDER_FILTER_TYPES = {"allowed_senders", "blocked_senders"}
_TYPE_FILTER_TYPES = {"allowed_types", "blocked_types"}


def normalize_filter_value(filter_type: str, value: str) -> tuple[str, str] | None:
    filter_key = filter_type.strip().lower()
    trimmed = value.strip()
    if not trimmed:
        return None
    if filter_key in _SENDER_FILTER_TYPES:
        if trimmed.lstrip("-").isdigit():
            numeric = str(int(trimmed))
            return numeric, f"id:{numeric}"
        normalized_name = normalize_username(trimmed)
        if normalized_name is None:
            return None
        return normalized_name, f"name:{normalized_name}"
    if filter_key in _TYPE_FILTER_TYPES:
        normalized_value = trimmed.lower()
        return normalized_value, normalized_value
    if filter_key in _TEXT_FILTER_TYPES:
        return trimmed, trimmed.casefold()
    return trimmed, trimmed


def format_filter_value(filter_type: str, value: str) -> str:
    filter_key = filter_type.strip().lower()
    cleaned = value.strip()
    if not cleaned:
        return cleaned
    if filter_key in _SENDER_FILTER_TYPES and cleaned.lstrip("-").isdigit():
        return str(int(cleaned))
    return cleaned


def _filter_target(filters: FilterConfig, filter_type: str) -> set[str] | None:
    normalized_type = filter_type.strip().lower()
    mapping = {
        "whitelist": filters.whitelist,
        "blacklist": filters.blacklist,
        "allowed_senders": filters.allowed_senders,
        "blocked_senders": filters.blocked_senders,
        "allowed_types": filters.allowed_types,
        "blocked_types": filters.blocked_types,
    }
    return mapping.get(normalized_type)


def _formatting_from_options(
    base: dict[str, str],
    overrides: dict[str, str],
) -> FormattingOptions:
    formatting_overrides = {
        key.removeprefix("formatting."): value
        for key, value in overrides.items()
        if key.startswith("formatting.")
    }
    options = {**base, **formatting_overrides}
    return FormattingOptions(
        disable_preview=options.get("disable_preview", "true").lower() == "true",
        max_length=int(options.get("max_length", "3500")),
        ellipsis=options.get("ellipsis", "…"),
        attachments_style=options.get("attachments_style", "summary"),
    )

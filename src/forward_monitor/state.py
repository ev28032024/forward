from __future__ import annotations

import json
from pathlib import Path
from typing import Dict


class MonitorState:
    """Stores progress information between monitor runs."""

    __slots__ = ("_path", "_data", "_dirty")

    def __init__(self, path: Path):
        self._path = path
        self._data = {
            "last_message_ids": {},
        }
        self._dirty = False
        self._load()

    def get_last_message_id(self, channel_id: int) -> str | None:
        return self._data["last_message_ids"].get(str(channel_id))

    def update_last_message_id(self, channel_id: int, message_id: str) -> None:
        key = str(channel_id)
        if self._data["last_message_ids"].get(key) == message_id:
            return
        self._data["last_message_ids"][key] = message_id
        self._dirty = True

    def save(self) -> None:
        if not self._dirty:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_name(self._path.name + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as file:
            json.dump(self._data, file, indent=2)
        try:
            tmp_path.replace(self._path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        self._dirty = False

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with self._path.open("r", encoding="utf-8") as file:
                data: Dict[str, dict[str, list[str]]] = json.load(file)
        except json.JSONDecodeError:
            # Corrupted file - start fresh but keep backup of original contents.
            backup_path = self._path.with_suffix(".bak")
            if backup_path.exists():
                backup_path.unlink()
            self._path.rename(backup_path)
            return

        if not isinstance(data, dict):
            return

        self._data["last_message_ids"].update(
            {
                str(key): str(value)
                for key, value in data.get("last_message_ids", {}).items()
                if value
            }
        )

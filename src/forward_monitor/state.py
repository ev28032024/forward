from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, List


class MonitorState:
    """Stores progress information between monitor runs."""

    def __init__(self, path: Path):
        self._path = path
        self._data = {
            "last_message_ids": {},
            "pinned_message_ids": {},
        }
        self._load()

    def get_last_message_id(self, channel_id: int) -> str | None:
        return self._data["last_message_ids"].get(str(channel_id))

    def update_last_message_id(self, channel_id: int, message_id: str) -> None:
        self._data["last_message_ids"][str(channel_id)] = message_id

    def get_known_pins(self, channel_id: int) -> List[str]:
        ids = self._data["pinned_message_ids"].get(str(channel_id), [])
        return list(ids)

    def set_known_pins(self, channel_id: int, message_ids: Iterable[str]) -> None:
        self._data["pinned_message_ids"][str(channel_id)] = list(message_ids)

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w", encoding="utf-8") as file:
            json.dump(self._data, file, indent=2)

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with self._path.open("r", encoding="utf-8") as file:
                data: Dict[str, Dict[str, List[str]]] = json.load(file)
        except json.JSONDecodeError:
            # Corrupted file - start fresh but keep backup of original contents.
            backup_path = self._path.with_suffix(".bak")
            self._path.rename(backup_path)
            return

        if not isinstance(data, dict):
            return

        self._data["last_message_ids"].update(
            {str(key): str(value) for key, value in data.get("last_message_ids", {}).items() if value}
        )
        self._data["pinned_message_ids"].update(
            {str(key): list(value) for key, value in data.get("pinned_message_ids", {}).items() if value}
        )

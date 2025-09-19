from pathlib import Path
from typing import Any, Iterable, List

import pytest

from forward_monitor.config import (
    ChannelMapping,
    MessageCustomization,
    MessageFilters,
)
from forward_monitor.monitor import ChannelContext, _sync_pins
from forward_monitor.state import MonitorState


class _StubDiscord:
    def __init__(self, pins: Iterable[dict[str, Any]]):
        self._pins: List[dict[str, Any]] = list(pins)

    async def fetch_pins(self, channel_id: int) -> List[dict[str, Any]]:
        return list(self._pins)


class _StubTelegram:
    def __init__(self) -> None:
        self.messages: List[tuple[str, str]] = []

    async def send_message(self, chat_id: str, text: str) -> None:
        self.messages.append((chat_id, text))

    async def send_photo(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("unexpected media upload")

    async def send_video(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("unexpected media upload")

    async def send_audio(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("unexpected media upload")

    async def send_document(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("unexpected media upload")


@pytest.mark.asyncio
async def test_sync_pins_handles_non_string_ids(tmp_path: Path) -> None:
    state = MonitorState(tmp_path / "state.json")
    state.set_known_pins(42, ["101"])
    state.save()

    discord = _StubDiscord(
        [
            {
                "id": 101,
                "content": "old",
                "author": {"username": "Alice", "id": "1"},
                "attachments": [],
            },
            {
                "id": 202,
                "content": "New pinned",
                "author": {"username": "Bob", "id": "2"},
                "attachments": [],
            },
        ]
    )

    telegram = _StubTelegram()

    context = ChannelContext(
        mapping=ChannelMapping(42, "@chat"),
        filters=MessageFilters(),
        customization=MessageCustomization(),
    )

    await _sync_pins([context], discord, telegram, state, min_delay=0, max_delay=0)

    assert len(telegram.messages) == 1
    chat_id, text = telegram.messages[0]
    assert chat_id == "@chat"
    assert "New pinned" in text

    assert set(state.get_known_pins(42)) == {"101", "202"}

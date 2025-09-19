from __future__ import annotations

from typing import Any, Sequence

import pytest

from forward_monitor.config import ChannelMapping, MessageCustomization, MessageFilters
from forward_monitor.formatter import AttachmentInfo, FormattedMessage, format_announcement_message
from forward_monitor.monitor import ChannelContext, _forward_message, _sync_announcements


class StubTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))


@pytest.mark.asyncio
async def test_forward_message_sends_extra_messages() -> None:
    context = ChannelContext(
        mapping=ChannelMapping(discord_channel_id=1, telegram_chat_id="chat"),
        filters=MessageFilters().prepare(),
        customization=MessageCustomization().prepare(),
    )

    message = {
        "content": "Hello",
        "author": {"id": "123"},
        "id": "456",
    }

    def formatter(
        channel_id: int,
        payload: dict,
        content: str,
        attachments: Sequence[AttachmentInfo],
        *,
        embed_text: str | None = None,
    ) -> FormattedMessage:
        assert channel_id == 1
        assert content == "Hello"
        assert payload is message
        assert list(attachments) == []
        assert embed_text == ""
        return FormattedMessage("Main", ("Extra one", "Extra two"))

    telegram = StubTelegram()

    await _forward_message(
        context=context,
        channel_id=1,
        message=message,
        telegram=telegram,
        formatter=formatter,
        min_delay=0,
        max_delay=0,
    )

    assert telegram.sent == [
        ("chat", "Main"),
        ("chat", "Extra one"),
        ("chat", "Extra two"),
    ]


@pytest.mark.asyncio
async def test_forward_message_applies_customisation_to_embed_text() -> None:
    context = ChannelContext(
        mapping=ChannelMapping(discord_channel_id=2, telegram_chat_id="room"),
        filters=MessageFilters().prepare(),
        customization=MessageCustomization(
            headers=["Header"],
            footers=["Footer"],
            replacements={"Secret": "Visible"},
        ).prepare(),
    )

    message = {
        "content": "",
        "author": {"username": "Author"},
        "guild_id": 99,
        "id": 100,
        "embeds": [
            {"description": "Secret embed text"},
        ],
    }

    telegram = StubTelegram()

    await _forward_message(
        context=context,
        channel_id=2,
        message=message,
        telegram=telegram,
        formatter=format_announcement_message,
        min_delay=0,
        max_delay=0,
    )

    assert telegram.sent, "Expected at least one message to be sent"
    forwarded = telegram.sent[0][1]
    assert "Header" in forwarded
    assert "Visible embed text" in forwarded
    assert forwarded.index("Header") > forwarded.index("канале 2 от Author")
    footer_position = forwarded.rfind("Footer")
    assert footer_position != -1
    jump_index = forwarded.find("Открыть в Discord")
    assert jump_index == -1 or footer_position < jump_index


@pytest.mark.asyncio
async def test_sync_announcements_fetches_multiple_batches() -> None:
    context = ChannelContext(
        mapping=ChannelMapping(discord_channel_id=5, telegram_chat_id="dest"),
        filters=MessageFilters().prepare(),
        customization=MessageCustomization().prepare(),
    )

    first_batch = [
        {"id": str(index), "author": {"id": "1"}, "content": f"Message {index}"}
        for index in range(1, 101)
    ]
    second_batch = [
        {"id": "101", "author": {"id": "1"}, "content": "After backlog"},
        {"id": "102", "author": {"id": "1"}, "content": "Newest"},
    ]

    class FakeDiscord:
        def __init__(self, batches: list[list[dict[str, Any]]]) -> None:
            self._batches = batches
            self.calls: list[tuple[int, str | None, int]] = []

        async def fetch_messages(
            self,
            channel_id: int,
            *,
            after: str | None = None,
            limit: int = 100,
        ) -> list[dict[str, Any]]:
            self.calls.append((channel_id, after, limit))
            if self._batches:
                return self._batches.pop(0)
            return []

    class DummyState:
        def __init__(self) -> None:
            self._values: dict[int, str] = {}

        def get_last_message_id(self, channel_id: int) -> str | None:
            return self._values.get(channel_id)

        def update_last_message_id(self, channel_id: int, message_id: str) -> None:
            self._values[channel_id] = message_id

    discord = FakeDiscord([first_batch, second_batch])
    telegram = StubTelegram()
    state = DummyState()

    await _sync_announcements(
        [context],
        discord,
        telegram,
        state,
        min_delay=0,
        max_delay=0,
    )

    # 102 messages should have been forwarded once.
    assert len(telegram.sent) == 102
    assert state._values[5] == "102"
    assert discord.calls == [
        (5, None, 100),
        (5, "100", 100),
    ]

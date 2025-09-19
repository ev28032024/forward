from __future__ import annotations

from typing import Sequence

import pytest

from forward_monitor.config import ChannelMapping, MessageCustomization, MessageFilters
from forward_monitor.formatter import AttachmentInfo, FormattedMessage
from forward_monitor.monitor import ChannelContext, _forward_message


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
    ) -> FormattedMessage:
        assert channel_id == 1
        assert content == "Hello"
        assert payload is message
        assert list(attachments) == []
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

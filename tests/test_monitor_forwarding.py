from __future__ import annotations

import asyncio
from typing import Any, Mapping, Sequence, cast

import pytest

from forward_monitor.config import (
    ChannelMapping,
    CustomisedText,
    FormattingProfile,
    MessageCustomization,
    MessageFilters,
)
from forward_monitor.formatter import AttachmentInfo, FormattedMessage, format_announcement_message
from forward_monitor.monitor import (
    AnnouncementFormatter,
    ChannelContext,
    _forward_message,
    _sync_announcements,
)
from forward_monitor.types import DiscordMessage


class StubTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        parse_mode: str | None = None,
        disable_web_page_preview: bool | None = None,
    ) -> None:
        self.sent.append((chat_id, text))

    async def send_photo(
        self,
        chat_id: str,
        photo: str,
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
    ) -> None:
        self.sent.append((chat_id, f"photo:{photo}"))
        if caption:
            self.sent.append((chat_id, caption))

    async def send_video(
        self,
        chat_id: str,
        video: str,
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
    ) -> None:
        self.sent.append((chat_id, f"video:{video}"))
        if caption:
            self.sent.append((chat_id, caption))

    async def send_audio(
        self,
        chat_id: str,
        audio: str,
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
    ) -> None:
        self.sent.append((chat_id, f"audio:{audio}"))
        if caption:
            self.sent.append((chat_id, caption))

    async def send_document(
        self,
        chat_id: str,
        document: str,
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
    ) -> None:
        self.sent.append((chat_id, f"document:{document}"))
        if caption:
            self.sent.append((chat_id, caption))


class DummyState:
    def __init__(self) -> None:
        self._values: dict[int, str] = {}

    def get_last_message_id(self, channel_id: int) -> str | None:
        return self._values.get(channel_id)

    def update_last_message_id(self, channel_id: int, message_id: str) -> None:
        self._values[channel_id] = message_id


@pytest.mark.asyncio
async def test_forward_message_sends_extra_messages() -> None:
    context = ChannelContext(
        mapping=ChannelMapping(discord_channel_id=1, telegram_chat_id="chat"),
        filters=MessageFilters().prepare(),
        customization=MessageCustomization().prepare(),
        formatting=FormattingProfile(),
    )

    message = cast(
        DiscordMessage,
        {
            "content": "Hello",
            "author": {"id": "123"},
            "id": "456",
        },
    )

    def formatter(
        channel_id: int,
        payload: Mapping[str, Any],
        content: CustomisedText,
        attachments: Sequence[AttachmentInfo],
        *,
        embed_text: str | None = None,
        channel_label: str | None = None,
        formatting: FormattingProfile | None = None,
    ) -> FormattedMessage:
        assert channel_id == 1
        assert content.body_lines == ("Hello",)
        assert payload is message
        assert list(attachments) == []
        assert embed_text == ""
        assert channel_label is None
        return FormattedMessage("Main", ("Extra one", "Extra two"))

    telegram = StubTelegram()

    await _forward_message(
        context=context,
        channel_id=1,
        message=message,
        telegram=telegram,
        formatter=cast(AnnouncementFormatter, formatter),
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
            headers=("Header",),
            footers=("Footer",),
            replacements=(("Secret", "Visible"),),
        ).prepare(),
        formatting=FormattingProfile(),
    )

    message = cast(
        DiscordMessage,
        {
            "content": "",
            "author": {"username": "Author"},
            "guild_id": 99,
            "id": 100,
            "embeds": [
                {"description": "Secret embed text"},
            ],
        },
    )

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
    lines = forwarded.splitlines()
    assert lines[0] == "📢 2 · Author"
    assert "Header" in lines
    assert "Visible embed text" in lines
    assert "Footer" in lines
    assert lines[-1].startswith("Открыть в Discord:")


@pytest.mark.asyncio
async def test_sync_announcements_fetches_multiple_batches() -> None:
    context = ChannelContext(
        mapping=ChannelMapping(discord_channel_id=5, telegram_chat_id="dest"),
        filters=MessageFilters().prepare(),
        customization=MessageCustomization().prepare(),
        formatting=FormattingProfile(),
    )

    first_batch: list[DiscordMessage] = [
        cast(
            DiscordMessage,
            {"id": str(index), "author": {"id": "1"}, "content": f"Message {index}"},
        )
        for index in range(1, 101)
    ]
    second_batch: list[DiscordMessage] = [
        cast(
            DiscordMessage,
            {"id": "101", "author": {"id": "1"}, "content": "After backlog"},
        ),
        cast(
            DiscordMessage,
            {"id": "102", "author": {"id": "1"}, "content": "Newest"},
        ),
    ]

    class FakeDiscord:
        def __init__(self, batches: list[list[DiscordMessage]]) -> None:
            self._batches = batches
            self.calls: list[tuple[int, str | None, int]] = []

        async def fetch_messages(
            self,
            channel_id: int,
            *,
            after: str | None = None,
            limit: int = 100,
        ) -> list[DiscordMessage]:
            self.calls.append((channel_id, after, limit))
            if self._batches:
                return self._batches.pop(0)
            return []

    discord = FakeDiscord([first_batch, second_batch])
    telegram = StubTelegram()
    state = DummyState()

    fetched, forwarded_count = await _sync_announcements(
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
    assert fetched == 102
    assert forwarded_count == 102
    assert discord.calls == [
        (5, None, 100),
        (5, "100", 100),
    ]


@pytest.mark.asyncio
async def test_sync_announcements_fetches_channels_in_parallel() -> None:
    slow_context = ChannelContext(
        mapping=ChannelMapping(
            discord_channel_id=11,
            telegram_chat_id="slow_chat",
            display_name="Slow Channel",
        ),
        filters=MessageFilters().prepare(),
        customization=MessageCustomization().prepare(),
        formatting=FormattingProfile(),
    )
    fast_context = ChannelContext(
        mapping=ChannelMapping(discord_channel_id=12, telegram_chat_id="fast_chat"),
        filters=MessageFilters().prepare(),
        customization=MessageCustomization().prepare(),
        formatting=FormattingProfile(),
    )

    slow_started = asyncio.Event()
    fast_completed = asyncio.Event()
    release_slow = asyncio.Event()

    class ParallelDiscord:
        def __init__(self) -> None:
            self.calls: list[int] = []

        async def fetch_messages(
            self,
            channel_id: int,
            *,
            after: str | None = None,
            limit: int = 100,
        ) -> list[DiscordMessage]:
            self.calls.append(channel_id)
            if channel_id == 11:
                slow_started.set()
                await release_slow.wait()
                return [
                    cast(
                        DiscordMessage,
                        {"id": "21", "author": {"id": "1"}, "content": "slow news"},
                    )
                ]
            await slow_started.wait()
            fast_completed.set()
            return [
                cast(
                    DiscordMessage,
                    {"id": "12", "author": {"id": "2"}, "content": "fast update"},
                )
            ]

    discord = ParallelDiscord()
    telegram = StubTelegram()
    state = DummyState()

    sync_task = asyncio.create_task(
        _sync_announcements(
            [slow_context, fast_context],
            discord,
            telegram,
            state,
            min_delay=0,
            max_delay=0,
        )
    )

    await slow_started.wait()
    await fast_completed.wait()
    assert not sync_task.done(), "Expected slow fetch to still be pending"

    release_slow.set()
    fetched, forwarded_count = await sync_task

    assert {call for call in discord.calls} == {11, 12}
    assert any("slow news" in text for _, text in telegram.sent)
    assert any("fast update" in text for _, text in telegram.sent)
    assert state._values[11] == "21"
    assert state._values[12] == "12"
    assert fetched == 2
    assert forwarded_count == 2


@pytest.mark.asyncio
async def test_sync_announcements_skips_failed_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = ChannelContext(
        mapping=ChannelMapping(discord_channel_id=7, telegram_chat_id="chat"),
        filters=MessageFilters().prepare(),
        customization=MessageCustomization().prepare(),
        formatting=FormattingProfile(),
    )

    messages: list[DiscordMessage] = [
        cast(DiscordMessage, {"id": "31", "author": {"id": "1"}, "content": "first"}),
        cast(DiscordMessage, {"id": "32", "author": {"id": "1"}, "content": "second"}),
    ]

    class FakeDiscord:
        async def fetch_messages(
            self,
            channel_id: int,
            *,
            after: str | None = None,
            limit: int = 100,
        ) -> list[DiscordMessage]:
            return messages

    forwarded: list[str] = []
    failures: list[str] = []
    error_triggered = False

    async def failing_forward(**kwargs: Any) -> bool:
        nonlocal error_triggered
        message = kwargs["message"]
        if not error_triggered:
            error_triggered = True
            failures.append(message["id"])
            raise RuntimeError("boom")
        forwarded.append(message["content"])
        return True

    monkeypatch.setattr("forward_monitor.monitor._forward_message", failing_forward)

    telegram = StubTelegram()
    state = DummyState()

    fetched, forwarded_count = await _sync_announcements(
        [context],
        FakeDiscord(),
        telegram,
        state,
        min_delay=0,
        max_delay=0,
    )

    assert forwarded == ["second"]
    assert failures == ["31"]
    assert state._values[7] == "32"
    assert fetched == 2
    assert forwarded_count == 1


@pytest.mark.asyncio
async def test_sync_announcements_counts_only_forwarded_messages() -> None:
    context = ChannelContext(
        mapping=ChannelMapping(discord_channel_id=8, telegram_chat_id="chat"),
        filters=MessageFilters(whitelist=("keep",)).prepare(),
        customization=MessageCustomization().prepare(),
        formatting=FormattingProfile(),
    )

    class FilteringDiscord:
        async def fetch_messages(
            self,
            channel_id: int,
            *,
            after: str | None = None,
            limit: int = 100,
        ) -> list[DiscordMessage]:
            return [
                cast(
                    DiscordMessage,
                    {"id": "41", "author": {"id": "1"}, "content": "ignore"},
                ),
                cast(
                    DiscordMessage,
                    {"id": "42", "author": {"id": "1"}, "content": "keep me"},
                ),
            ]

    telegram = StubTelegram()
    state = DummyState()

    fetched, forwarded_count = await _sync_announcements(
        [context],
        FilteringDiscord(),
        telegram,
        state,
        min_delay=0,
        max_delay=0,
    )

    assert fetched == 2
    assert forwarded_count == 1
    assert state._values[8] == "42"
    assert any("keep me" in text for _, text in telegram.sent)
    assert all("ignore" not in text for _, text in telegram.sent)

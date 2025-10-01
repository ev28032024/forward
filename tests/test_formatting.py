from __future__ import annotations

from forward_monitor.formatting import format_discord_message
from forward_monitor.models import ChannelConfig, DiscordMessage, FilterConfig, FormattingOptions


def sample_channel(**overrides: object) -> ChannelConfig:
    formatting = FormattingOptions()
    channel = ChannelConfig(
        discord_id="123",
        telegram_chat_id="456",
        telegram_thread_id=None,
        label="Label",
        formatting=formatting,
        filters=FilterConfig(),
        last_message_id=None,
        storage_id=1,
    )
    for key, value in overrides.items():
        setattr(channel, key, value)
    return channel


def test_formatting_includes_label_and_author() -> None:
    message = DiscordMessage(
        id="1",
        channel_id="123",
        author_id="99",
        author_name="Author",
        content="original content",
        attachments=(
            {
                "url": "https://example.com/file.txt",
                "filename": "file.txt",
                "size": 1024,
            },
        ),
        embeds=(),
        stickers=(),
        role_ids=set(),
    )
    formatted = format_discord_message(message, sample_channel())
    assert formatted.parse_mode == "HTML"
    assert formatted.text.startswith("<b>Label</b> • <b>Author</b>")
    assert "original content" in formatted.text
    assert "file.txt" in formatted.text


def test_formatting_chunks_long_text() -> None:
    channel = sample_channel()
    channel.formatting.max_length = 50
    message = DiscordMessage(
        id="1",
        channel_id="123",
        author_id="99",
        author_name="Author",
        content="long text " * 20,
        attachments=(),
        embeds=(),
        stickers=(),
        role_ids=set(),
    )
    formatted = format_discord_message(message, channel)
    assert len(formatted.extra_messages) >= 1


def test_channel_mentions_converted_in_content() -> None:
    channel = sample_channel()
    message = DiscordMessage(
        id="2",
        channel_id="123",
        author_id="99",
        author_name="Author",
        content="See <#1234567890> for details",
        attachments=(),
        embeds=(),
        stickers=(),
        role_ids=set(),
    )

    formatted = format_discord_message(message, channel)

    assert formatted.parse_mode == "HTML"
    assert "#1234567890" in formatted.text
    assert "See" in formatted.text

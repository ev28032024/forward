from __future__ import annotations

from forward_monitor.formatting import format_discord_message
from forward_monitor.models import (
    ChannelConfig,
    DiscordMessage,
    FilterConfig,
    FormattingOptions,
    ReplacementRule,
)


def sample_channel(**overrides: object) -> ChannelConfig:
    formatting = FormattingOptions(header="Header", footer="Footer", chip="Chip")
    channel = ChannelConfig(
        discord_id="123",
        telegram_chat_id="456",
        label="Label",
        formatting=formatting,
        filters=FilterConfig(),
        replacements=(ReplacementRule(pattern="foo", replacement="bar"),),
        last_message_id=None,
        storage_id=1,
    )
    for key, value in overrides.items():
        setattr(channel, key, value)
    return channel


def test_formatting_includes_header_footer_and_chip() -> None:
    message = DiscordMessage(
        id="1",
        channel_id="123",
        author_id="99",
        author_name="Author",
        content="foo content",
        attachments=(
            {
                "url": "https://example.com/file.txt",
                "filename": "file.txt",
                "size": 1024,
            },
        ),
        embeds=(),
    )
    formatted = format_discord_message(message, sample_channel())
    body = formatted.text.replace("\\", "")
    assert "Header" in body
    assert "Footer" in body
    assert "Label" in body
    assert "bar content" in body
    assert "file.txt" in body


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
    )
    formatted = format_discord_message(message, channel)
    assert len(formatted.extra_messages) >= 1

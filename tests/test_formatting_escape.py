from __future__ import annotations

from forward_monitor.formatting import format_discord_message
from forward_monitor.models import ChannelConfig, DiscordMessage, FilterConfig, FormattingOptions


def test_markdown_escape_preserves_special_chars() -> None:
    channel = ChannelConfig(
        discord_id="1",
        telegram_chat_id="2",
        label="Test",
        formatting=FormattingOptions(parse_mode="MarkdownV2"),
        filters=FilterConfig(),
        replacements=(),
        last_message_id=None,
        storage_id=1,
    )
    message = DiscordMessage(
        id="1",
        channel_id="1",
        author_id="1",
        author_name="User",
        content="*bold* _italic_ [link](url)",
        attachments=(),
        embeds=(),
    )
    formatted = format_discord_message(message, channel)
    assert "\\*bold\\*" in formatted.text
    assert "\\_italic\\_" in formatted.text

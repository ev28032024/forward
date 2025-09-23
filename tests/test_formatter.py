from __future__ import annotations

from typing import cast

from forward_monitor.config import CustomisedText
from forward_monitor.formatter import (
    AttachmentInfo,
    FormattedMessage,
    build_attachments,
    clean_discord_content,
    extract_embed_text,
    format_announcement_message,
)
from forward_monitor.types import DiscordMessage


def test_clean_discord_content_preserves_literal_underscores() -> None:
    message = cast(DiscordMessage, {"content": "release_candidate"})

    assert clean_discord_content(message) == "release_candidate"


def test_clean_discord_content_preserves_literal_backticks() -> None:
    message = cast(DiscordMessage, {"content": "literal`backtick"})

    assert clean_discord_content(message) == "literal`backtick"


def test_clean_discord_content_strips_markdown_wrappers() -> None:
    message = cast(
        DiscordMessage, {"content": "Look at **bold** and `code` with _italics_"}
    )

    assert clean_discord_content(message) == "Look at bold and code with italics"


def test_clean_discord_content_preserves_indentation() -> None:
    message = cast(
        DiscordMessage,
        {"content": "Line one\n  - Nested item\n    Code block"},
    )

    assert clean_discord_content(message) == "Line one\n  - Nested item\n    Code block"


def test_format_includes_embed_data() -> None:
    message = {
        "author": {"username": "Tester"},
        "guild_id": 123,
        "id": 456,
        "embeds": [
            {
                "title": "Update title",
                "description": "Details with _markdown_",
                "fields": [
                    {"name": "Field", "value": "Some `value`"},
                ],
                "footer": {"text": "Footer text"},
                "author": {"name": "Embed Author"},
            }
        ],
    }

    result = format_announcement_message(789, message, "", [])

    assert isinstance(result, FormattedMessage)
    assert "Update title" in result.text
    assert "Details with markdown" in result.text
    assert "Field: Some value" in result.text
    assert "Footer text" in result.text
    assert "Embed Author" in result.text
    assert result.extra_messages == ()


def test_extract_embed_text_returns_clean_text() -> None:
    message = cast(
        DiscordMessage,
        {
            "embeds": [
                {
                    "title": "  Title  ",
                    "description": "`Code` block",
                    "url": "https://example.com/details",
                }
            ]
        },
    )

    text = extract_embed_text(message)
    assert "Title" in text
    assert "Code block" in text
    assert "https://example.com/details" in text


def test_format_announcement_uses_channel_label() -> None:
    message = cast(DiscordMessage, {"author": {"username": "Tester"}, "id": 999})

    result = format_announcement_message(
        123,
        message,
        "Body",
        [],
        channel_label="Announcements",
    )

    first_line = result.text.splitlines()[0]
    assert first_line.startswith("📢 Announcements")
    assert "Tester" in first_line
    assert "123" not in first_line


def test_format_does_not_include_jump_url() -> None:
    message = {
        "author": {"username": "Tester"},
        "id": 555,
        "guild_id": 321,
    }

    formatted = format_announcement_message(888, message, "Body", [])

    assert "discord.com/channels" not in formatted.text
    for extra in formatted.extra_messages:
        assert "discord.com/channels" not in extra


def test_build_attachments_includes_embed_urls() -> None:
    message = cast(
        DiscordMessage,
        {
            "attachments": [
                {
                    "url": "https://example.com/file.txt",
                    "filename": "file.txt",
                    "content_type": "text/plain",
                }
            ],
            "embeds": [
                {
                    "image": {"url": "https://example.com/image.png"},
                    "thumbnail": {"url": "https://example.com/thumb.png"},
                    "video": {"url": "https://example.com/video.mp4"},
                    "provider": {"url": "https://example.com/provider"},
                }
            ],
        },
    )

    attachments = build_attachments(message)
    urls = {attachment.url for attachment in attachments}
    assert "https://example.com/file.txt" in urls
    assert "https://example.com/image.png" in urls
    assert "https://example.com/thumb.png" in urls
    assert "https://example.com/video.mp4" in urls
    assert "https://example.com/provider" in urls
    assert len(urls) == len(attachments)


def test_format_keeps_small_attachment_summary_inline() -> None:
    message = cast(
        DiscordMessage, {"author": {"username": "Tester"}, "guild_id": 1, "id": 2}
    )
    attachments = [
        AttachmentInfo(
            url="https://example.com/file.txt",
            filename="file.txt",
            domain="example.com",
        )
    ]

    result = format_announcement_message(3, message, "Body", attachments)

    assert "Вложения: 1" in result.text
    assert "example.com" in result.text
    assert result.extra_messages == ()


def test_format_splits_oversized_attachment_summary() -> None:
    message = cast(
        DiscordMessage, {"author": {"username": "Tester"}, "guild_id": 1, "id": 2}
    )
    attachments = [
        AttachmentInfo(
            url=f"https://host{index}.example.com/file{index}.txt",
            filename=f"file{index}.txt",
            domain=f"host{index}.example.com",
        )
        for index in range(6)
    ]

    result = format_announcement_message(3, message, "Body", attachments)

    assert "Вложения: 6" in result.text
    assert "host0.example.com" in result.text
    assert "+3" in result.text
    assert result.extra_messages == ()


def test_format_removes_duplicate_lines() -> None:
    message = cast(DiscordMessage, {"author": {"username": "Tester"}, "id": 7})
    customised = CustomisedText(
        chips=("🔥",),
        header_lines=("Заголовок", "заголовок"),
        body_lines=("Повтор", "Повтор", "", "Основной текст", "Основной текст"),
        footer_lines=("Финал", "Финал"),
    )

    result = format_announcement_message(123, message, customised, [])
    lines = [line for line in result.text.splitlines() if line and not line.startswith("📢 ")]

    assert lines.count("Заголовок") == 1
    assert lines.count("Повтор") == 1
    assert lines.count("Основной текст") == 1
    assert lines.count("Финал") == 1

from __future__ import annotations

from forward_monitor.formatter import (
    AttachmentInfo,
    FormattedMessage,
    build_attachments,
    clean_discord_content,
    extract_embed_text,
    format_announcement_message,
)


def test_clean_discord_content_preserves_literal_underscores() -> None:
    message = {"content": "release_candidate"}

    assert clean_discord_content(message) == "release_candidate"


def test_clean_discord_content_preserves_literal_backticks() -> None:
    message = {"content": "literal`backtick"}

    assert clean_discord_content(message) == "literal`backtick"


def test_clean_discord_content_strips_markdown_wrappers() -> None:
    message = {"content": "Look at **bold** and `code` with _italics_"}

    assert (
        clean_discord_content(message)
        == "Look at bold and code with italics"
    )


def test_clean_discord_content_preserves_indentation() -> None:
    message = {
        "content": "Line one\n  - Nested item\n    Code block",
    }

    assert (
        clean_discord_content(message)
        == "Line one\n  - Nested item\n    Code block"
    )


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
    message = {
        "embeds": [
            {
                "title": "  Title  ",
                "description": "`Code` block",
            }
        ]
    }

    text = extract_embed_text(message)
    assert "Title" in text
    assert "Code block" in text


def test_build_attachments_includes_embed_urls() -> None:
    message = {
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
    }

    attachments = build_attachments(message)
    urls = {attachment.url for attachment in attachments}
    assert "https://example.com/file.txt" in urls
    assert "https://example.com/image.png" in urls
    assert "https://example.com/thumb.png" in urls
    assert "https://example.com/video.mp4" in urls
    assert "https://example.com/provider" in urls
    assert len(urls) == len(attachments)


def test_format_keeps_small_attachment_summary_inline() -> None:
    message = {
        "author": {"username": "Tester"},
        "guild_id": 1,
        "id": 2,
    }
    attachments = [
        AttachmentInfo(
            url="https://example.com/file.txt",
            filename="file.txt",
        )
    ]

    result = format_announcement_message(3, message, "Body", attachments)

    assert "Вложения:" in result.text
    assert attachments[0].display_label() in result.text
    assert result.extra_messages == ()


def test_format_splits_oversized_attachment_summary() -> None:
    message = {
        "author": {"username": "Tester"},
        "guild_id": 1,
        "id": 2,
    }
    long_name = "a" * 500
    attachments = [
        AttachmentInfo(
            url=f"https://example.com/{index}",
            filename=f"{long_name}_{index}",
        )
        for index in range(12)
    ]

    result = format_announcement_message(3, message, "Body", attachments)

    assert len(result.text) <= 4096
    assert result.extra_messages
    assert all(len(chunk) <= 4096 for chunk in result.extra_messages)

    combined = "\n".join([result.text, *result.extra_messages])
    assert "Вложения:" in combined
    for attachment in attachments:
        assert attachment.display_label() in combined

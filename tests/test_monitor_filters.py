from __future__ import annotations

from forward_monitor.formatter import AttachmentInfo, build_attachments, clean_discord_content, extract_embed_text
from forward_monitor.monitor import _attachment_category, _message_types, _should_forward
from forward_monitor.config import MessageFilters


def test_attachment_category_supports_file_alias() -> None:
    attachment = AttachmentInfo(
        url="https://example.com/document.pdf",
        filename="document.pdf",
        content_type="application/pdf",
    )

    assert _attachment_category(attachment) == "file"


def test_should_forward_respects_file_alias_in_filters() -> None:
    attachment = AttachmentInfo(
        url="https://example.com/archive.zip",
        filename="archive.zip",
        content_type=None,
    )

    filters = MessageFilters(allowed_types=["file"]).prepare()
    message = {"author": {"id": "42"}}

    categories = [_attachment_category(attachment)]
    assert _should_forward(message, "", "", [attachment], categories, filters)


def test_message_types_include_document_alias() -> None:
    attachment = AttachmentInfo(
        url="https://example.com/archive.zip",
        filename="archive.zip",
        content_type=None,
    )

    categories = [_attachment_category(attachment)]
    types = _message_types("", "", [attachment], categories)

    assert "file" in types
    assert "document" in types


def test_should_forward_whitelist_matches_embed_text() -> None:
    message = {
        "content": "",
        "author": {"id": "99"},
        "embeds": [
            {
                "description": "Important Keyword present",
            }
        ],
    }

    filters = MessageFilters(whitelist=["keyword"]).prepare()
    attachments: list[AttachmentInfo] = []
    categories: list[str] = []
    embed_text = extract_embed_text(message)

    assert _should_forward(
        message,
        clean_discord_content(message),
        embed_text,
        attachments,
        categories,
        filters,
    )


def test_should_forward_allowed_types_accepts_embed_text() -> None:
    message = {
        "content": "",
        "author": {"id": "77"},
        "embeds": [
            {
                "description": "Only embed content",
            }
        ],
    }

    filters = MessageFilters(allowed_types=["text"]).prepare()
    embed_text = extract_embed_text(message)
    attachments: list[AttachmentInfo] = []
    categories: list[str] = []

    assert _should_forward(
        message,
        clean_discord_content(message),
        embed_text,
        attachments,
        categories,
        filters,
    )


def test_should_forward_allows_embed_image_attachment() -> None:
    message = {
        "content": "",
        "author": {"id": "55"},
        "embeds": [
            {
                "image": {"url": "https://example.com/picture.png"},
            }
        ],
    }

    filters = MessageFilters(allowed_types=["image"]).prepare()
    attachments = build_attachments(message)
    categories = [_attachment_category(attachment) for attachment in attachments]

    assert _should_forward(
        message,
        clean_discord_content(message),
        extract_embed_text(message),
        attachments,
        categories,
        filters,
    )

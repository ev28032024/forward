from __future__ import annotations

from forward_monitor.formatter import AttachmentInfo
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

    filters = MessageFilters(allowed_types=["file"])
    message = {"author": {"id": "42"}}

    assert _should_forward(message, "", [attachment], filters)


def test_message_types_include_document_alias() -> None:
    attachment = AttachmentInfo(
        url="https://example.com/archive.zip",
        filename="archive.zip",
        content_type=None,
    )

    types = _message_types("", [attachment])

    assert "file" in types
    assert "document" in types

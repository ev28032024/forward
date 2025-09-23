from __future__ import annotations

import logging
from typing import cast

import pytest

from forward_monitor.config import (
    ChannelMapping,
    FormattingProfile,
    MessageCustomization,
    MessageFilters,
)
from forward_monitor.formatter import (
    AttachmentInfo,
    FormattedMessage,
    build_attachments,
    clean_discord_content,
    extract_embed_text,
)
from forward_monitor.monitor import (
    ChannelContext,
    _attachment_category,
    _forward_message,
    _message_types,
    _should_forward,
)
from forward_monitor.types import DiscordMessage


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

    filters = MessageFilters(allowed_types=("file",)).prepare()
    message = {"author": {"id": "42"}}

    categories = [_attachment_category(attachment)]
    allowed, reason = _should_forward(message, "", "", [attachment], categories, filters)
    assert allowed
    assert reason is None


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
    message = cast(
        DiscordMessage,
        {
            "content": "",
            "author": {"id": "99"},
            "embeds": [
                {
                    "description": "Important Keyword present",
                }
            ],
        },
    )

    filters = MessageFilters(whitelist=("keyword",)).prepare()
    attachments: list[AttachmentInfo] = []
    categories: list[str] = []
    embed_text = extract_embed_text(message)

    allowed, reason = _should_forward(
        message,
        clean_discord_content(message),
        embed_text,
        attachments,
        categories,
        filters,
    )
    assert allowed
    assert reason is None


def test_should_forward_allowed_types_accepts_embed_text() -> None:
    message = cast(
        DiscordMessage,
        {
            "content": "",
            "author": {"id": "77"},
            "embeds": [
                {
                    "description": "Only embed content",
                }
            ],
        },
    )

    filters = MessageFilters(allowed_types=("text",)).prepare()
    embed_text = extract_embed_text(message)
    attachments: list[AttachmentInfo] = []
    categories: list[str] = []

    allowed, reason = _should_forward(
        message,
        clean_discord_content(message),
        embed_text,
        attachments,
        categories,
        filters,
    )
    assert allowed
    assert reason is None


def test_should_forward_allows_embed_image_attachment() -> None:
    message = cast(
        DiscordMessage,
        {
            "content": "",
            "author": {"id": "55"},
            "embeds": [
                {
                    "image": {"url": "https://example.com/picture.png"},
                }
            ],
        },
    )

    filters = MessageFilters(allowed_types=("image",)).prepare()
    attachments = build_attachments(message)
    categories = [_attachment_category(attachment) for attachment in attachments]

    allowed, reason = _should_forward(
        message,
        clean_discord_content(message),
        extract_embed_text(message),
        attachments,
        categories,
        filters,
    )
    assert allowed
    assert reason is None


def test_whitelist_matches_attachment_domain() -> None:
    attachment = AttachmentInfo(url="https://cdn.example.net/path/to/file.png")
    filters = MessageFilters(whitelist=("cdn.example.net",)).prepare()
    categories = [_attachment_category(attachment)]
    allowed, reason = _should_forward(
        cast(DiscordMessage, {"author": {"id": "1"}}),
        "",
        "",
        [attachment],
        categories,
        filters,
    )
    assert allowed
    assert reason is None


def test_whitelist_uses_attachment_url_when_domain_missing() -> None:
    attachment = AttachmentInfo(
        url="https://static.example.org/files/manual.pdf", domain=None
    )
    filters = MessageFilters(whitelist=("static.example.org",)).prepare()
    categories = [_attachment_category(attachment)]
    allowed, reason = _should_forward(
        cast(DiscordMessage, {"author": {"id": "1"}}),
        "",
        "",
        [attachment],
        categories,
        filters,
    )
    assert allowed
    assert reason is None


def test_allowed_senders_accepts_member_nick() -> None:
    filters = MessageFilters(allowed_senders=("Project Lead",)).prepare()
    message = cast(
        DiscordMessage,
        {"author": {"id": "2", "username": "user"}, "member": {"nick": "Project Lead"}},
    )
    allowed, reason = _should_forward(
        message,
        "",
        "",
        [],
        [],
        filters,
    )
    assert allowed
    assert reason is None


@pytest.mark.asyncio
async def test_forward_message_logs_filter_reason(caplog: pytest.LogCaptureFixture) -> None:
    context = ChannelContext(
        mapping=ChannelMapping(discord_channel_id=10, telegram_chat_id="chat"),
        filters=MessageFilters(blacklist=("deny",)).prepare(),
        customization=MessageCustomization().prepare(),
        formatting=FormattingProfile(),
    )

    message = cast(
        DiscordMessage,
        {"id": "123", "author": {"id": "42"}, "content": "should deny"},
    )

    class DummyTelegram:
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
            raise AssertionError("send_photo should not be called")

        async def send_video(
            self,
            chat_id: str,
            video: str,
            *,
            caption: str | None = None,
            parse_mode: str | None = None,
        ) -> None:
            raise AssertionError("send_video should not be called")

        async def send_audio(
            self,
            chat_id: str,
            audio: str,
            *,
            caption: str | None = None,
            parse_mode: str | None = None,
        ) -> None:
            raise AssertionError("send_audio should not be called")

        async def send_document(
            self,
            chat_id: str,
            document: str,
            *,
            caption: str | None = None,
            parse_mode: str | None = None,
        ) -> None:
            raise AssertionError("send_document should not be called")

    telegram = DummyTelegram()

    def unused_formatter(
        *_args: object,
        **_kwargs: object,
    ) -> FormattedMessage:  # pragma: no cover - formatter unused
        raise RuntimeError("formatter should not be called")

    with caplog.at_level(logging.DEBUG, logger="forward_monitor.monitor"):
        await _forward_message(
            context=context,
            channel_id=10,
            message=message,
            telegram=telegram,
            formatter=unused_formatter,
            min_delay=0,
            max_delay=0,
        )

    assert not telegram.sent
    assert "blacklist" in caplog.text

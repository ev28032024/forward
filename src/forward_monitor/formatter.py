"""Helpers for formatting Discord messages for Telegram forwarding."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any, Iterable, List, Mapping, Sequence

__all__ = [
    "AttachmentInfo",
    "FormattedMessage",
    "format_announcement_message",
    "build_attachments",
    "clean_discord_content",
]

TELEGRAM_MAX_LENGTH = 4096

MENTION_PATTERN = re.compile(r"<@!?(\d+)>")
ROLE_PATTERN = re.compile(r"<@&(\d+)>")
CHANNEL_PATTERN = re.compile(r"<#(\d+)>")


@dataclass(slots=True)
class AttachmentInfo:
    """Simplified representation of a Discord attachment."""

    url: str
    filename: str | None = None
    content_type: str | None = None
    size: int | None = None

    def display_label(self) -> str:
        """Return a human-friendly label for the attachment."""

        if self.filename:
            return f"{self.filename}: {self.url}"
        return self.url


@dataclass(slots=True)
class FormattedMessage:
    """Container describing the textual parts of a forwarded message."""

    text: str
    extra_messages: Sequence[str] = ()


def format_announcement_message(
    channel_id: int,
    message: Mapping[str, Any],
    content: str,
    attachments: Sequence[AttachmentInfo],
) -> FormattedMessage:
    """Build the outgoing text for a regular Discord message."""

    author_name = _author_name(message)
    prefix = f"📢 Новое сообщение в канале {channel_id} от {author_name}"
    embed_text = _format_embeds(message)
    combined_content = _combine_content_sections([content, embed_text])
    jump_url = _build_jump_url(message, channel_id)
    return _compose_message(prefix, combined_content, attachments, jump_url)


def _compose_message(
    prefix: str,
    content: str,
    attachments: Sequence[AttachmentInfo],
    jump_url: str | None,
) -> FormattedMessage:
    """Compose the multi-line text that will be forwarded to Telegram."""

    lines: List[str] = [prefix]
    if content:
        lines.extend(["", content])

    base_text = "\n".join(lines)
    messages = _chunk_text(base_text, TELEGRAM_MAX_LENGTH)

    attachment_block: str | None = None
    if attachments:
        attachment_lines = ["Вложения:"] + _attachment_lines(attachments)
        attachment_block = "\n".join(attachment_lines)

    if attachment_block:
        appended = False
        for index in range(len(messages) - 1, -1, -1):
            candidate = _append_section(messages[index], attachment_block)
            if len(candidate) <= TELEGRAM_MAX_LENGTH:
                messages[index] = candidate
                appended = True
                break
        if not appended:
            messages.extend(_chunk_text(attachment_block, TELEGRAM_MAX_LENGTH))

    if jump_url:
        jump_line = f"Открыть в Discord: {jump_url}"
        appended = False
        for index in range(len(messages) - 1, -1, -1):
            candidate = _append_section(messages[index], jump_line)
            if len(candidate) <= TELEGRAM_MAX_LENGTH:
                messages[index] = candidate
                appended = True
                break
        if not appended:
            messages.extend(_chunk_text(jump_line, TELEGRAM_MAX_LENGTH))

    main_text = messages[0]
    extra_messages = tuple(messages[1:])
    return FormattedMessage(main_text, extra_messages)


def _append_section(base: str, section: str) -> str:
    if not base:
        return section
    if not section:
        return base
    if base.endswith("\n"):
        separator = "\n"
    else:
        separator = "\n\n"
    return f"{base}{separator}{section}"


def _chunk_text(text: str, limit: int) -> List[str]:
    if limit <= 0:
        return [text]

    lines = text.split("\n")
    chunks: List[str] = []
    current_lines: List[str] = []
    current_length = 0

    def flush() -> None:
        nonlocal current_lines, current_length
        if current_lines:
            chunks.append("\n".join(current_lines))
            current_lines = []
            current_length = 0

    for line in lines:
        line_length = len(line)
        if line_length > limit:
            flush()
            start = 0
            while start < line_length:
                end = min(start + limit, line_length)
                chunks.append(line[start:end])
                start = end
            continue

        projected = line_length if not current_lines else current_length + 1 + line_length
        if projected > limit:
            flush()

        current_lines.append(line)
        current_length = line_length if len(current_lines) == 1 else current_length + 1 + line_length

    flush()

    if not chunks:
        return [""]
    return chunks


def _attachment_lines(attachments: Iterable[AttachmentInfo]) -> List[str]:
    """Represent attachments as a list of human-readable lines."""

    lines: List[str] = []
    for attachment in attachments:
        if attachment.url:
            lines.append(attachment.display_label())
    return lines


def build_attachments(message: Mapping[str, Any]) -> List[AttachmentInfo]:
    """Convert the raw Discord payload into AttachmentInfo objects."""

    attachments: List[AttachmentInfo] = []
    for raw in message.get("attachments", []):
        url = raw.get("url")
        if not url:
            continue
        attachments.append(
            AttachmentInfo(
                url=url,
                filename=raw.get("filename"),
                content_type=raw.get("content_type"),
                size=raw.get("size"),
            )
        )
    return attachments


def clean_discord_content(message: Mapping[str, Any]) -> str:
    """Normalise Discord message text for forwarding."""

    return _clean_discord_content(message)


def _build_jump_url(message: Mapping[str, Any], channel_id: int) -> str | None:
    guild_id = message.get("guild_id")
    message_id = message.get("id")
    if guild_id and message_id:
        return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
    return None


def _author_name(message: Mapping[str, Any]) -> str:
    author = message.get("author", {})
    return author.get("global_name") or author.get("username") or "Unknown user"


def _clean_discord_content(message: Mapping[str, Any]) -> str:
    raw_content = message.get("content")
    return _clean_text_fragment(raw_content, message)


def _clean_text_fragment(
    raw_text: str | None, message: Mapping[str, Any]
) -> str:
    if not raw_text:
        return ""

    content = str(raw_text)

    if "<" in content:
        mentions: dict[str, str] | None = None
        channel_mentions: dict[str, str] | None = None

        if "<@" in content:
            mentions = {
                str(user.get("id")): user.get("global_name") or user.get("username")
                for user in message.get("mentions", [])
                if user.get("id")
            }

            def replace_user(match: re.Match[str]) -> str:
                user_id = match.group(1)
                if mentions is None:
                    return "@пользователь"
                name = mentions.get(user_id)
                return f"@{name}" if name else "@пользователь"

            content = MENTION_PATTERN.sub(replace_user, content)

        if "<@&" in content:

            def replace_role(match: re.Match[str]) -> str:
                role_id = match.group(1)
                return f"@роль-{role_id}"

            content = ROLE_PATTERN.sub(replace_role, content)

        if "<#" in content:
            channel_mentions = {
                str(channel.get("id")): channel.get("name")
                for channel in message.get("mention_channels", [])
                if channel.get("id")
            }

            def replace_channel(match: re.Match[str]) -> str:
                channel_id = match.group(1)
                if channel_mentions is None:
                    return f"#канал-{channel_id}"
                name = channel_mentions.get(channel_id)
                return f"#{name}" if name else f"#канал-{channel_id}"

            content = CHANNEL_PATTERN.sub(replace_channel, content)

    content = _strip_simple_markdown(content)

    if "&" in content:
        content = html.unescape(content)

    lines = [line.strip() for line in content.splitlines()]
    # Remove trailing empty lines for tidier messages but preserve intentional blank spacing inside.
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _strip_simple_markdown(content: str) -> str:
    if not content or not any(char in content for char in "*_`~"):
        return content

    patterns = [
        re.compile(r"```([\s\S]+?)```"),
        re.compile(r"(?<!\\)`([^`]+?)`"),
        re.compile(r"~~(?=\S)(.+?)(?<=\S)~~"),
        re.compile(r"\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*"),
        re.compile(r"___(?=\S)(.+?)(?<=\S)___"),
        re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*"),
        re.compile(r"__(?=\S)(.+?)(?<=\S)__"),
        re.compile(r"(?<!\*)\*(?=\S)(.+?)(?<=\S)\*(?!\*)"),
        re.compile(r"(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)"),
        re.compile(r"~(?=\S)(.+?)(?<=\S)~"),
    ]

    previous = None
    stripped = content
    while previous != stripped:
        previous = stripped
        for pattern in patterns:
            stripped, _ = pattern.subn(r"\1", stripped)
    return stripped


def _combine_content_sections(sections: Sequence[str]) -> str:
    parts = [section for section in sections if section]
    if not parts:
        return ""
    return "\n\n".join(parts)


def _format_embeds(message: Mapping[str, Any]) -> str:
    embeds = message.get("embeds") or []
    if not isinstance(embeds, Sequence):
        return ""

    sections: List[str] = []
    for embed in embeds:
        if not isinstance(embed, Mapping):
            continue
        lines: List[str] = []

        title = _clean_text_fragment(embed.get("title"), message)
        if title:
            lines.append(title)

        description = _clean_text_fragment(embed.get("description"), message)
        if description:
            lines.append(description)

        fields = embed.get("fields") or []
        if isinstance(fields, Sequence):
            for field in fields:
                if not isinstance(field, Mapping):
                    continue
                name = _clean_text_fragment(field.get("name"), message)
                value = _clean_text_fragment(field.get("value"), message)
                if name and value:
                    lines.append(f"{name}: {value}")
                elif name:
                    lines.append(name)
                elif value:
                    lines.append(value)

        footer = embed.get("footer")
        if isinstance(footer, Mapping):
            footer_text = _clean_text_fragment(footer.get("text"), message)
            if footer_text:
                lines.append(footer_text)

        author = embed.get("author")
        if isinstance(author, Mapping):
            author_name = _clean_text_fragment(author.get("name"), message)
            if author_name:
                lines.append(author_name)

        if lines:
            sections.append("\n".join(lines))

    return "\n\n".join(sections)

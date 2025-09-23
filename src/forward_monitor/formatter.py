"""Helpers for formatting Discord messages for Telegram forwarding."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any, List, Mapping, Sequence, cast
from urllib.parse import urlparse

from .config import CustomisedText, FormattingProfile
from .types import DiscordMessage

__all__ = [
    "AttachmentInfo",
    "FormattedMessage",
    "format_announcement_message",
    "build_attachments",
    "clean_discord_content",
    "extract_embed_text",
]

MENTION_PATTERN = re.compile(r"<@!?(\d+)>")
ROLE_PATTERN = re.compile(r"<@&(\d+)>")
CHANNEL_PATTERN = re.compile(r"<#(\d+)>")

_MARKDOWN_PATTERNS = [
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


@dataclass(slots=True, frozen=True)
class AttachmentInfo:
    """Simplified representation of a Discord attachment."""

    url: str
    filename: str | None = None
    content_type: str | None = None
    size: int | None = None
    domain: str | None = None

    def display_label(self) -> str:
        """Return a human-friendly label for the attachment."""

        if self.filename:
            return f"{self.filename}: {self.url}"
        return self.url


@dataclass(slots=True, frozen=True)
class FormattedMessage:
    """Container describing the textual parts of a forwarded message."""

    text: str
    extra_messages: Sequence[str] = ()
    parse_mode: str | None = "HTML"
    disable_preview: bool = True


def format_announcement_message(
    channel_id: int,
    message: Mapping[str, Any],
    content: CustomisedText | str,
    attachments: Sequence[AttachmentInfo],
    *,
    embed_text: str | None = None,
    channel_label: str | None = None,
    formatting: FormattingProfile | None = None,
) -> FormattedMessage:
    """Build the outgoing text for a regular Discord message."""

    profile = formatting or FormattingProfile()
    if embed_text is None:
        embed_text = extract_embed_text(cast(DiscordMessage, message))
    customised = _ensure_customised_text(content, embed_text)
    author_name = _author_name(message)
    jump_url = _build_jump_url(message, channel_id)
    label = (channel_label or "").strip() or str(channel_id)

    chip_line = _build_chip_line(label, author_name, customised.chips)
    main_lines = _assemble_sections(customised)
    attachment_line = _attachment_summary(attachments, profile.attachments_style)
    if attachment_line:
        main_lines.append(attachment_line)
    if jump_url:
        main_lines.append("")
        main_lines.append(f"Открыть в Discord: {jump_url}")

    if chip_line:
        main_lines.insert(0, chip_line)

    escaped_lines = [_escape_html(line) for line in _collapse_blank_lines(main_lines)]
    base_text = "\n".join(escaped_lines)
    chunks = _chunk_html(base_text, profile.max_length, profile.ellipsis)
    main_text = chunks[0]
    extra_messages = tuple(chunks[1:])

    return FormattedMessage(
        text=main_text,
        extra_messages=extra_messages,
        parse_mode=profile.parse_mode,
        disable_preview=profile.disable_link_preview,
    )



def build_attachments(message: DiscordMessage) -> List[AttachmentInfo]:
    """Convert the raw Discord payload into AttachmentInfo objects."""

    attachments: List[AttachmentInfo] = []
    seen_urls: set[str] = set()

    for raw in message.get("attachments", []):
        if not isinstance(raw, Mapping):
            continue
        info = _attachment_from_payload(raw, seen_urls)
        if info is not None:
            attachments.append(info)

    embeds = message.get("embeds") or []
    if isinstance(embeds, Sequence):
        for embed in embeds:
            if not isinstance(embed, Mapping):
                continue
            attachments.extend(_build_embed_attachments(embed, seen_urls))

    return attachments


def _attachment_from_payload(
    raw: Mapping[str, Any], seen_urls: set[str]
) -> AttachmentInfo | None:
    url = raw.get("url")
    if not url or not isinstance(url, str) or url in seen_urls:
        return None

    seen_urls.add(url)
    filename = raw.get("filename")
    content_type = raw.get("content_type")
    size = raw.get("size")

    filename_text = str(filename) if filename else None
    content_type_text = str(content_type) if content_type else None
    if isinstance(size, (int, float)):
        size_value = int(size)
    elif isinstance(size, str):
        try:
            size_value = int(size)
        except ValueError:
            size_value = None
    else:
        size_value = None

    return AttachmentInfo(
        url=url,
        filename=filename_text,
        content_type=content_type_text,
        size=size_value,
        domain=_domain_from_url(url),
    )


def clean_discord_content(message: Mapping[str, Any]) -> str:
    """Normalise Discord message text for forwarding."""

    return _clean_discord_content(message)


def _build_jump_url(message: Mapping[str, Any], channel_id: int) -> str | None:
    guild_id = message.get("guild_id")
    message_id = message.get("id")
    if guild_id and message_id:
        return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
    if message_id and not guild_id:
        return f"https://discord.com/channels/@me/{channel_id}/{message_id}"
    return None


def _author_name(message: Mapping[str, Any]) -> str:
    author = message.get("author", {})
    return author.get("global_name") or author.get("username") or "Unknown user"


def _clean_discord_content(message: Mapping[str, Any]) -> str:
    raw_content = message.get("content")
    return _clean_text_fragment(raw_content, message)


def _clean_text_fragment(raw_text: str | None, message: Mapping[str, Any]) -> str:
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

    processed_lines: List[str] = []
    for line in content.splitlines():
        if line.strip():
            processed_lines.append(line.rstrip())
        else:
            processed_lines.append("")

    # Remove trailing empty lines for tidier messages but preserve intentional blank spacing inside.
    while processed_lines and not processed_lines[-1]:
        processed_lines.pop()
    return "\n".join(processed_lines)


def _strip_simple_markdown(content: str) -> str:
    if not content or not any(char in content for char in "*_`~"):
        return content

    previous = None
    stripped = content
    while previous != stripped:
        previous = stripped
        for pattern in _MARKDOWN_PATTERNS:
            stripped, _ = pattern.subn(r"\1", stripped)
    return stripped


def extract_embed_text(message: DiscordMessage) -> str:
    """Return cleaned textual content from embeds."""

    return _format_embeds(message)


def _format_embeds(message: DiscordMessage) -> str:
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

        url = embed.get("url")
        if url is not None:
            url_text = str(url).strip()
            if url_text:
                lines.append(url_text)

        if lines:
            sections.append("\n".join(lines))

    return "\n\n".join(sections)


def _build_embed_attachments(embed: Mapping[str, Any], seen_urls: set[str]) -> List[AttachmentInfo]:
    attachments: List[AttachmentInfo] = []

    for key, category in (
        ("image", "image/unknown"),
        ("thumbnail", "image/unknown"),
        ("video", "video/unknown"),
    ):
        item = embed.get(key)
        attachment = _attachment_from_embed_item(item, category, seen_urls)
        if attachment is not None:
            attachments.append(attachment)

    provider = embed.get("provider")
    provider_attachment = _attachment_from_embed_item(provider, None, seen_urls)
    if provider_attachment is not None:
        attachments.append(provider_attachment)

    return attachments


def _attachment_from_embed_item(
    item: Any,
    content_type: str | None,
    seen_urls: set[str],
) -> AttachmentInfo | None:
    if not isinstance(item, Mapping):
        return None

    url = item.get("url") or item.get("proxy_url")
    if not url or not isinstance(url, str) or url in seen_urls:
        return None

    seen_urls.add(url)
    filename = _filename_from_url(url)
    attachment_content_type = content_type
    if attachment_content_type is None:
        attachment_content_type = _guess_content_type_from_filename(filename)

    return AttachmentInfo(
        url=url,
        filename=filename,
        content_type=attachment_content_type,
        domain=_domain_from_url(url),
    )


def _filename_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    path = parsed.path or ""
    if not path:
        return None
    filename = path.rsplit("/", 1)[-1]
    if not filename:
        return None
    return filename


def _domain_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    domain = parsed.netloc or parsed.path
    if domain:
        return domain
    return url or None


def _guess_content_type_from_filename(filename: str | None) -> str | None:
    if not filename or "." not in filename:
        return None
    extension = filename.rsplit(".", 1)[-1].lower()
    if extension in {"jpg", "jpeg", "png", "gif", "bmp", "webp"}:
        return "image/unknown"
    if extension in {"mp4", "mov", "mkv", "webm"}:
        return "video/unknown"
    if extension in {"mp3", "wav", "ogg", "flac", "m4a"}:
        return "audio/unknown"
    return None


def _ensure_customised_text(
    content: CustomisedText | str, embed_text: str | None
) -> CustomisedText:
    if isinstance(content, CustomisedText):
        body_lines = list(content.body_lines)
        if embed_text:
            embed_lines = _split_and_normalise(embed_text)
            if embed_lines:
                if body_lines:
                    body_lines.append("")
                body_lines.extend(embed_lines)
        combined = tuple(_collapse_blank_lines(body_lines))
        return CustomisedText(
            chips=content.chips,
            header_lines=content.header_lines,
            body_lines=combined,
            footer_lines=content.footer_lines,
        )

    lines = _split_and_normalise(content)
    if embed_text:
        embed_lines = _split_and_normalise(embed_text)
        if embed_lines:
            if lines:
                lines.append("")
            lines.extend(embed_lines)
    return CustomisedText(
        chips=(),
        header_lines=(),
        body_lines=tuple(_collapse_blank_lines(lines)),
        footer_lines=(),
    )


def _split_and_normalise(text: str) -> List[str]:
    if not text:
        return []
    raw_lines = [line.rstrip() for line in text.splitlines()]
    return _collapse_blank_lines(raw_lines)


def _assemble_sections(customised: CustomisedText) -> List[str]:
    sections: List[List[str]] = []
    if customised.header_lines:
        sections.append(list(customised.header_lines))
    if customised.body_lines:
        sections.append(list(customised.body_lines))
    if customised.footer_lines:
        sections.append(list(customised.footer_lines))

    lines: List[str] = []
    for block in sections:
        cleaned = _collapse_blank_lines(block)
        if not cleaned:
            continue
        if lines:
            lines.append("")
        lines.extend(cleaned)
    return _deduplicate_consecutive(lines)


def _build_chip_line(
    channel_label: str | None, author: str, chips: Sequence[str]
) -> str:
    ordered: List[str] = []
    for candidate in (channel_label, author, *chips):
        text = (candidate or "").strip()
        if not text or text in ordered:
            continue
        ordered.append(text)
    if not ordered:
        return ""
    return f"📢 {' · '.join(ordered)}"


def _attachment_summary(
    attachments: Sequence[AttachmentInfo], style: str
) -> str | None:
    if not attachments:
        return None
    domains: List[str] = []
    for attachment in attachments:
        domain = attachment.domain or attachment.url
        if not domain:
            continue
        cleaned = domain.split("/", 1)[0]
        if cleaned not in domains:
            domains.append(cleaned)

    summary = ""
    if domains:
        limit = 3 if style == "minimal" else len(domains)
        visible = domains[:limit]
        summary = ", ".join(visible)
        remaining = len(domains) - len(visible)
        if remaining > 0:
            summary = f"{summary} +{remaining}"

    text = f"Вложения: {len(attachments)}"
    if summary:
        text = f"{text} · {summary}"
    return text


def _escape_html(value: str) -> str:
    return html.escape(value, quote=False)


def _collapse_blank_lines(lines: Sequence[str]) -> List[str]:
    result: List[str] = []
    blank = False
    for line in lines:
        if line is None:
            continue
        cleaned = line.rstrip()
        if cleaned:
            result.append(cleaned)
            blank = False
        else:
            if result and not blank:
                result.append("")
            blank = True
    while result and not result[-1]:
        result.pop()
    return result


def _deduplicate_consecutive(lines: Sequence[str]) -> List[str]:
    trimmed: List[str] = []
    previous_key: str | None = None
    for line in lines:
        if not line:
            if trimmed and trimmed[-1] == "":
                continue
            trimmed.append("")
            previous_key = None
            continue

        key = line.casefold()
        if key == previous_key:
            continue

        trimmed.append(line)
        previous_key = key

    while trimmed and not trimmed[-1]:
        trimmed.pop()

    return trimmed


def _chunk_html(text: str, limit: int, ellipsis: str) -> List[str]:
    if limit <= 0 or len(text) <= limit:
        return [text]

    chunks: List[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        cut = _find_safe_cut(remaining, limit)
        fragment = remaining[:cut].rstrip()
        fragment = _trim_partial_entity(fragment)
        if not fragment:
            fragment = _trim_partial_entity(remaining[:limit])
        if not fragment:
            fragment = remaining[:limit]
        chunks.append(f"{fragment}{ellipsis}")
        remaining = remaining[len(fragment) :].lstrip()

    return chunks


def _find_safe_cut(text: str, limit: int) -> int:
    newline = text.rfind("\n", 0, limit)
    if newline > limit * 0.6:
        return newline
    space = text.rfind(" ", 0, limit)
    if space > limit * 0.6:
        return space
    return limit


def _trim_partial_entity(fragment: str) -> str:
    if "&" not in fragment:
        return fragment
    if fragment.endswith("&"):
        return fragment[:-1]
    last_amp = fragment.rfind("&")
    if last_amp == -1:
        return fragment
    if ";" not in fragment[last_amp:]:
        return fragment[:last_amp]
    return fragment

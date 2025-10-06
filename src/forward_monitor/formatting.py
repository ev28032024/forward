"""Telegram formatting helpers."""

from __future__ import annotations

import html
import re
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse

from .models import ChannelConfig, DiscordMessage, FormattedTelegramMessage

EmbedPayload = Mapping[str, Any]
AttachmentPayload = Mapping[str, Any]


def format_discord_message(
    message: DiscordMessage,
    channel: ChannelConfig,
) -> FormattedTelegramMessage:
    """Convert a Discord message into Telegram text respecting the channel profile."""

    formatting = channel.formatting
    content = _sanitize_content(message.content or "")
    embed_blocks = list(_clean_embed_text(message.embeds))
    image_urls, file_attachments = _split_attachments(message.attachments)
    attachments_block = _render_attachments_block(
        file_attachments, formatting.attachments_style
    )
    link_block = _build_link_block(message, formatting.show_discord_link)

    blocks: list[str] = []
    chip = _build_chip(channel.label, message.author_name)
    if chip:
        blocks.append(chip)

    if content:
        blocks.append(_format_text_block(content))
    for embed in embed_blocks:
        blocks.append(_format_text_block(embed))
    if attachments_block:
        blocks.append(attachments_block)
    if link_block:
        blocks.append(link_block)

    combined = "\n\n".join(block for block in blocks if block)
    chunks = _chunk_html_text(combined, formatting.max_length, formatting.ellipsis)

    return FormattedTelegramMessage(
        text=chunks[0] if chunks else "",
        extra_messages=tuple(chunks[1:]),
        parse_mode="HTML",
        disable_preview=formatting.disable_preview,
        image_urls=image_urls,
    )


def _clean_embed_text(embeds: Sequence[EmbedPayload]) -> Iterable[str]:
    for embed in embeds:
        title = str(embed.get("title") or "").strip()
        description = str(embed.get("description") or "").strip()
        url = str(embed.get("url") or "").strip()
        fields_text = []
        for field in embed.get("fields") or []:
            name = str(field.get("name") or "").strip()
            value = str(field.get("value") or "").strip()
            if name and value:
                fields_text.append(f"{name}: {value}")
            elif value:
                fields_text.append(value)
        segments = [
            segment for segment in (title, description, "\n".join(fields_text), url) if segment
        ]
        if segments:
            cleaned_segments = [
                part
                for part in (
                    _sanitize_content(segment) for segment in segments if segment
                )
                if part
            ]
            if cleaned_segments:
                yield "\n".join(cleaned_segments)


def _summarise_attachments(
    attachments: Sequence[AttachmentPayload],
    style: str,
) -> Iterable[str]:
    if not attachments:
        return []

    style = style.lower()
    if style not in {"summary", "links"}:
        style = "summary"

    lines: list[str] = []
    for index, attachment in enumerate(attachments, start=1):
        url = str(attachment.get("url") or attachment.get("proxy_url") or "").strip()
        if not url:
            continue
        filename = str(attachment.get("filename") or "").strip()
        content_type = str(attachment.get("content_type") or "").strip()
        size = attachment.get("size")
        size_text = _human_size(size) if isinstance(size, (int, float)) else ""
        domain = urlparse(url).netloc

        if style == "links":
            label = filename or domain or f"Attachment {index}"
            lines.append(f"{label}: {url}")
        else:
            parts = [part for part in (filename, content_type, size_text, url) if part]
            lines.append(" • ".join(parts))
    if lines:
        header = "Вложения" if style == "summary" else "Ссылки на вложения"
        return [header, *lines]
    return []


def _render_attachments_block(
    attachments: Sequence[AttachmentPayload], style: str
) -> str:
    summary = list(_summarise_attachments(attachments, style))
    if not summary:
        return ""
    header_text = summary[0].rstrip(":")
    icon = "🔗" if style.lower() == "links" else "📎"
    header = f"{icon} <b>{_escape(header_text)}</b>"
    lines = [f"• {_escape(line)}" for line in summary[1:]]
    if lines:
        return "\n".join([header, *lines])
    return header


def _split_attachments(
    attachments: Sequence[AttachmentPayload],
) -> tuple[tuple[str, ...], tuple[AttachmentPayload, ...]]:
    images: list[str] = []
    others: list[AttachmentPayload] = []
    for attachment in attachments:
        url = str(attachment.get("url") or attachment.get("proxy_url") or "").strip()
        if not url:
            continue
        if _is_image_attachment(attachment):
            images.append(url)
        else:
            others.append(attachment)
    return tuple(images), tuple(others)


def _is_image_attachment(attachment: AttachmentPayload) -> bool:
    filename = str(attachment.get("filename") or "").lower()
    content_type = str(attachment.get("content_type") or "").lower()
    if any(filename.endswith(ext) for ext in _IMAGE_EXTENSIONS):
        return True
    if content_type.startswith("image/"):
        subtype = content_type.split("/", 1)[1] if "/" in content_type else ""
        return not subtype.startswith("gifv")
    return False


def _human_size(value: float | int | None) -> str:
    if value is None:
        return ""
    size = float(value)
    units = ["B", "KB", "MB", "GB", "TB"]
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    return f"{size:.1f}{units[index]}"


def _build_chip(label: str, author: str) -> str:
    parts: list[str] = []
    if label:
        parts.append(f"<b>{_escape(label)}</b>")
    if author:
        parts.append(f"<b>{_escape(author)}</b>")
    return " • ".join(parts)


def _build_link_block(message: DiscordMessage, enabled: bool) -> str:
    if not enabled:
        return ""
    message_id = message.id.strip()
    channel_id = message.channel_id.strip()
    if not message_id or not channel_id:
        return ""
    guild_id = message.guild_id.strip() if message.guild_id else "@me"
    link = f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
    return f"🔗 <a href=\"{_escape(link)}\">link</a>"


def _chunk_html_text(text: str, limit: int, ellipsis: str) -> list[str]:
    if not text:
        return []
    if limit <= 0 or len(text) <= limit:
        return [text]

    remaining = text
    chunks: list[str] = []
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        split = remaining.rfind("\n", 0, limit)
        if split == -1 or split < limit // 4:
            split = remaining.rfind(" ", 0, limit)
        if split == -1 or split < limit // 4:
            split = limit

        chunk = remaining[:split].rstrip()
        tail = remaining[split:].lstrip("\n")
        if chunk:
            if tail:
                chunk = f"{chunk}{ellipsis}"
            chunks.append(chunk)
        else:
            chunk = remaining[:limit]
            chunk = f"{chunk}{ellipsis}" if len(remaining) > limit else chunk
            chunks.append(chunk)
            tail = remaining[limit:]
        remaining = tail.lstrip()
    return chunks


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def _format_text_block(text: str) -> str:
    return _apply_basic_markdown(text)


_CHANNEL_MENTION_RE = re.compile(r"<#([0-9]+)>")
_CUSTOM_EMOJI_RE = re.compile(r"<a?:[a-zA-Z0-9_~]+:[0-9]+>")
_EXTRA_SPACE_RE = re.compile(r"[ \t]{2,}")
_TRIPLE_NEWLINES_RE = re.compile(r"\n{3,}")


def _sanitize_content(text: str) -> str:
    if not text:
        return ""
    cleaned = _CHANNEL_MENTION_RE.sub(lambda match: f"#{match.group(1)}", text)
    cleaned = _CUSTOM_EMOJI_RE.sub("", cleaned)
    cleaned = _EXTRA_SPACE_RE.sub(" ", cleaned)
    cleaned = _TRIPLE_NEWLINES_RE.sub("\n\n", cleaned)
    return cleaned.strip()


def _apply_basic_markdown(text: str) -> str:
    placeholders: dict[str, str] = {}

    def _store(value: str) -> str:
        token = f"__MARKUP_PLACEHOLDER_{len(placeholders)}__"
        placeholders[token] = value
        return token

    def _format_code_block(match: re.Match[str]) -> str:
        content = match.group(1).strip("\n")
        escaped = html.escape(content, quote=False)
        return _store(f"<pre><code>{escaped}</code></pre>")

    def _format_inline_code(match: re.Match[str]) -> str:
        content = match.group(1)
        escaped = html.escape(content, quote=False)
        return _store(f"<code>{escaped}</code>")

    text_without_code = _CODE_BLOCK_RE.sub(_format_code_block, text)
    text_without_code = _CODE_SPAN_RE.sub(_format_inline_code, text_without_code)

    escaped = html.escape(text_without_code, quote=False)

    escaped = _BOLD_RE.sub(lambda m: f"<b>{m.group(1)}</b>", escaped)
    escaped = _UNDERLINE_RE.sub(lambda m: f"<u>{m.group(1)}</u>", escaped)
    escaped = _STRIKE_RE.sub(lambda m: f"<s>{m.group(1)}</s>", escaped)
    escaped = _SPOILER_RE.sub(lambda m: f"<tg-spoiler>{m.group(1)}</tg-spoiler>", escaped)

    result = escaped
    for token, value in placeholders.items():
        result = result.replace(token, value)
    return result


_CODE_BLOCK_RE = re.compile(r"```(.*?)```", re.DOTALL)
_CODE_SPAN_RE = re.compile(r"`([^`]+?)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_UNDERLINE_RE = re.compile(r"__(.+?)__", re.DOTALL)
_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)
_SPOILER_RE = re.compile(r"\|\|(.+?)\|\|", re.DOTALL)

_IMAGE_EXTENSIONS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".tiff",
    ".svg",
)

"""Telegram formatting helpers."""

from __future__ import annotations

import html
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse

from .models import ChannelConfig, DiscordMessage, FormattedTelegramMessage, ReplacementRule

EmbedPayload = Mapping[str, Any]
AttachmentPayload = Mapping[str, Any]


def format_discord_message(
    message: DiscordMessage,
    channel: ChannelConfig,
) -> FormattedTelegramMessage:
    """Convert a Discord message into Telegram text respecting the channel profile."""

    formatting = channel.formatting
    content = apply_replacements(message.content or "", channel.replacements)
    embed_text = "\n".join(_clean_embed_text(message.embeds))
    attachment_lines = list(
        _summarise_attachments(message.attachments, formatting.attachments_style)
    )

    blocks: list[str] = []
    if formatting.header:
        blocks.append(formatting.header)

    chip = _build_chip(channel.label, message.author_name, formatting.chip)
    if chip:
        blocks.append(chip)

    if content:
        blocks.append(content)
    if embed_text:
        blocks.append(embed_text)
    if attachment_lines:
        blocks.extend(attachment_lines)
    if formatting.footer:
        blocks.append(formatting.footer)

    joined = "\n".join(line for line in blocks if line)
    escaped = _escape(joined, formatting.parse_mode)
    chunks = _chunk_text(escaped, formatting.max_length, formatting.ellipsis)
    parse_mode = None if formatting.parse_mode.lower() == "text" else formatting.parse_mode

    return FormattedTelegramMessage(
        text=chunks[0] if chunks else "",
        extra_messages=tuple(chunks[1:]),
        parse_mode=parse_mode,
        disable_preview=formatting.disable_preview,
    )


def apply_replacements(text: str, replacements: Sequence[ReplacementRule]) -> str:
    result = text
    for rule in replacements:
        if rule.pattern:
            result = result.replace(rule.pattern, rule.replacement)
    return result


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
            yield "\n".join(segments)


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
        header = "Вложения:" if style == "summary" else "Ссылки на вложения:"
        return [header, *lines]
    return []


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


def _build_chip(label: str, author: str, chip: str) -> str:
    parts = [part for part in (label, author, chip) if part]
    if not parts:
        return ""
    return " • ".join(parts)


def _escape(text: str, parse_mode: str) -> str:
    mode = parse_mode.lower()
    if mode == "markdownv2":
        return _escape_markdown_v2(text)
    if mode == "markdown":
        return _escape_markdown(text)
    if mode == "html":
        return html.escape(text)
    return text


def _escape_markdown_v2(text: str) -> str:
    for char in "_[]()~`>#+-=|{}.!":
        text = text.replace(char, f"\\{char}")
    return text.replace("*", "\\*")


def _escape_markdown(text: str) -> str:
    specials = "*_`[]()"
    for char in specials:
        text = text.replace(char, f"\\{char}")
    return text


def _chunk_text(text: str, limit: int, ellipsis: str) -> list[str]:
    if limit <= 0 or len(text) <= limit:
        return [text] if text else []

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
        if chunk and split < len(remaining):
            chunk = f"{chunk}{ellipsis}"
        elif not chunk:
            chunk = remaining[:limit]
        chunks.append(chunk)
        remaining = remaining[split:].lstrip()
    return chunks

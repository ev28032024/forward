from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence


@dataclass(slots=True)
class AttachmentInfo:
    """Simplified representation of a Discord attachment."""

    url: str
    filename: str | None
    content_type: str | None
    size: int | None = None

    def display_label(self) -> str:
        if self.filename:
            return f"{self.filename}: {self.url}"
        return self.url


def format_announcement_message(
    channel_id: int,
    message: Dict,
    content: str,
    attachments: Sequence[AttachmentInfo],
) -> str:
    author_name = _author_name(message)
    jump_url = _build_jump_url(message, channel_id)

    lines: List[str] = [
        f"📢 Новое сообщение в канале {channel_id} от {author_name}",
    ]
    if content:
        lines.append("")
        lines.append(content)

    if attachments:
        lines.append("")
        lines.append("Вложения:")
        lines.extend(_attachment_lines(attachments))

    if jump_url:
        lines.append("")
        lines.append(f"Открыть в Discord: {jump_url}")

    return "\n".join(lines)


def format_pinned_message(
    channel_id: int,
    message: Dict,
    content: str,
    attachments: Sequence[AttachmentInfo],
) -> str:
    author_name = _author_name(message)
    jump_url = _build_jump_url(message, channel_id)

    lines: List[str] = [
        f"📌 Новая закреплённая запись в канале {channel_id} (автор: {author_name})",
    ]
    if content:
        lines.append("")
        lines.append(content)

    if attachments:
        lines.append("")
        lines.append("Вложения:")
        lines.extend(_attachment_lines(attachments))

    if jump_url:
        lines.append("")
        lines.append(f"Открыть в Discord: {jump_url}")

    return "\n".join(lines)


def _attachment_lines(attachments: Iterable[AttachmentInfo]) -> List[str]:
    lines: List[str] = []
    for attachment in attachments:
        if attachment.url:
            lines.append(attachment.display_label())
    return lines


def build_attachments(message: Dict) -> List[AttachmentInfo]:
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


def clean_discord_content(message: Dict) -> str:
    return _clean_discord_content(message)


def _build_jump_url(message: Dict, channel_id: int) -> str | None:
    guild_id = message.get("guild_id")
    message_id = message.get("id")
    if guild_id and message_id:
        return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
    return None


MENTION_PATTERN = re.compile(r"<@!?(\d+)>")
ROLE_PATTERN = re.compile(r"<@&(\d+)>")
CHANNEL_PATTERN = re.compile(r"<#(\d+)>")
SIMPLE_MARKDOWN = re.compile(r"([*_`~])+")


def _author_name(message: Dict) -> str:
    author = message.get("author", {})
    return author.get("global_name") or author.get("username") or "Unknown user"


def _clean_discord_content(message: Dict) -> str:
    raw_content = message.get("content") or ""
    if not raw_content:
        return ""

    mentions = {
        str(user.get("id")): user.get("global_name") or user.get("username")
        for user in message.get("mentions", [])
        if user.get("id")
    }
    channel_mentions = {
        str(channel.get("id")): channel.get("name")
        for channel in message.get("mention_channels", [])
        if channel.get("id")
    }

    def replace_user(match: re.Match[str]) -> str:
        user_id = match.group(1)
        name = mentions.get(user_id)
        return f"@{name}" if name else "@пользователь"

    def replace_role(match: re.Match[str]) -> str:
        role_id = match.group(1)
        return f"@роль-{role_id}"

    def replace_channel(match: re.Match[str]) -> str:
        channel_id = match.group(1)
        name = channel_mentions.get(channel_id)
        return f"#{name}" if name else f"#канал-{channel_id}"

    content = MENTION_PATTERN.sub(replace_user, raw_content)
    content = ROLE_PATTERN.sub(replace_role, content)
    content = CHANNEL_PATTERN.sub(replace_channel, content)
    content = SIMPLE_MARKDOWN.sub("", content)
    content = html.unescape(content)

    lines = [line.strip() for line in content.splitlines()]
    # Remove trailing empty lines for tidier messages but preserve intentional blank spacing inside.
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)

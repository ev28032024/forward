"""Filtering rules applied before forwarding messages."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .models import DiscordMessage, FilterConfig


@dataclass(slots=True)
class FilterDecision:
    """Result of evaluating filters."""

    allowed: bool
    reason: str | None = None


class FilterEngine:
    """Apply allow/deny filters to Discord messages."""

    def __init__(self, config: FilterConfig):
        self._config = config

    def evaluate(self, message: DiscordMessage) -> FilterDecision:
        content = message.content or ""
        lowered = content.lower()
        author_id = message.author_id

        if self._config.allowed_senders and author_id not in self._config.allowed_senders:
            return FilterDecision(False, "sender_not_allowed")
        if author_id in self._config.blocked_senders:
            return FilterDecision(False, "sender_blocked")

        if self._config.whitelist:
            if not any(token in lowered for token in _normalise_tokens(self._config.whitelist)):
                return FilterDecision(False, "whitelist_miss")
        if any(token in lowered for token in _normalise_tokens(self._config.blacklist)):
            return FilterDecision(False, "blacklist_hit")

        message_types = set(_infer_types(message))
        if self._config.allowed_types and not (message_types & self._config.allowed_types):
            return FilterDecision(False, "type_not_allowed")
        if self._config.blocked_types and (message_types & self._config.blocked_types):
            return FilterDecision(False, "type_blocked")

        return FilterDecision(True)


def _infer_types(message: DiscordMessage) -> Iterable[str]:
    if message.content:
        yield "text"

    for attachment in message.attachments:
        content_type = str(attachment.get("content_type") or "").lower()
        filename = str(attachment.get("filename") or "").lower()
        if any(filename.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp")):
            yield "image"
        elif any(filename.endswith(ext) for ext in (".mp4", ".mov", ".mkv", ".webm")):
            yield "video"
        elif any(filename.endswith(ext) for ext in (".mp3", ".ogg", ".wav", ".flac")):
            yield "audio"
        elif content_type.startswith("image/"):
            yield "image"
        elif content_type.startswith("video/"):
            yield "video"
        elif content_type.startswith("audio/"):
            yield "audio"
        else:
            yield "attachment"

    if message.embeds:
        yield "embed"

    if not message.content and not message.attachments and not message.embeds:
        yield "empty"


def _normalise_tokens(tokens: Iterable[str]) -> set[str]:
    cleaned: set[str] = set()
    for token in tokens:
        text = token.strip().lower()
        if text:
            cleaned.add(text)
    return cleaned


_WORD_RE = re.compile(r"\w+")


def tokenise(text: str) -> set[str]:
    """Public helper used in tests to inspect tokenisation."""

    return {match.group(0).lower() for match in _WORD_RE.finditer(text)}

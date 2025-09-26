from __future__ import annotations

from forward_monitor.filters import FilterEngine
from forward_monitor.models import DiscordMessage, FilterConfig


def make_message(**kwargs: object) -> DiscordMessage:
    return DiscordMessage(
        id=str(kwargs.get("id", "1")),
        channel_id=str(kwargs.get("channel_id", "10")),
        author_id=str(kwargs.get("author_id", "42")),
        author_name=str(kwargs.get("author_name", "User")),
        content=str(kwargs.get("content", "")),
        attachments=tuple(kwargs.get("attachments", [])),
        embeds=tuple(kwargs.get("embeds", [])),
    )


def test_filter_engine_whitelist_and_blacklist() -> None:
    config = FilterConfig(whitelist={"promo"}, blacklist={"spam"})
    engine = FilterEngine(config)

    allowed = engine.evaluate(make_message(content="Big promo today"))
    rejected = engine.evaluate(make_message(content="spam only"))
    missing = engine.evaluate(make_message(content="nothing interesting"))

    assert allowed.allowed is True
    assert rejected.allowed is False
    assert missing.allowed is False


def test_filter_engine_types() -> None:
    config = FilterConfig(allowed_types={"image"})
    engine = FilterEngine(config)
    image_message = make_message(attachments=[{"filename": "image.png"}])
    file_message = make_message(attachments=[{"filename": "report.pdf"}])

    assert engine.evaluate(image_message).allowed is True
    assert engine.evaluate(file_message).allowed is False

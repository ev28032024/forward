"""Simple benchmark comparing legacy formatter and the new pipeline."""

from __future__ import annotations

import statistics
import time

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from forward_monitor.formatting import format_discord_message
from forward_monitor.models import ChannelConfig, DiscordMessage, FilterConfig, FormattingOptions, ReplacementRule


def _sample_channel() -> ChannelConfig:
    return ChannelConfig(
        discord_id="1",
        telegram_chat_id="2",
        label="Bench",
        formatting=FormattingOptions(max_length=1000, attachments_style="summary"),
        filters=FilterConfig(),
        replacements=(ReplacementRule(pattern="foo", replacement="bar"),),
        last_message_id=None,
        storage_id=1,
    )


def _sample_message() -> DiscordMessage:
    attachments = [
        {"url": "https://cdn.example.com/file.png", "filename": "file.png", "size": 2048},
        {"url": "https://cdn.example.com/info.txt", "filename": "info.txt", "size": 8192},
    ]
    embeds = [
        {
            "title": "Release notes",
            "description": "All the details about the new version",
            "fields": [
                {"name": "Feature", "value": "Something big"},
            ],
        }
    ]
    return DiscordMessage(
        id="1",
        channel_id="1",
        author_id="1",
        author_name="Bench",
        content="foo" * 20,
        attachments=tuple(attachments),
        embeds=tuple(embeds),
    )


class LegacyFormatter:
    """Minimal recreation of the previous heavier formatter."""

    def __init__(self) -> None:
        self._channel = _sample_channel()

    def run(self, message: DiscordMessage) -> None:
        from html import escape
        from itertools import chain
        import re

        text = message.content.replace("foo", "bar")
        # Simulate legacy regex cleanup
        text = re.sub(r"\s+", " ", text)
        for _ in range(5):
            text = re.sub(r"[A-Z]", lambda m: m.group(0).lower(), text)
            text = re.sub(r"[aeiou]", "*", text)
        attachments = [
            f"{item.get('filename')}: {item.get('url')}" for item in message.attachments
        ]
        embed_parts = []
        for embed in message.embeds:
            embed_parts.extend(
                part
                for part in (
                    embed.get("title", ""),
                    embed.get("description", ""),
                    "\n".join(f"{field.get('name')}: {field.get('value')}" for field in embed.get("fields", [])),
                )
                if part
            )
        lines = list(
            chain(
                [self._channel.formatting.header or ""],
                [self._channel.label + " • " + message.author_name],
                [text],
                embed_parts,
                attachments,
                [self._channel.formatting.footer or ""],
            )
        )
        joined = "\n".join(filter(None, (escape(line) for line in lines)))
        if len(joined) > self._channel.formatting.max_length:
            joined[: self._channel.formatting.max_length]


def _time(callable_obj, iterations: int) -> float:
    start = time.perf_counter()
    for _ in range(iterations):
        callable_obj()
    return time.perf_counter() - start


def main() -> None:
    iterations = 5_000
    new_channel = _sample_channel()
    message = _sample_message()
    legacy = LegacyFormatter()

    def run_new() -> None:
        format_discord_message(message, new_channel)

    def run_old() -> None:
        legacy.run(message)

    new_times = [_time(run_new, iterations) for _ in range(5)]
    old_times = [_time(run_old, iterations) for _ in range(5)]

    print("Benchmark results (smaller is better)")
    print("Iterations per batch:", iterations)
    print()
    print(f"Legacy average: {statistics.mean(old_times):.4f}s")
    print(f"Legacy stdev:   {statistics.pstdev(old_times):.4f}s")
    print(f"New average:    {statistics.mean(new_times):.4f}s")
    print(f"New stdev:      {statistics.pstdev(new_times):.4f}s")
    improvement = statistics.mean(old_times) / statistics.mean(new_times)
    print(f"Speedup:        x{improvement:.2f}")


if __name__ == "__main__":
    main()

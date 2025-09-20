from __future__ import annotations

import argparse
from time import perf_counter
from typing import cast

from forward_monitor.config import ChannelMapping, MessageFilters
from forward_monitor.formatter import (
    build_attachments,
    clean_discord_content,
    extract_embed_text,
    format_announcement_message,
)
from forward_monitor.monitor import _attachment_category, _should_forward
from forward_monitor.types import DiscordMessage

_SAMPLE_AUTHOR = {"id": "123", "username": "Benchmark"}
_SAMPLE_CHANNEL = ChannelMapping(discord_channel_id=1, telegram_chat_id="chat")
_SAMPLE_FILTERS = MessageFilters().prepare()


def _synthetic_message(length: int, attachments: int) -> DiscordMessage:
    base = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. "
    repeated = (base * ((length // len(base)) + 1))[:length]
    payload: dict[str, object] = {
        "id": "9001",
        "content": repeated,
        "author": _SAMPLE_AUTHOR,
        "embeds": [
            {
                "title": "Benchmark",
                "description": repeated[: max(0, length // 2)],
                "fields": [
                    {"name": "Field", "value": "Value"},
                ],
            }
        ],
        "attachments": [
            {
                "url": f"https://example.com/file{index}.png",
                "filename": f"file{index}.png",
                "content_type": "image/png",
                "size": 1024 * (index + 1),
            }
            for index in range(attachments)
        ],
    }
    return cast(DiscordMessage, payload)


def benchmark_formatter(iterations: int, *, length: int = 4000, attachments: int = 4) -> float:
    message = _synthetic_message(length, attachments)
    cleaned = clean_discord_content(message)
    embed_text = extract_embed_text(message)
    attachment_infos = build_attachments(message)
    start = perf_counter()
    for _ in range(iterations):
        format_announcement_message(
            _SAMPLE_CHANNEL.discord_channel_id,
            message,
            cleaned,
            attachment_infos,
            embed_text=embed_text,
            channel_label="Bench",
        )
    return perf_counter() - start


def benchmark_processing(iterations: int, *, length: int = 4000, attachments: int = 4) -> float:
    message = _synthetic_message(length, attachments)
    start = perf_counter()
    for _ in range(iterations):
        cleaned = clean_discord_content(message)
        embed_text = extract_embed_text(message)
        attachment_infos = build_attachments(message)
        categories = tuple(_attachment_category(attachment) for attachment in attachment_infos)
        _should_forward(
            message,
            cleaned,
            embed_text,
            attachment_infos,
            categories,
            _SAMPLE_FILTERS,
        )
    return perf_counter() - start


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark formatter and processing paths.")
    parser.add_argument("--iterations", type=int, default=500, help="Iterations per benchmark")
    parser.add_argument("--length", type=int, default=4000, help="Synthetic message length")
    parser.add_argument("--attachments", type=int, default=4, help="Synthetic attachment count")
    args = parser.parse_args()

    fmt_duration = benchmark_formatter(
        args.iterations, length=args.length, attachments=args.attachments
    )
    proc_duration = benchmark_processing(
        args.iterations, length=args.length, attachments=args.attachments
    )

    fmt_ms = fmt_duration * 1000 / max(args.iterations, 1)
    proc_ms = proc_duration * 1000 / max(args.iterations, 1)

    print(
        "formatter: {:.2f} ms/iter ({:.2f} s total)".format(fmt_ms, fmt_duration)
    )
    print(
        "processing: {:.2f} ms/iter ({:.2f} s total)".format(proc_ms, proc_duration)
    )


if __name__ == "__main__":
    main()

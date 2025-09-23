#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from time import perf_counter
from typing import cast

ROOT = Path(__file__).resolve().parent.parent
SRC_PATH = ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.append(str(SRC_PATH))

from forward_monitor.config import (  # noqa: E402
    ChannelMapping,
    FormattingProfile,
    MessageCustomization,
    MessageFilters,
)
from forward_monitor.formatter import (  # noqa: E402
    CustomisedText,
    build_attachments,
    format_announcement_message,
)
from forward_monitor.monitor import ChannelContext, _forward_message  # noqa: E402
from forward_monitor.types import DiscordMessage  # noqa: E402


def _make_sample_message() -> DiscordMessage:
    return cast(
        DiscordMessage,
        {
            "id": "100",
            "channel_id": 42,
            "guild_id": 999,
            "content": "Update for <@123> in <#456> with attachment",
            "author": {"id": "7", "username": "Reporter", "global_name": "Reporter"},
            "mentions": [{"id": "123", "username": "Alice"}],
            "mention_channels": [{"id": "456", "name": "announcements"}],
            "attachments": [
                {
                    "url": "https://cdn.example.com/image.png",
                    "filename": "image.png",
                    "content_type": "image/png",
                    "size": 2048,
                },
                {
                    "url": "https://cdn.example.com/file.pdf",
                    "filename": "report.pdf",
                    "content_type": "application/pdf",
                    "size": 8192,
                },
            ],
            "embeds": [
                {
                    "title": "Highlights",
                    "description": "Key details of the announcement",
                    "fields": [
                        {"name": "Status", "value": "Ready"},
                        {"name": "Priority", "value": "High"},
                    ],
                }
            ],
        },
    )


class _NullTelegram:
    __slots__ = ()

    async def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        parse_mode: str | None = None,
        disable_web_page_preview: bool | None = None,
    ) -> None:
        return None

    async def send_photo(
        self,
        chat_id: str,
        photo: str,
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
    ) -> None:
        return None

    async def send_video(
        self,
        chat_id: str,
        video: str,
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
    ) -> None:
        return None

    async def send_audio(
        self,
        chat_id: str,
        audio: str,
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
    ) -> None:
        return None

    async def send_document(
        self,
        chat_id: str,
        document: str,
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
    ) -> None:
        return None


def benchmark_formatter(iterations: int) -> None:
    message = _make_sample_message()
    attachments = build_attachments(message)
    customization = MessageCustomization(
        chips=("Alert", "Production"),
        headers=("Header line",),
        footers=("Footer line",),
    ).prepare()
    customised: CustomisedText = customization.render(message["content"])
    context_formatting = FormattingProfile()
    start = perf_counter()
    for _ in range(iterations):
        format_announcement_message(
            message["channel_id"],
            message,
            customised,
            attachments,
            channel_label="Status",
            formatting=context_formatting,
        )
    elapsed = perf_counter() - start
    throughput = iterations / elapsed if elapsed else float("inf")
    print(f"formatter: {iterations} iterations in {elapsed:.3f}s ({throughput:.1f} msg/s)")


async def benchmark_forwarding(iterations: int) -> None:
    message_template = _make_sample_message()
    context = ChannelContext(
        mapping=ChannelMapping(discord_channel_id=42, telegram_chat_id="@target"),
        filters=MessageFilters().prepare(),
        customization=MessageCustomization().prepare(),
        formatting=FormattingProfile(),
    )
    telegram = _NullTelegram()
    start = perf_counter()
    for index in range(iterations):
        message = dict(message_template)
        message["id"] = str(1000 + index)
        await _forward_message(
            context=context,
            channel_id=context.mapping.discord_channel_id,
            message=cast(DiscordMessage, message),
            telegram=telegram,
            formatter=format_announcement_message,
            min_delay=0.0,
            max_delay=0.0,
        )
    elapsed = perf_counter() - start
    throughput = iterations / elapsed if elapsed else float("inf")
    print(f"forwarding: {iterations} iterations in {elapsed:.3f}s ({throughput:.1f} msg/s)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark formatting and forwarding hot paths",
    )
    parser.add_argument(
        "--format-count",
        type=int,
        default=1000,
        help="Number of formatting iterations",
    )
    parser.add_argument(
        "--forward-count",
        type=int,
        default=300,
        help="Number of forwarding iterations",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    benchmark_formatter(args.format_count)
    asyncio.run(benchmark_forwarding(args.forward_count))


if __name__ == "__main__":
    main()

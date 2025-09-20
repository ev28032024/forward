from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .config import MonitorConfig
from .monitor import run_monitor
from .structured_logging import configure_bridge_logging, log_event


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor Discord channels and forward updates to Telegram",
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Path to the YAML configuration file",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single iteration and exit",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (DEBUG, INFO, WARNING, ERROR)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    log_level = getattr(logging, args.log_level.upper(), logging.INFO)
    configure_bridge_logging(log_level)

    config_path = Path(args.config)
    try:
        config = MonitorConfig.from_file(config_path)
        asyncio.run(run_monitor(config, once=args.once))
    except (FileNotFoundError, ValueError, OSError) as exc:
        logging.getLogger(__name__).error("Failed to start monitor: %s", exc)
        log_event(
            "startup_failed",
            level=logging.ERROR,
            discord_channel_id=None,
            discord_message_id=None,
            telegram_chat_id=None,
            attempt=1,
            outcome="failure",
            latency_ms=None,
            extra={"reason": str(exc)},
        )
        sys.exit(1)


if __name__ == "__main__":
    main()

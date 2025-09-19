from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .config import MonitorConfig
from .monitor import run_monitor


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
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))

    config_path = Path(args.config)
    try:
        config = MonitorConfig.from_file(config_path)
        asyncio.run(run_monitor(config, once=args.once))
    except (FileNotFoundError, ValueError, OSError) as exc:
        logging.error("Failed to start monitor: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()

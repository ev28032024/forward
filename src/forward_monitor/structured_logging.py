from __future__ import annotations

import json
import logging
from collections.abc import Mapping, MutableMapping
from typing import Any, Final

BRIDGE_LOGGER_NAME: Final = "bridge"
_REDACT_KEYS: Final = {"token", "authorization"}
_MAX_STRING_LENGTH: Final = 512
_LOGGER = logging.getLogger(BRIDGE_LOGGER_NAME)


def configure_bridge_logging(level: int) -> None:
    logger = _LOGGER
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False


def log_event(
    event: str,
    *,
    level: int,
    discord_channel_id: int | None,
    discord_message_id: str | None,
    telegram_chat_id: str | None,
    attempt: int | None,
    outcome: str | None,
    latency_ms: float | None,
    extra: Mapping[str, Any] | None = None,
) -> None:
    payload: MutableMapping[str, Any] = {
        "event": event,
        "discord_channel_id": discord_channel_id,
        "discord_message_id": discord_message_id,
        "telegram_chat_id": telegram_chat_id,
        "attempt": attempt,
        "outcome": outcome,
        "latency_ms": round(latency_ms, 3) if latency_ms is not None else None,
    }

    if extra:
        for key, value in extra.items():
            if key is None:
                continue
            key_text = str(key)
            lower_key = key_text.lower()
            if lower_key in _REDACT_KEYS:
                payload[key_text] = "***"
                continue
            payload[key_text] = _sanitize_value(value)

    _LOGGER.log(level, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) > _MAX_STRING_LENGTH:
            return f"{value[:_MAX_STRING_LENGTH]}…"
        return value
    if isinstance(value, bool | int | float) or value is None:
        return value
    return str(value)

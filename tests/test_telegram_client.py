from __future__ import annotations
from pathlib import Path
from typing import Any, Iterable, List

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from forward_monitor import telegram_client
from forward_monitor.telegram_client import TelegramClient


class _FakeResponse:
    def __init__(self, status: int, *, text: str = "", json_payload: Any | None = None):
        self.status = status
        self._text = text
        self._json_payload = json_payload if json_payload is not None else {}
        self.headers: dict[str, str] = {}

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def text(self) -> str:
        return self._text

    async def json(self) -> Any:
        return self._json_payload


class _FakeSession:
    def __init__(self, responses: Iterable[_FakeResponse]):
        self._responses: List[_FakeResponse] = list(responses)
        self.calls = 0

    def post(self, url: str, *, json: dict[str, Any]) -> _FakeResponse:
        # aiohttp returns an awaitable context manager; our fake response already
        # implements the async context management protocol, so we simply return it.
        response = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return response


@pytest.mark.asyncio
async def test_post_respects_explicit_retry_statuses(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession([_FakeResponse(429, text="rate limited")])
    client = TelegramClient("token", session)  # type: ignore[arg-type]

    async def _unexpected_retry(_: Any) -> float:
        raise AssertionError("retry helper must not be called when retries are disabled")

    monkeypatch.setattr(telegram_client, "_retry_after_seconds", _unexpected_retry)

    with pytest.raises(RuntimeError):
        await client.send_message("chat", "hello", retry_statuses=[], retry_attempts=0)

    assert session.calls == 1


def test_normalise_retry_statuses_accepts_strings() -> None:
    statuses = telegram_client._normalise_retry_statuses([429, "503"])  # type: ignore[arg-type]
    assert statuses == {429, 503}

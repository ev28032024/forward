from __future__ import annotations

from typing import Any, Iterable, Mapping

import asyncio
import aiohttp
import pytest

from forward_monitor.discord_client import DISCORD_API_BASE, DiscordAPIError, DiscordClient


class _FakeResponse:
    def __init__(
        self,
        status: int,
        *,
        json_payload: Any | None = None,
        text: str = "",
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.status = status
        self._json_payload = json_payload if json_payload is not None else {}
        self._text = text
        self.headers = dict(headers or {"Content-Type": "application/json"})

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # pragma: no cover - protocol
        return None

    async def json(self) -> Any:
        return self._json_payload

    async def text(self) -> str:
        return self._text


class _FakeSession:
    def __init__(self, responses: Iterable[_FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> _FakeResponse:
        self.calls.append({"method": method, "url": url, "params": dict(params or {})})
        if not self._responses:
            raise AssertionError("No fake responses left to return")
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_fetch_messages_sorts_results_and_passes_params(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _sleep(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _sleep)

    response = _FakeResponse(
        200,
        json_payload=[{"id": "3"}, {"id": "1"}, {"id": "2"}],
    )
    session = _FakeSession([response])
    client = DiscordClient("token", session)  # type: ignore[arg-type]

    result = await client.fetch_messages(123, after="5", limit=10)

    assert [item["id"] for item in result] == ["1", "2", "3"]
    assert session.calls == [
        {
            "method": "GET",
            "url": f"{DISCORD_API_BASE}/channels/123/messages",
            "params": {"limit": "10", "after": "5"},
        }
    ]


@pytest.mark.asyncio
async def test_fetch_messages_waits_for_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded_delays: list[float] = []

    async def _sleep(delay: float) -> None:
        recorded_delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _sleep)

    session = _FakeSession(
        [
            _FakeResponse(429, json_payload={"retry_after": 0.25}),
            _FakeResponse(200, json_payload=[{"id": "1"}]),
        ]
    )
    client = DiscordClient("token", session)  # type: ignore[arg-type]

    messages = await client.fetch_messages(42)

    assert messages == [{"id": "1"}]
    assert recorded_delays == [pytest.approx(0.25, abs=0.01)]
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_request_json_retries_on_network_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[float] = []

    async def _sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _sleep)

    class FlakySession:
        def __init__(self) -> None:
            self.calls = 0

        def request(
            self,
            method: str,
            url: str,
            *,
            headers: Mapping[str, str] | None = None,
            params: Mapping[str, str] | None = None,
        ) -> _FakeResponse:
            self.calls += 1
            if self.calls == 1:
                raise aiohttp.ClientConnectionError()
            return _FakeResponse(200, json_payload={"ok": True})

    session = FlakySession()
    client = DiscordClient("token", session)  # type: ignore[arg-type]

    result = await client._request_json(  # pylint: disable=protected-access
        "GET",
        "https://example.com",
        max_network_retries=1,
    )

    assert result == {"ok": True}
    assert session.calls == 2
    assert delays == [1]


@pytest.mark.asyncio
async def test_request_json_raises_after_rate_limit_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _sleep(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _sleep)

    session = _FakeSession(
        [
            _FakeResponse(429, json_payload={"retry_after": 0}, text="retry"),
            _FakeResponse(429, json_payload={"retry_after": 0}, text="retry"),
        ]
    )
    client = DiscordClient("token", session)  # type: ignore[arg-type]

    with pytest.raises(DiscordAPIError):
        await client._request_json("GET", "https://example.com", max_rate_limit_retries=1)

    assert len(session.calls) == 2

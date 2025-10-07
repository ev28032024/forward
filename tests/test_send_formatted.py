import asyncio

from forward_monitor.models import FormattedTelegramMessage
from forward_monitor.telegram import (
    _CHUNK_CONTINUE_BANNER,
    _CHUNK_CONTINUE_HINT,
    _CHUNK_END_BANNER,
    _CHUNK_START_BANNER,
    send_formatted,
)


class RecordingAPI:
    def __init__(self) -> None:
        self.messages: list[tuple[int | str, str]] = []

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        parse_mode: str | None = None,
        disable_preview: bool = True,
        message_thread_id: int | None = None,
    ) -> None:
        self.messages.append((chat_id, text))

    async def send_photo(
        self,
        chat_id: int | str,
        photo: str,
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
        message_thread_id: int | None = None,
    ) -> None:
        raise AssertionError("photos are not expected in tests")


def test_send_formatted_adds_separators() -> None:
    async def runner() -> None:
        api = RecordingAPI()
        message = FormattedTelegramMessage(
            text="Первый блок",
            extra_messages=("Второй блок", "Третий блок"),
            parse_mode="HTML",
            disable_preview=True,
        )

        await send_formatted(api, "chat", message)

        assert len(api.messages) == 3
        first = api.messages[0][1]
        second = api.messages[1][1]
        third = api.messages[2][1]

        assert first.startswith(_CHUNK_START_BANNER)
        assert first.rstrip().endswith(_CHUNK_CONTINUE_HINT)
        assert second.startswith(_CHUNK_CONTINUE_BANNER)
        assert second.rstrip().endswith(_CHUNK_CONTINUE_HINT)
        assert third.startswith(_CHUNK_CONTINUE_BANNER)
        assert third.rstrip().endswith(_CHUNK_END_BANNER)

    asyncio.run(runner())


def test_send_formatted_single_chunk_without_separators() -> None:
    async def runner() -> None:
        api = RecordingAPI()
        message = FormattedTelegramMessage(
            text="Одиночное сообщение",
            extra_messages=(),
            parse_mode="HTML",
            disable_preview=True,
        )

        await send_formatted(api, "chat", message)

        assert len(api.messages) == 1
        assert api.messages[0][1] == "Одиночное сообщение"

    asyncio.run(runner())

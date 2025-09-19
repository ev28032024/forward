from __future__ import annotations

from pathlib import Path
import textwrap

import pytest

from forward_monitor.config import MonitorConfig


def _write_config(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")


def test_state_file_relative_to_config(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "config.yml"

    _write_config(
        config_path,
        """
        discord_token: discord
        telegram_token: telegram
        telegram_chat_id: "@chat"
        state_file: state/monitor.json
        """,
    )

    config = MonitorConfig.from_file(config_path)

    expected = config_dir / "state" / "monitor.json"
    assert config.state_file == expected


def test_default_state_file_uses_config_directory(tmp_path: Path) -> None:
    config_path = tmp_path / "forward.yml"

    _write_config(
        config_path,
        """
        discord_token: discord
        telegram_token: telegram
        telegram_chat_id: "@chat"
        """,
    )

    config = MonitorConfig.from_file(config_path)

    assert config.state_file == tmp_path / "monitor_state.json"


def test_negative_poll_interval_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "forward.yml"

    _write_config(
        config_path,
        """
        discord_token: discord
        telegram_token: telegram
        telegram_chat_id: "@chat"
        poll_interval: -5
        """,
    )

    with pytest.raises(ValueError):
        MonitorConfig.from_file(config_path)


def test_zero_poll_interval_allowed(tmp_path: Path) -> None:
    config_path = tmp_path / "forward.yml"

    _write_config(
        config_path,
        """
        discord_token: discord
        telegram_token: telegram
        telegram_chat_id: "@chat"
        poll_interval: 0
        """,
    )

    config = MonitorConfig.from_file(config_path)

    assert config.poll_interval == 0


@pytest.mark.parametrize(
    "field, value",
    [
        ("discord_token", ""),
        ("discord_token", "   "),
        ("telegram_token", ""),
        ("telegram_token", "   "),
        ("telegram_chat_id", ""),
        ("telegram_chat_id", "   "),
    ],
)
def test_required_tokens_cannot_be_empty(tmp_path: Path, field: str, value: str) -> None:
    config_path = tmp_path / "forward.yml"

    discord_value = value if field == "discord_token" else "discord"
    telegram_value = value if field == "telegram_token" else "telegram"
    chat_value = value if field == "telegram_chat_id" else "@chat"

    _write_config(
        config_path,
        f"""
        discord_token: "{discord_value}"
        telegram_token: "{telegram_value}"
        telegram_chat_id: "{chat_value}"
        """,
    )

    with pytest.raises(ValueError):
        MonitorConfig.from_file(config_path)


def test_customization_replacements_allow_empty_strings(tmp_path: Path) -> None:
    config_path = tmp_path / "forward.yml"

    _write_config(
        config_path,
        """
        discord_token: discord
        telegram_token: telegram
        telegram_chat_id: "@chat"
        customization:
          replacements:
            - find: "Secret"
              replace: ""
        """,
    )

    config = MonitorConfig.from_file(config_path)
    prepared = config.customization.prepare()
    assert prepared.replacements == (("Secret", ""),)
    assert prepared.apply("Secret") == ""


def test_channel_mapping_requires_non_empty_chat(tmp_path: Path) -> None:
    config_path = tmp_path / "forward.yml"

    _write_config(
        config_path,
        """
        discord_token: discord
        telegram_token: telegram
        telegram_chat_id: "@chat"
        announcement_channels:
          - discord_channel_id: 1
            telegram_chat_id: "   "
        """,
    )

    with pytest.raises(ValueError):
        MonitorConfig.from_file(config_path)


def test_channel_mapping_captures_display_name(tmp_path: Path) -> None:
    config_path = tmp_path / "forward.yml"

    _write_config(
        config_path,
        """
        discord_token: discord
        telegram_token: telegram
        telegram_chat_id: "@chat"
        announcement_channels:
          - discord_channel_id: 1
            telegram_chat_id: "@chat"
            display_name: "Announcements"
        """,
    )

    config = MonitorConfig.from_file(config_path)
    assert config.announcement_channels[0].display_name == "Announcements"


def test_invalid_message_type_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "forward.yml"

    _write_config(
        config_path,
        """
        discord_token: discord
        telegram_token: telegram
        telegram_chat_id: "@chat"
        filters:
          allowed_types: [text, unknown]
        """,
    )

    with pytest.raises(ValueError):
        MonitorConfig.from_file(config_path)

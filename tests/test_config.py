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

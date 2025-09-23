from __future__ import annotations

import argparse
import logging
from pathlib import Path

import pytest

import forward_monitor.__main__ as cli


def test_main_reports_missing_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    missing_path = tmp_path / "absent.yml"

    def fake_parse_args() -> argparse.Namespace:
        return argparse.Namespace(
            config=str(missing_path),
            once=True,
            log_level="INFO",
            profile=[],
            overrides=[],
        )

    monkeypatch.setattr(cli, "parse_args", fake_parse_args)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(SystemExit) as exc:
            cli.main()

    assert exc.value.code == 1
    assert str(missing_path) in caplog.text


def test_main_rejects_invalid_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    config_path = tmp_path / "config.yml"
    config_path.write_text("telegram:\n  token: t\n  chat: c\ndiscord:\n  token: d\nforward:\n  channels:\n    - discord: 1\n      telegram: c\n", encoding="utf-8")

    def fake_parse_args() -> argparse.Namespace:
        return argparse.Namespace(
            config=str(config_path),
            once=True,
            log_level="INFO",
            profile=[],
            overrides=["runtime.poll_every"],
        )

    monkeypatch.setattr(cli, "parse_args", fake_parse_args)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(SystemExit) as exc:
            cli.main()

    assert exc.value.code == 1
    assert "Invalid override" in caplog.text

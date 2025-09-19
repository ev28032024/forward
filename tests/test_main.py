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
        )

    monkeypatch.setattr(cli, "parse_args", fake_parse_args)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(SystemExit) as exc:
            cli.main()

    assert exc.value.code == 1
    assert str(missing_path) in caplog.text

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, cast

import pytest

import forward_monitor.state as state_module


def test_monitor_state_writes_only_when_dirty(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    monitor_state = state_module.MonitorState(state_path)

    call_count = 0
    real_dump: Callable[..., None] = state_module.json.dump

    def counting_dump(*args: object, **kwargs: object) -> None:
        nonlocal call_count
        call_count += 1
        real_dump(*args, **kwargs)

    # Patch json.dump inside the module to track write attempts.
    state_module.json.dump = cast(Any, counting_dump)
    try:
        monitor_state.save()
        assert call_count == 0

        monitor_state.update_last_message_id(123, "456")
        monitor_state.save()
        assert call_count == 1

        # Saving again without changes should be a no-op.
        monitor_state.save()
        assert call_count == 1

        # Writing the same value should not mark the state dirty again.
        monitor_state.update_last_message_id(123, "456")
        monitor_state.save()
        assert call_count == 1

        # A new value should trigger another flush.
        monitor_state.update_last_message_id(123, "789")
        monitor_state.save()
        assert call_count == 2
    finally:
        state_module.json.dump = cast(Any, real_dump)


def test_load_rotates_backup_without_collision(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    backup_path = state_path.with_suffix(".bak")

    state_path.write_text("{not json}", encoding="utf-8")
    state_module.MonitorState(state_path)

    assert backup_path.exists()
    first_backup = backup_path.read_text(encoding="utf-8")

    state_path.write_text("{still not json}", encoding="utf-8")
    state_module.MonitorState(state_path)

    assert backup_path.exists()
    assert backup_path.read_text(encoding="utf-8") == "{still not json}"
    assert not state_path.exists()
    assert first_backup != backup_path.read_text(encoding="utf-8")


def test_monitor_state_atomic_save(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_path = tmp_path / "state.json"
    monitor_state = state_module.MonitorState(state_path)
    monitor_state.update_last_message_id(123, "456")
    monitor_state.save()

    original_contents = state_path.read_text(encoding="utf-8")
    monitor_state.update_last_message_id(123, "789")

    real_replace = state_module.Path.replace
    call_count = 0

    def flaky_replace(self: Path, target: Path) -> Path:
        nonlocal call_count
        call_count += 1
        if call_count == 1 and self.name.endswith(".tmp"):
            raise OSError("simulated failure")
        return real_replace(self, target)

    monkeypatch.setattr(state_module.Path, "replace", flaky_replace, raising=False)

    with pytest.raises(OSError):
        monitor_state.save()

    assert state_path.read_text(encoding="utf-8") == original_contents
    assert not state_path.with_name("state.json.tmp").exists()

    monitor_state.save()

    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert data["last_message_ids"]["123"] == "789"


def test_monitor_state_flushes_to_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_path = tmp_path / "state.json"
    monitor_state = state_module.MonitorState(state_path)
    monitor_state.update_last_message_id(1, "2")

    called = False

    def tracking_fsync(fd: int) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(state_module.os, "fsync", tracking_fsync)

    monitor_state.save()

    assert called

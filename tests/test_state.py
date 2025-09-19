from __future__ import annotations

import json
from typing import Callable

import pytest

import forward_monitor.state as state_module


def test_monitor_state_writes_only_when_dirty(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    monitor_state = state_module.MonitorState(state_path)

    call_count = 0
    real_dump: Callable[..., None] = state_module.json.dump

    def counting_dump(*args, **kwargs):  # type: ignore[override]
        nonlocal call_count
        call_count += 1
        return real_dump(*args, **kwargs)

    # Patch json.dump inside the module to track write attempts.
    state_module.json.dump = counting_dump  # type: ignore[assignment]
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
        state_module.json.dump = real_dump  # type: ignore[assignment]


def test_load_rotates_backup_without_collision(tmp_path) -> None:
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


def test_monitor_state_atomic_save(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_path = tmp_path / "state.json"
    monitor_state = state_module.MonitorState(state_path)
    monitor_state.update_last_message_id(123, "456")
    monitor_state.save()

    original_contents = state_path.read_text(encoding="utf-8")
    monitor_state.update_last_message_id(123, "789")

    real_replace = state_module.Path.replace
    call_count = 0

    def flaky_replace(self, target):  # type: ignore[no-untyped-def]
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


def test_monitor_state_flushes_to_disk(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_path = tmp_path / "state.json"
    monitor_state = state_module.MonitorState(state_path)
    monitor_state.update_last_message_id(1, "2")

    called = False

    def tracking_fsync(fd: int) -> None:  # type: ignore[no-untyped-def]
        nonlocal called
        called = True

    monkeypatch.setattr(state_module.os, "fsync", tracking_fsync)

    monitor_state.save()

    assert called

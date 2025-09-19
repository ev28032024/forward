from __future__ import annotations

from typing import Callable

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

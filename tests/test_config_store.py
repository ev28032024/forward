from __future__ import annotations

from pathlib import Path

from forward_monitor.config_store import ConfigStore


def test_channel_lifecycle(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "db.sqlite")
    store.set_setting("formatting.disable_preview", "false")
    store.add_filter(0, "whitelist", "hello")

    record = store.add_channel("123", "456", "Label")
    store.set_channel_option(record.id, "formatting.attachments_style", "links")
    store.set_last_message(record.id, "900")

    configs = store.load_channel_configurations()
    assert len(configs) == 1
    channel = configs[0]
    assert channel.label == "Label"
    assert channel.formatting.disable_preview is False
    assert channel.formatting.attachments_style == "links"
    assert channel.filters.whitelist == {"hello"}
    assert channel.last_message_id == "900"

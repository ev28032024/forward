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


def test_filter_management(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "filters.sqlite")

    assert store.add_filter(0, "whitelist", "Hello") is True
    assert store.add_filter(0, "whitelist", "hello") is False
    assert store.get_filter_config(0).whitelist == {"Hello"}

    removed = store.remove_filter(0, "whitelist", "HELLO")
    assert removed == 1
    assert store.remove_filter(0, "whitelist", "HELLO") == 0

    assert store.add_filter(0, "allowed_senders", " 1090758325299314818 ") is True
    assert store.add_filter(0, "allowed_senders", "1090758325299314818") is False
    assert store.add_filter(0, "allowed_senders", "@CoDeD") is True
    allowed = store.get_filter_config(0).allowed_senders
    assert allowed == {"1090758325299314818", "coded"}

    assert store.remove_filter(0, "allowed_senders", "@coded") == 1
    assert store.remove_filter(0, "allowed_senders", "coded") == 0

    store.add_filter(0, "blacklist", "Spam")
    assert store.get_filter_config(0).blacklist == {"Spam"}
    cleared = store.clear_filters(0)
    assert cleared >= 1
    config = store.get_filter_config(0)
    assert not any(
        getattr(config, name)
        for name in (
            "whitelist",
            "blacklist",
            "allowed_senders",
            "blocked_senders",
            "allowed_types",
            "blocked_types",
        )
    )

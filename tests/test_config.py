import textwrap
from pathlib import Path
from typing import Any, Mapping

import pytest
from yaml import safe_dump

from forward_monitor.config import (
    ConfigOverride,
    MonitorConfig,
    parse_override_expression,
)


def _write_config(path: Path, content: str | Mapping[str, Any]) -> None:
    if isinstance(content, str):
        text = textwrap.dedent(content).strip() + "\n"
        path.write_text(text, encoding="utf-8")
        return
    rendered = safe_dump(content, sort_keys=False).rstrip() + "\n"
    path.write_text(rendered, encoding="utf-8")


def _base_config(extra: str = "") -> str:
    base = textwrap.dedent(
        """
        telegram:
          token: telegram
          chat: "@chat"
        discord:
          token: discord
        forward:
          channels:
            - discord: 1
              telegram: "-100"
        """
    ).strip()
    extra_text = textwrap.dedent(extra).strip()
    if extra_text:
        return f"{base}\n{extra_text}\n"
    return base + "\n"


def test_state_file_relative_to_config(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "config.yml"

    _write_config(
        config_path,
        _base_config(
            """
            runtime:
              state_file: state/monitor.json
            """
        ),
    )

    config = MonitorConfig.from_file(config_path)
    expected = config_dir / "state" / "monitor.json"
    assert config.runtime.state_file == expected


def test_default_state_file_uses_config_directory(tmp_path: Path) -> None:
    config_path = tmp_path / "forward.yml"
    _write_config(config_path, _base_config())

    config = MonitorConfig.from_file(config_path)
    assert config.runtime.state_file == tmp_path / "monitor_state.json"


def test_discord_token_type_defaults_to_auto(tmp_path: Path) -> None:
    config_path = tmp_path / "forward.yml"
    _write_config(config_path, _base_config())

    config = MonitorConfig.from_file(config_path)
    assert config.discord.token_type == "auto"


def test_discord_token_type_normalized(tmp_path: Path) -> None:
    config_path = tmp_path / "forward.yml"
    _write_config(
        config_path,
        """
        telegram:
          token: telegram
          chat: "@chat"
        discord:
          token: discord
          token_type: BOT
        forward:
          channels:
            - discord: 1
              telegram: "-100"
        """,
    )

    config = MonitorConfig.from_file(config_path)
    assert config.discord.token_type == "bot"


def test_discord_token_type_invalid_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "forward.yml"
    _write_config(
        config_path,
        """
        telegram:
          token: telegram
          chat: "@chat"
        discord:
          token: discord
          token_type: something
        forward:
          channels:
            - discord: 1
              telegram: "-100"
        """,
    )

    with pytest.raises(ValueError):
        MonitorConfig.from_file(config_path)


def test_negative_poll_interval_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "forward.yml"
    _write_config(
        config_path,
        _base_config(
            """
            runtime:
              poll_every: -5
            """
        ),
    )

    with pytest.raises(ValueError):
        MonitorConfig.from_file(config_path)


def test_zero_poll_interval_allowed(tmp_path: Path) -> None:
    config_path = tmp_path / "forward.yml"
    _write_config(
        config_path,
        _base_config(
            """
            runtime:
              poll_every: 0
            """
        ),
    )

    config = MonitorConfig.from_file(config_path)
    assert config.runtime.poll_interval == 0


def test_runtime_caps_parsed(tmp_path: Path) -> None:
    config_path = tmp_path / "forward.yml"
    _write_config(
        config_path,
        _base_config(
            """
            runtime:
              max_messages: 250
              max_fetch_seconds: 12.5
            """
        ),
    )

    config = MonitorConfig.from_file(config_path)
    assert config.runtime.max_messages_per_channel == 250
    assert config.runtime.max_fetch_seconds == pytest.approx(12.5)


@pytest.mark.parametrize(
    "field, value",
    [("max_messages", 0), ("max_fetch_seconds", 0)],
)
def test_runtime_caps_must_be_positive(
    tmp_path: Path, field: str, value: int
) -> None:
    config_path = tmp_path / "forward.yml"
    _write_config(
        config_path,
        _base_config(
            f"""
            runtime:
              {field}: {value}
            """
        ),
    )

    with pytest.raises(ValueError):
        MonitorConfig.from_file(config_path)


@pytest.mark.parametrize(
    "content",
    [
        """
        telegram:
          token: ""
          chat: "@chat"
        discord:
          token: discord
        forward:
          channels:
            - discord: 1
              telegram: "-100"
        """,
        """
        telegram:
          token: telegram
          chat: ""
        discord:
          token: discord
        forward:
          channels:
            - discord: 1
              telegram: "-100"
        """,
        """
        telegram:
          token: telegram
          chat: "@chat"
        discord:
          token: ""
        forward:
          channels:
            - discord: 1
              telegram: "-100"
        """,
    ],
)
def test_required_tokens_cannot_be_empty(tmp_path: Path, content: str) -> None:
    config_path = tmp_path / "forward.yml"
    _write_config(config_path, content)

    with pytest.raises(ValueError):
        MonitorConfig.from_file(config_path)


def test_customization_replacements_allow_empty_strings(tmp_path: Path) -> None:
    config_path = tmp_path / "forward.yml"
    _write_config(
        config_path,
        """
        telegram:
          token: telegram
          chat: "@chat"
        discord:
          token: discord
        forward:
          defaults:
            text:
              replacements:
                - find: Secret
                  replace: ""
          channels:
            - discord: 1
              telegram: "-100"
        """,
    )

    config = MonitorConfig.from_file(config_path)
    prepared = config.defaults.customization.prepare()
    assert prepared.replacements == (("Secret", ""),)
    rendered = prepared.render("Secret")
    assert rendered.body_lines == ()


def test_channel_mapping_requires_non_empty_chat(tmp_path: Path) -> None:
    config_path = tmp_path / "forward.yml"
    _write_config(
        config_path,
        """
        telegram:
          token: telegram
          chat: ""
        discord:
          token: discord
        forward:
          channels:
            - discord: 1
              telegram: "   "
        """,
    )

    with pytest.raises(ValueError):
        MonitorConfig.from_file(config_path)


def test_channel_mapping_captures_display_name(tmp_path: Path) -> None:
    config_path = tmp_path / "forward.yml"
    _write_config(
        config_path,
        """
        telegram:
          token: telegram
          chat: "@chat"
        discord:
          token: discord
        forward:
          channels:
            - discord: 1
              telegram: "-100"
              name: Announcements
        """,
    )

    config = MonitorConfig.from_file(config_path)
    assert config.channels[0].display_name == "Announcements"


def test_invalid_message_type_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "forward.yml"
    _write_config(
        config_path,
        """
        telegram:
          token: telegram
          chat: "@chat"
        discord:
          token: discord
        forward:
          defaults:
            filters:
              allowed_types: [text, unknown]
          channels:
            - discord: 1
              telegram: "-100"
        """,
    )

    with pytest.raises(ValueError):
        MonitorConfig.from_file(config_path)


def test_formatting_defaults_merge(tmp_path: Path) -> None:
    config_path = tmp_path / "forward.yml"
    _write_config(
        config_path,
        """
        telegram:
          token: telegram
          chat: "@chat"
          formatting:
            parse_mode: MarkdownV2
            disable_preview: false
        discord:
          token: discord
        forward:
          defaults:
            formatting:
              attachments: compact
          channels:
            - discord: 1
              telegram: "-100"
              formatting:
                attachments: minimal
        """,
    )

    config = MonitorConfig.from_file(config_path)
    channel_formatting = config.channels[0].formatting
    assert channel_formatting.parse_mode == "MarkdownV2"
    assert channel_formatting.disable_link_preview is False
    assert channel_formatting.attachments_style == "minimal"


def test_proxy_credentials_and_rotation_parsing(tmp_path: Path) -> None:
    config_path = tmp_path / "forward.yml"
    _write_config(
        config_path,
        _base_config(
            """
            network:
              proxies:
                username: base-user
                password: base-pass
                rotate_url: https://rotate.example
                pool:
                  - http://proxy.example:8080
                telegram:
                  auth: tg-user:tg-pass
                  rotate: https://rotate.telegram
                  pool:
                    - http://tg-proxy.example:9000
            """
        ),
    )

    config = MonitorConfig.from_file(config_path)

    default_proxy = config.network.default_proxy
    assert default_proxy.username == "base-user"
    assert default_proxy.password == "base-pass"
    assert default_proxy.rotate_url == "https://rotate.example"

    telegram_proxy = config.network.proxy_for_service("telegram")
    assert telegram_proxy.username == "tg-user"
    assert telegram_proxy.password == "tg-pass"
    assert telegram_proxy.rotate_url == "https://rotate.telegram"


def test_named_profile_merges_before_overrides(tmp_path: Path) -> None:
    config_path = tmp_path / "forward.yml"
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()

    _write_config(config_path, _base_config())
    _write_config(profiles_dir / "nightly.yml", {"runtime": {"poll_every": 42}})

    config = MonitorConfig.from_file(config_path, profiles=["nightly"])
    assert config.runtime.poll_interval == 42


def test_environment_profiles_are_applied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "forward.yml"
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()

    _write_config(config_path, _base_config())
    _write_config(profiles_dir / "slow.yaml", {"runtime": {"poll_every": 600}})

    monkeypatch.setenv("FORWARD_MONITOR_PROFILE", "slow")

    config = MonitorConfig.from_file(config_path)
    assert config.runtime.poll_interval == 600


def test_override_priority_cli_over_env_and_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "forward.yml"
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()

    _write_config(
        config_path,
        _base_config(
            """
            runtime:
              poll_every: 15
            """
        ),
    )
    _write_config(profiles_dir / "fast.yml", {"runtime": {"poll_every": 30}})

    monkeypatch.setenv("FORWARD_MONITOR_PROFILE", "fast")
    monkeypatch.setenv("FORWARD_MONITOR__RUNTIME__POLL_EVERY", "45")

    override = parse_override_expression("runtime.poll_every=60")
    config = MonitorConfig.from_file(config_path, overrides=[override])

    assert config.runtime.poll_interval == 60


def test_environment_override_updates_nested_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "forward.yml"
    _write_config(config_path, _base_config())

    monkeypatch.setenv("FORWARD_MONITOR__TELEGRAM__TOKEN", "env-token")
    monkeypatch.setenv("FORWARD_MONITOR__FORWARD__CHANNELS__0__TELEGRAM", "\"@env\"")

    config = MonitorConfig.from_file(config_path)
    assert config.telegram.token == "env-token"
    assert config.channels[0].telegram_chat_id == "@env"


def test_override_invalid_path_raises(tmp_path: Path) -> None:
    config_path = tmp_path / "forward.yml"
    _write_config(config_path, _base_config())

    with pytest.raises(ValueError):
        MonitorConfig.from_file(
            config_path,
            overrides=[ConfigOverride(path=("forward", "channels", 5, "telegram"), value="@bad")],
        )


def test_missing_profile_raises_error(tmp_path: Path) -> None:
    config_path = tmp_path / "forward.yml"
    _write_config(config_path, _base_config())

    with pytest.raises(FileNotFoundError):
        MonitorConfig.from_file(config_path, profiles=["absent"])

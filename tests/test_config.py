"""配置层：缺失项要给出可读提示，而不是 pydantic 堆栈。"""

from __future__ import annotations

from pathlib import Path

import pytest

from bot.core.config import ENV_NAMES, ConfigError, Settings, load_settings

REQUIRED = ("DISCORD_TOKEN", "GUILD_ID", "OWNER_ID")


@pytest.fixture(autouse=True)
def _clear_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*REQUIRED, "MOD_LOG_CHANNEL_ID"):
        monkeypatch.delenv(name, raising=False)


def test_all_required_missing_are_listed(tmp_path) -> None:
    env = tmp_path / ".env"
    env.write_text("LOG_LEVEL=DEBUG\n", encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        load_settings(env)

    message = str(excinfo.value)
    for name in REQUIRED:
        assert name in message
    assert "Traceback" not in message
    assert "pydantic" not in message


def test_partially_filled_config_names_the_missing_one(tmp_path) -> None:
    env = tmp_path / ".env"
    env.write_text("DISCORD_TOKEN=abc\nGUILD_ID=123\n", encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        load_settings(env)

    assert "OWNER_ID" in str(excinfo.value)


def test_blank_values_are_reported(tmp_path) -> None:
    env = tmp_path / ".env"
    env.write_text("DISCORD_TOKEN=\nGUILD_ID=\nOWNER_ID=1\n", encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        load_settings(env)

    message = str(excinfo.value)
    assert "DISCORD_TOKEN" in message
    assert "GUILD_ID" in message


def test_valid_config_is_loaded(tmp_path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "DISCORD_TOKEN=abc\nGUILD_ID=123\nOWNER_ID=456\nMOD_LOG_CHANNEL_ID=789\nDATABASE_PATH=custom/bot.db\n"
        "GITHUB_OAUTH_CLIENT_ID=cid\nGITHUB_OAUTH_CLIENT_SECRET=sec\n"
        "GITHUB_OAUTH_REDIRECT_URI=http://127.0.0.1:8080/github/callback\n"
        "WEB_PORT=8080\n",
        encoding="utf-8",
    )

    settings = load_settings(env)

    assert settings.discord_token == "abc"
    assert settings.guild_id == 123
    assert settings.owner_id == 456
    assert settings.mod_log_channel_id == 789
    assert settings.database_path.name == "bot.db"
    assert settings.default_locale == "en-US"
    assert settings.github_oauth_client_id == "cid"
    assert settings.github_oauth_client_secret == "sec"
    assert settings.github_oauth_redirect_uri == "http://127.0.0.1:8080/github/callback"
    assert settings.web_port == 8080
    assert settings.web_host == "127.0.0.1"


def test_optional_fields_keep_their_defaults(tmp_path) -> None:
    env = tmp_path / ".env"
    env.write_text("DISCORD_TOKEN=abc\nGUILD_ID=123\nOWNER_ID=456\n", encoding="utf-8")

    settings = load_settings(env)

    # 没配 GitHub / 入站端点也要能启动：入站端点默认不监听
    assert settings.github_oauth_client_id is None
    assert settings.github_oauth_client_secret is None
    assert settings.github_oauth_redirect_uri is None
    assert settings.web_port == 0
    assert settings.web_host == "127.0.0.1"


def test_a_broken_optional_value_names_that_field(tmp_path) -> None:
    """必填三项都填好了，就别再让人回去翻 token——直接点名出错的那一项。"""
    env = tmp_path / ".env"
    env.write_text("DISCORD_TOKEN=abc\nGUILD_ID=123\nOWNER_ID=456\nWEB_PORT=not-a-number\n", encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        load_settings(env)

    message = str(excinfo.value)
    assert "WEB_PORT" in message
    assert "必填项都齐了" in message


def test_env_example_documents_every_setting() -> None:
    """`.env.example` 与 `ENV_NAMES` 必须覆盖 Settings 的每个字段。

    「加了配置项但没人知道要填」是这类项目最常见的漂移，两条断言把它按住：
    一条保证登记表不漏字段，一条保证示例文件不漏变量。
    """
    root = Path(__file__).resolve().parents[1]
    example = (root / ".env.example").read_text(encoding="utf-8")

    assert set(ENV_NAMES) == set(Settings.model_fields)
    missing = [env_name for env_name in ENV_NAMES.values() if env_name not in example]
    assert missing == []

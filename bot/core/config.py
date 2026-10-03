"""配置层：从 .env 读取设置，缺失关键项时给出可读的提示而不是堆栈。"""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(RuntimeError):
    """配置缺失或非法。消息面向运维者，不是面向用户。"""


# 字段名 → 环境变量名。用于把校验错误翻译成「去 .env 里填哪一项」。
ENV_NAMES: dict[str, str] = {
    "discord_token": "DISCORD_TOKEN",
    "guild_id": "GUILD_ID",
    "owner_id": "OWNER_ID",
    "mod_log_channel_id": "MOD_LOG_CHANNEL_ID",
    "database_path": "DATABASE_PATH",
    "log_dir": "LOG_DIR",
    "log_level": "LOG_LEVEL",
    "default_locale": "DEFAULT_LOCALE",
    "github_oauth_client_id": "GITHUB_OAUTH_CLIENT_ID",
    "github_oauth_client_secret": "GITHUB_OAUTH_CLIENT_SECRET",
    "github_oauth_redirect_uri": "GITHUB_OAUTH_REDIRECT_URI",
    "github_token": "GITHUB_TOKEN",
    "watcher_base_url": "WATCHER_BASE_URL",
    "watcher_api_token": "WATCHER_API_TOKEN",
    "access_base_url": "ACCESS_BASE_URL",
    "access_service_id": "ACCESS_SERVICE_ID",
    "access_service_secret": "ACCESS_SERVICE_SECRET",
    "web_host": "WEB_HOST",
    "web_port": "WEB_PORT",
}

REQUIRED_ENV_NAMES: tuple[str, ...] = ("DISCORD_TOKEN", "GUILD_ID", "OWNER_ID")


class Settings(BaseSettings):
    """运行配置。字段名即环境变量名（大小写不敏感）。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    discord_token: str
    guild_id: int
    owner_id: int
    mod_log_channel_id: int | None = None
    database_path: Path = Path("data/bot.db")
    log_dir: Path = Path("data/logs")
    log_level: str = "INFO"
    default_locale: str = "en-US"
    # --- 阶段三才用得上：没配也能启动，只是相关功能会明确说「未配置」。---
    # 设备流只需要 client_id；web flow 还额外需要 client_secret 与回调地址。
    github_oauth_client_id: str | None = None
    github_oauth_client_secret: str | None = None
    github_oauth_redirect_uri: str | None = None
    # 读公开仓库的 release 用它。**不勾任何 scope 的 classic PAT 就够**：
    # 只能读，泄露也改不了仓库；换来 5000 次/小时的额度（匿名只有 60）。
    github_token: str | None = None
    # 用户的 gh-webhook-watcher（GitHub → webhook 的监听服务）。配了才开常驻订阅；
    # 订阅走 SSE，断线重连的退避用服务端帧里给的 retry，所以这里没有轮询间隔。
    watcher_base_url: str | None = None
    watcher_api_token: str | None = None
    # SMAS：机器人以**自己的服务号**身份连过去（不代替用户行事）。
    # 三项齐了才会启用节点→角色同步与发 key；缺了就明确说「未配置」，不会静默什么都不做。
    access_base_url: str | None = None
    access_service_id: str = "discord-bot"
    access_service_secret: str | None = None
    # 入站 HTTP 端点（web flow 的 OAuth 回调、以后的 webhook 都用它）。
    # 默认绑 127.0.0.1：对外暴露交给反向代理或隧道，机器人自己不开公网监听。
    web_host: str = "127.0.0.1"
    web_port: int = 0

    @field_validator("discord_token")
    @classmethod
    def _token_must_not_be_blank(cls, value: str) -> str:
        """``.env.example`` 里 token 是空的；照抄过来必须当成「没填」。"""
        if not value.strip():
            raise ValueError("不能为空")
        return value.strip()


def load_settings(env_file: str | Path | None = None) -> Settings:
    """读取配置。失败时抛 :class:`ConfigError`，消息里逐项指出缺了什么。"""
    try:
        if env_file is None:
            return Settings()
        return Settings(_env_file=env_file)
    except ValidationError as exc:
        raise ConfigError(format_config_error(exc)) from exc


def format_config_error(exc: ValidationError) -> str:
    """把 pydantic 的校验错误整理成一段运维可读的话。"""
    missing: list[str] = []
    invalid: list[str] = []
    required_hit = False
    for error in exc.errors():
        field = str(error["loc"][0]) if error["loc"] else ""
        env_name = ENV_NAMES.get(field, field.upper())
        required_hit = required_hit or env_name in REQUIRED_ENV_NAMES
        if error["type"] in {"missing", "int_parsing", "int_type", "string_too_short"}:
            missing.append(f"  - {env_name}")
        else:
            invalid.append(f"  - {env_name}：{error['msg']}")

    lines = ["配置不完整，无法启动。"]
    if required_hit:
        lines.append("请复制 .env.example 为 .env，并至少填写：" + " / ".join(REQUIRED_ENV_NAMES))
    else:
        # 三项必填都在，问题出在别的项上——别让人回去翻 token。
        lines.append("必填项都齐了，问题在下面这几项：")
    if missing:
        lines.append("")
        lines.append("缺失或无法解析的项：")
        lines.extend(missing)
    if invalid:
        lines.append("")
        lines.append("取值非法的项：")
        lines.extend(invalid)
    return "\n".join(lines)

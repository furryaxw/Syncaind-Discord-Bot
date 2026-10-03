"""入口：``python -m bot``。"""

from __future__ import annotations

import os
import sys

import discord

from .core.bot import SyncaindBot
from .core.config import ConfigError, load_settings
from .core.instance_lock import LOCK_FILE_NAME, AlreadyRunningError, InstanceLock
from .core.logging import ensure_utf8_streams, setup_logging
from .core.run_loop import run_bot

EXIT_CONFIG_ERROR = 2
EXIT_LOGIN_FAILURE = 3
EXIT_PRIVILEGED_INTENTS = 4
EXIT_ALREADY_RUNNING = 5


def main() -> int:
    # 配置错误要在这之前就打出来，所以先单独把输出流切成 UTF-8。
    ensure_utf8_streams()

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CONFIG_ERROR

    logger = setup_logging(settings.log_dir, settings.log_level)

    # 单实例守卫放在最前面：两个进程抢同一个 gateway 会话时，交互会时好时坏且不留日志，
    # 那种现象极难排查，不如在这里一次拦住。
    lock = InstanceLock(settings.database_path.parent / LOCK_FILE_NAME)
    try:
        lock.acquire()
    except AlreadyRunningError as exc:
        logger.error(
            "已经有一个机器人进程在跑（%s 里记的是 pid %s），本次启动中止。\n"
            "同一个 token 上跑两个进程会让交互时好时坏：Discord 只把交互发给其中一个会话，"
            "另一个（以及正在重连的那个）不会回应任何东西，客户端只会显示「该交互失败」，"
            "而且两边都不留日志。请先停掉那个进程再启动。",
            exc.path,
            exc.holder_pid if exc.holder_pid is not None else "未知",
        )
        return EXIT_ALREADY_RUNNING

    logger.info("已取得单实例锁：%s（pid=%s）", lock.path, os.getpid())

    try:
        try:
            return run_bot(settings, logger=logger, bot_factory=SyncaindBot)
        except discord.LoginFailure:
            logger.error("DISCORD_TOKEN 无效或已被重置，请到 Developer Portal 重新获取。")
            return EXIT_LOGIN_FAILURE
        except discord.PrivilegedIntentsRequired:
            logger.error(
                "缺少特权 intent：请在 Developer Portal → Bot → Privileged Gateway Intents "
                "里开启 SERVER MEMBERS INTENT。"
            )
            return EXIT_PRIVILEGED_INTENTS
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())

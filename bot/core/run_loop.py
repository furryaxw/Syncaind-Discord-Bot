"""启动循环：连接类失败按退避重试，而不是让进程直接死掉。

为什么需要它：``discord.Client.run()`` 的 ``reconnect`` 参数只管**已经建立**的会话；
**首次**连接就失败时它会把异常抛出来，于是网络抖一下进程就没了。
systemd 能靠 ``Restart=on-failure`` 兜住，手动在终端里跑就只能靠人发现——
而「机器人半夜连不上网就再也没起来」是最不值得的故障。

策略：指数退避（1s → 2s → 4s …），上限 5 分钟，**一直重试**。
只有「重试没有意义」的错误才直接放弃：token 无效、缺特权 intent。
"""

from __future__ import annotations

import asyncio
import logging
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import discord

EXIT_OK = 0
EXIT_GAVE_UP = 1

INITIAL_RETRY_SECONDS = 1.0
MAX_RETRY_SECONDS = 300.0

# 这两类重试没有意义，交给调用方给专门提示。
FATAL_EXCEPTIONS: tuple[type[BaseException], ...] = (
    discord.LoginFailure,
    discord.PrivilegedIntentsRequired,
)


@dataclass(frozen=True)
class RetryPolicy:
    initial_seconds: float = INITIAL_RETRY_SECONDS
    maximum_seconds: float = MAX_RETRY_SECONDS
    # None = 一直重试。「网络会自己好」比「进程静静地死掉」值得赌。
    max_attempts: int | None = None

    def delay_for(self, attempt: int) -> float:
        """第 ``attempt`` 次失败后要等多久（attempt 从 1 开始）。"""
        if attempt < 1:
            raise ValueError("attempt 从 1 开始")
        return min(self.initial_seconds * (2 ** (attempt - 1)), self.maximum_seconds)

    def should_retry(self, attempt: int) -> bool:
        return self.max_attempts is None or attempt < self.max_attempts


def run_bot(
    settings: Any,
    *,
    logger: logging.Logger,
    bot_factory: Callable[..., Any],
    policy: RetryPolicy | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """反复启动机器人，直到它正常退出或重试到上限。

    返回进程退出码。``LoginFailure`` / ``PrivilegedIntentsRequired`` 会向上抛，
    因为它们需要的是「改配置」而不是「再试一次」。
    """
    resolved = policy or RetryPolicy()
    attempt = 0

    while True:
        attempt += 1
        bot = bot_factory(settings=settings, logger=logger)
        try:
            # log_handler=None：日志已经由 setup_logging 配好，不要再让 discord.py 装一个。
            bot.run(settings.discord_token, log_handler=None)
        except KeyboardInterrupt:
            logger.info("收到中断信号，已退出。")
            return EXIT_OK
        except FATAL_EXCEPTIONS:
            _close_quietly(bot)
            raise
        except Exception as exc:
            detail = traceback.format_exc()
            _close_quietly(bot)
            if not resolved.should_retry(attempt):
                logger.error("启动失败：已重试 %d 次仍连不上，放弃。\n%s", attempt - 1, detail)
                return EXIT_GAVE_UP
            delay = resolved.delay_for(attempt)
            if attempt == 1:
                # 第一次给完整堆栈：这是唯一一次能看清「到底哪儿断了」的机会。
                logger.error("连接 Discord 失败，%.0f 秒后重试：\n%s", delay, detail)
            else:
                logger.warning(
                    "第 %d 次连接失败（%s: %s），%.0f 秒后重试",
                    attempt,
                    type(exc).__name__,
                    exc,
                    delay,
                )
            try:
                sleep(delay)
            except KeyboardInterrupt:
                logger.info("重试等待期间收到中断信号，已退出。")
                return EXIT_OK
            continue
        return EXIT_OK


def _close_quietly(bot: Any) -> None:
    """尽量把失败那次尝试占用的资源收回来（数据库连接、aiohttp 会话）。

    关不掉也无所谓——真正重要的是别让每次重试都漏一个连接。
    """
    close = getattr(bot, "close", None)
    if close is None:
        return
    try:
        asyncio.run(close())
    except Exception:
        pass

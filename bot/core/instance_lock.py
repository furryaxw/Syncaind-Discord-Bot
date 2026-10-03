"""单实例守卫：同一个 token 上只允许跑一个机器人进程。

为什么需要它：同一个 token 上跑两个进程时，Discord 只把交互发给其中一个会话；被抢走会话的那个
（以及正在重连的那个）不回应任何东西——客户端只显示「该交互失败」，而且**两边都不留下日志**。
这种故障排查起来极费时间，所以启动时直接拦住，给出一次就能读懂的话。

为什么用 **OS 级排他锁**而不是「写个 pid 文件自己判断有没有进程在跑」：
进程无论怎么死（崩溃、被 kill、掉电），锁都会被内核释放，不会留下需要人工清理的僵尸锁；
而「判断 pid 还活着」在 Windows 上没有便宜又安全的做法（``os.kill(pid, 0)`` 在 Windows 上
会真的把进程杀掉）。

为什么是**两个文件**：``bot.lock`` 是被锁住的那个（内容无关紧要），``bot.pid`` 是给人和日志看的。
Windows 的字节范围锁是强制性的，被锁住的字节连读都不允许，而且 CPython 的缓冲读一次要 8KB、
必然跨过锁区——所以「把 pid 写在被锁的文件里」在 Windows 上读不出来。分成两个文件之后，
锁与可读的 pid 各自成立，两个平台行为一致。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO

LOCK_FILE_NAME = "bot.lock"
PID_FILE_NAME = "bot.pid"


class AlreadyRunningError(RuntimeError):
    """另一个进程正持有这个锁。"""

    def __init__(self, path: Path, holder_pid: int | None = None) -> None:
        super().__init__(f"锁文件 {path} 已被占用")
        self.path = path
        self.holder_pid = holder_pid


def read_holder_pid(pid_path: Path) -> int | None:
    """读 pid 文件，只为把报错信息说得更有用。读不到或不是数字就返回 ``None``。"""
    try:
        text = pid_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


class InstanceLock:
    """基于文件排他锁的单实例守卫。

    故意**不删锁文件**：在别人已经打开文件、正要加锁的瞬间删掉它，会让两个进程各自锁住
    不同的 inode 而同时运行。pid 文件则无所谓，它不承担排他职责，释放时顺手删掉。
    """

    def __init__(self, path: Path | str, *, pid_path: Path | str | None = None) -> None:
        self._path = Path(path)
        self._pid_path = Path(pid_path) if pid_path is not None else self._path.with_suffix(".pid")
        self._handle: IO[str] | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def pid_path(self) -> Path:
        return self._pid_path

    @property
    def is_held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> None:
        """取得锁。已被别人持有时抛 :class:`AlreadyRunningError`。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+", encoding="utf-8")
        try:
            handle.seek(0)
            _lock(handle)
        except OSError as exc:
            holder = read_holder_pid(self._pid_path)
            handle.close()
            raise AlreadyRunningError(self._path, holder) from exc

        self._pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            _unlock(self._handle)
        except OSError:
            # 解锁失败不致命：进程退出时内核也会释放。
            pass
        self._handle.close()
        self._handle = None
        try:
            self._pid_path.unlink(missing_ok=True)
        except OSError:
            pass


if os.name == "nt":
    import msvcrt

    def _lock(handle: IO[str]) -> None:
        # Windows 的字节范围锁归文件句柄所有：同进程的另一个句柄也会冲突。
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle: IO[str]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(handle: IO[str]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle: IO[str]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

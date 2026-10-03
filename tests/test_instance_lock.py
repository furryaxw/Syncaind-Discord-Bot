"""单实例守卫：同一个 token 上只允许跑一个进程。

同一个 token 上出现两个会话时，交互会被两边抢，客户端显示「该交互失败」，
而两边都不留日志 —— 必须在启动时就挡住第二个进程。
"""

from __future__ import annotations

import os

import pytest

from bot.core.instance_lock import (
    LOCK_FILE_NAME,
    PID_FILE_NAME,
    AlreadyRunningError,
    InstanceLock,
    read_holder_pid,
)


def make_lock(tmp_path) -> InstanceLock:
    return InstanceLock(tmp_path / "data" / LOCK_FILE_NAME)


def test_acquire_creates_the_lock_file_and_records_the_pid(tmp_path) -> None:
    lock = make_lock(tmp_path)
    try:
        lock.acquire()

        assert lock.is_held
        assert lock.path.exists()
        # pid 写在另一个文件里：被锁住的那个文件在 Windows 上是读不得的。
        assert read_holder_pid(lock.pid_path) == os.getpid()
    finally:
        lock.release()


def test_second_instance_is_refused(tmp_path) -> None:
    path = tmp_path / LOCK_FILE_NAME
    first = InstanceLock(path)
    second = InstanceLock(path)
    try:
        first.acquire()

        with pytest.raises(AlreadyRunningError) as excinfo:
            second.acquire()

        assert excinfo.value.path == path
        assert excinfo.value.holder_pid == os.getpid()
        assert second.is_held is False
    finally:
        first.release()
        second.release()


def test_release_allows_starting_again(tmp_path) -> None:
    first = make_lock(tmp_path)
    first.acquire()
    first.release()

    second = make_lock(tmp_path)
    try:
        second.acquire()
        assert second.is_held
    finally:
        second.release()


def test_the_lock_file_is_left_behind_on_purpose(tmp_path) -> None:
    """故意不删锁文件：在别人正要加锁的瞬间删掉它，会让两个进程各锁不同的 inode 而同时运行。"""
    lock = make_lock(tmp_path)
    lock.acquire()
    lock.release()

    assert lock.path.exists()


def test_the_pid_file_is_cleaned_up(tmp_path) -> None:
    lock = make_lock(tmp_path)
    lock.acquire()
    assert lock.pid_path.exists()

    lock.release()

    assert not lock.pid_path.exists()


def test_release_is_idempotent(tmp_path) -> None:
    lock = make_lock(tmp_path)
    lock.acquire()
    lock.release()
    lock.release()

    assert lock.is_held is False


def test_pid_is_rewritten_by_the_next_holder(tmp_path) -> None:
    path = tmp_path / LOCK_FILE_NAME
    first = InstanceLock(path, pid_path=tmp_path / "custom.pid")
    first.acquire()
    first.release()

    second = InstanceLock(path, pid_path=tmp_path / "custom.pid")
    second.acquire()
    try:
        assert read_holder_pid(tmp_path / "custom.pid") == os.getpid()
    finally:
        second.release()


# ---------------------------------------------------------------- pid 读取


def test_holder_pid_reads_digits(tmp_path) -> None:
    path = tmp_path / "x.pid"
    path.write_text("12345\n", encoding="utf-8")

    assert read_holder_pid(path) == 12345


def test_holder_pid_is_none_for_a_garbage_file(tmp_path) -> None:
    path = tmp_path / "x.pid"
    path.write_text("garbage", encoding="utf-8")

    assert read_holder_pid(path) is None


def test_holder_pid_is_none_for_a_missing_file(tmp_path) -> None:
    assert read_holder_pid(tmp_path / "nope.pid") is None


def test_default_pid_path_sits_next_to_the_lock(tmp_path) -> None:
    lock = InstanceLock(tmp_path / LOCK_FILE_NAME)

    assert lock.pid_path.name == PID_FILE_NAME
    assert lock.pid_path.parent == lock.path.parent

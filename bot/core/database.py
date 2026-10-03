"""数据层：单连接 aiosqlite + 串行写队列 + 手写 schema 迁移。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from .clock import utcnow_iso

DEFAULT_MIGRATIONS_DIR = Path(__file__).with_name("migrations")

_SCHEMA_VERSION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL,
    applied_at TEXT    NOT NULL
)
"""


class Database:
    """SQLite 封装。

    单连接 + 一把 ``asyncio.Lock``：所有读写都串行执行，规避 ``database is locked``。
    本项目是单进程单服务器，这个模型足够，也省掉了连接池的复杂度。
    """

    def __init__(
        self,
        path: Path | str,
        migrations_dir: Path | str | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._path = Path(path)
        self._migrations_dir = Path(migrations_dir) if migrations_dir else DEFAULT_MIGRATIONS_DIR
        self._logger = logger or logging.getLogger("bot.db")
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def is_connected(self) -> bool:
        return self._connection is not None

    async def connect(self) -> None:
        if self._connection is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self._path)
        self._connection.row_factory = aiosqlite.Row
        await self._connection.execute("PRAGMA journal_mode=WAL")
        await self._connection.execute("PRAGMA foreign_keys=ON")
        await self._connection.commit()

    async def close(self) -> None:
        if self._connection is None:
            return
        async with self._lock:
            await self._connection.close()
            self._connection = None

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """执行一条写语句并提交，返回受影响行数。"""
        connection = self._require_connection()
        async with self._lock:
            cursor = await connection.execute(sql, tuple(params))
            await connection.commit()
            return cursor.rowcount

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """把多条语句放进同一个事务。

        注意：事务持有写锁，期间**不能**再调用本类的 ``execute`` / ``fetchone`` /
        ``fetchall``（锁不可重入）；请直接用 ``yield`` 出来的连接。
        """
        connection = self._require_connection()
        async with self._lock:
            try:
                yield connection
            except BaseException:
                await connection.rollback()
                raise
            await connection.commit()

    async def executemany(self, sql: str, params: Iterable[Sequence[Any]]) -> None:
        connection = self._require_connection()
        async with self._lock:
            await connection.executemany(sql, [tuple(row) for row in params])
            await connection.commit()

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
        connection = self._require_connection()
        async with self._lock:
            cursor = await connection.execute(sql, tuple(params))
            return await cursor.fetchone()

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        connection = self._require_connection()
        async with self._lock:
            cursor = await connection.execute(sql, tuple(params))
            return list(await cursor.fetchall())

    async def migrate(self) -> tuple[int, ...]:
        """按文件名前缀顺序执行未应用的迁移，返回本次实际应用的版本号。"""
        await self.execute(_SCHEMA_VERSION_TABLE)
        rows = await self.fetchall("SELECT version FROM schema_version")
        applied = {int(row["version"]) for row in rows}

        newly_applied: list[int] = []
        for script_path in sorted(self._migrations_dir.glob("*.sql")):
            version = _parse_version(script_path.name)
            if version in applied:
                continue
            script = script_path.read_text(encoding="utf-8")
            await self._apply_migration(version, script_path.stem, script)
            newly_applied.append(version)
            self._logger.info("已应用数据库迁移 %s", script_path.name)
        return tuple(newly_applied)

    async def _apply_migration(self, version: int, name: str, script: str) -> None:
        connection = self._require_connection()
        async with self._lock:
            await connection.executescript(script)
            await connection.execute(
                "INSERT INTO schema_version (version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, utcnow_iso()),
            )
            await connection.commit()

    def _require_connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("数据库尚未连接，请先 await db.connect()")
        return self._connection


def _parse_version(filename: str) -> int:
    prefix = filename.split("_", 1)[0]
    try:
        return int(prefix)
    except ValueError as exc:
        raise ValueError(f"迁移文件名必须以数字版本号开头：{filename}") from exc

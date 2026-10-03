"""GitHub ↔ Discord 账号映射的存储。

**只存身份，不存 token。** 用户的 access token 不落库：映射表只需要「谁是谁」，
而检查组织成员身份用的是机器人自己的凭据。少存一份凭据，就少一处泄露面。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from bot.core.clock import utcnow_iso
from bot.core.database import Database


@dataclass(frozen=True)
class GitHubAccount:
    guild_id: int
    discord_user_id: int
    github_user_id: int
    github_login: str
    linked_at: str


class GitHubAccountConflict(RuntimeError):
    """这个 GitHub 账号已经绑在另一个 Discord 用户身上。"""

    def __init__(self, github_login: str, holder_discord_user_id: int) -> None:
        super().__init__(f"{github_login} 已绑定到 {holder_discord_user_id}")
        self.github_login = github_login
        self.holder_discord_user_id = holder_discord_user_id


class GitHubAccountStore:
    def __init__(self, db: Database, *, logger: logging.Logger | None = None) -> None:
        self._db = db
        self._logger = logger or logging.getLogger("bot.github")

    async def link(
        self,
        guild_id: int,
        *,
        discord_user_id: int,
        github_user_id: int,
        github_login: str,
    ) -> GitHubAccount:
        """建立或更新绑定。同一个 GitHub 账号已被别人占用时抛 :class:`GitHubAccountConflict`。"""
        async with self._db.transaction() as connection:
            cursor = await connection.execute(
                """
                SELECT discord_user_id FROM github_accounts
                WHERE guild_id = ? AND github_user_id = ?
                """,
                (guild_id, github_user_id),
            )
            row = await cursor.fetchone()
            if row is not None and int(row["discord_user_id"]) != discord_user_id:
                raise GitHubAccountConflict(github_login, int(row["discord_user_id"]))

            await connection.execute(
                """
                INSERT INTO github_accounts (
                    guild_id, discord_user_id, github_user_id, github_login, linked_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (guild_id, discord_user_id) DO UPDATE SET
                    github_user_id = excluded.github_user_id,
                    github_login = excluded.github_login,
                    linked_at = excluded.linked_at
                """,
                (guild_id, discord_user_id, github_user_id, github_login, utcnow_iso()),
            )

        self._logger.info("绑定 GitHub 账号：discord=%s github=%s", discord_user_id, github_login)
        account = await self.get(guild_id, discord_user_id)
        assert account is not None  # 事务刚写进去的
        return account

    async def unlink(self, guild_id: int, discord_user_id: int) -> int:
        """解除绑定，返回删除的行数（0 表示本来就没绑）。"""
        return await self._db.execute(
            "DELETE FROM github_accounts WHERE guild_id = ? AND discord_user_id = ?",
            (guild_id, discord_user_id),
        )

    async def get(self, guild_id: int, discord_user_id: int) -> GitHubAccount | None:
        row = await self._db.fetchone(
            "SELECT * FROM github_accounts WHERE guild_id = ? AND discord_user_id = ?",
            (guild_id, discord_user_id),
        )
        return _to_account(row) if row is not None else None

    async def by_github_login(self, guild_id: int, login: str) -> GitHubAccount | None:
        """按 GitHub 用户名（大小写不敏感）反查——身份联动要先知道「这个 GitHub 用户是谁」。"""
        row = await self._db.fetchone(
            """
            SELECT * FROM github_accounts
            WHERE guild_id = ? AND github_login = ? COLLATE NOCASE
            """,
            (guild_id, login),
        )
        return _to_account(row) if row is not None else None

    async def all(self, guild_id: int) -> list[GitHubAccount]:
        rows = await self._db.fetchall(
            "SELECT * FROM github_accounts WHERE guild_id = ? ORDER BY linked_at",
            (guild_id,),
        )
        return [_to_account(row) for row in rows]


def _to_account(row) -> GitHubAccount:
    return GitHubAccount(
        guild_id=int(row["guild_id"]),
        discord_user_id=int(row["discord_user_id"]),
        github_user_id=int(row["github_user_id"]),
        github_login=str(row["github_login"]),
        linked_at=str(row["linked_at"]),
    )

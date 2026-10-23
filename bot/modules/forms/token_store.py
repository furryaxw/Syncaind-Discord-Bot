"""表单 token 的 jti 使用记录：成功提交后作废，防止重放。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from bot.core.clock import utcnow_iso
from bot.core.database import Database


class FormTokenStore:
    def __init__(self, db: Database, *, logger: logging.Logger | None = None) -> None:
        self._db = db
        self._logger = logger or logging.getLogger("bot.forms")

    async def remember(self, claims: dict[str, Any], *, kind: str) -> None:
        expires = datetime.fromtimestamp(int(claims["exp"]), timezone.utc).isoformat(timespec="seconds")
        await self._db.execute(
            """
            INSERT OR IGNORE INTO form_tokens
                (jti, kind, guild_id, form_id, publication_id, user_id, subject_id,
                 expires_at, used_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                str(claims["jti"]),
                kind,
                int(claims["gid"]),
                claims.get("fid"),
                claims.get("pid"),
                int(claims["sub"]),
                claims.get("sid"),
                expires,
                utcnow_iso(),
            ),
        )

    async def consume(self, jti: str) -> bool:
        """成功提交时调用；已被用过返回 ``False``。"""
        changed = await self._db.execute(
            "UPDATE form_tokens SET used_at = ? WHERE jti = ? AND used_at IS NULL",
            (utcnow_iso(), jti),
        )
        return bool(changed)

    async def is_used(self, jti: str) -> bool:
        row = await self._db.fetchone("SELECT used_at FROM form_tokens WHERE jti = ?", (jti,))
        return row is not None and row["used_at"] is not None

    async def cleanup(self, *, before_iso: str) -> int:
        return await self._db.execute(
            "DELETE FROM form_tokens WHERE expires_at < ?",
            (before_iso,),
        )

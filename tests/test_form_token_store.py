"""表单 token 的 jti 记录：成功提交后作废。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bot.modules.forms.token_store import FormTokenStore


def claims(jti: str, *, seconds: int = 60) -> dict:
    return {
        "jti": jti,
        "gid": 100,
        "fid": "team",
        "pid": 7,
        "sub": 200,
        "exp": int((datetime.now(timezone.utc) + timedelta(seconds=seconds)).timestamp()),
    }


async def test_consume_is_one_time(db) -> None:
    store = FormTokenStore(db)
    await store.remember(claims("a"), kind="form")

    assert await store.is_used("a") is False
    assert await store.consume("a") is True
    assert await store.consume("a") is False
    assert await store.is_used("a") is True


async def test_cleanup_removes_expired(db) -> None:
    store = FormTokenStore(db)
    await store.remember(claims("old", seconds=-10), kind="form")
    await store.remember(claims("new"), kind="form")

    removed = await store.cleanup(before_iso=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    assert removed == 1
    assert await store.is_used("new") is False

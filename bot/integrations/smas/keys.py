"""激活码的取用与退回 —— 包在 :class:`AccessClient` 上的两个动作。

契约（`docs/runtime-communication-contract.md`，2026-10-03 复核）：

* ``take``：``team.<team_id>.keys`` 的 ``take``，体 ``{"batch_id","count","recipient"}``，
  **一个事务**里取走 N 枚「未使用且未发放」的码并记下收件人，返回含明文的 ``{keys, count}``；
  数量不足返回 ``409 key_unavailable``，并发取用不会发出同一枚。
* ``release``：``{"key_id"}``，撤销一枚**尚未兑换**的发放记录 —— 私信失败时的退路。

这两条是「发出去」的唯一入口：**不要自己从 read 里挑码再本地记状态**，
那会丢掉服务端的原子性，并发时可能把同一枚发给两个人。
"""

from __future__ import annotations

from dataclasses import dataclass

from .client import AccessClient, AccessError

KEYS_NODE = "team.{team_id}.keys"
KEY_UNAVAILABLE = "key_unavailable"


class KeyPoolError(RuntimeError):
    """取码/退码失败。``unavailable`` 表示批次里没有可发的了（不是错误，是状态）。"""

    def __init__(self, code: str, message: str = "", *, unavailable: bool = False) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.unavailable = unavailable


@dataclass(frozen=True)
class TakenKey:
    key_id: str
    plaintext: str
    batch_id: str
    key_prefix: str = ""


def recipient_tag(discord_user_id: int) -> str:
    """服务端的 ``recipient`` 是命名空间写法（它也为非 GitHub 身份留了位置）。"""
    return f"discord:{discord_user_id}"


async def take_keys(
    client: AccessClient,
    *,
    team_id: str,
    batch_id: str,
    count: int,
    recipient: int,
    request_id: str | None = None,
) -> list[TakenKey]:
    """从批次里原子取走 ``count`` 枚并记下收件人。取不到就抛 :class:`KeyPoolError`。"""
    if count < 1:
        raise KeyPoolError("invalid_request", "count 必须 >= 1")
    try:
        payload = await client.call(
            "take",
            KEYS_NODE.format(team_id=team_id),
            {"batch_id": batch_id, "count": count, "recipient": recipient_tag(recipient)},
            request_id=request_id,
        )
    except AccessError as exc:
        raise KeyPoolError(
            exc.code,
            exc.message,
            unavailable=exc.code == KEY_UNAVAILABLE,
        ) from exc

    keys: list[TakenKey] = []
    for item in payload.get("keys") or []:
        if not isinstance(item, dict) or not item.get("key_id"):
            continue
        keys.append(
            TakenKey(
                key_id=str(item["key_id"]),
                plaintext=str(item.get("plaintext") or ""),
                batch_id=str(item.get("batch_id") or batch_id),
                key_prefix=str(item.get("key_prefix") or ""),
            )
        )
    return keys


async def release_key(client: AccessClient, *, team_id: str, key_id: str) -> bool:
    """撤销一枚尚未兑换的发放记录（私信发不出去时的退路）。

    返回是否成功；失败不抛 —— 调用方已经在处理「发不出去」这件事了，
    退不回去只该记一条日志，不该再炸一次。
    """
    try:
        await client.call("release", KEYS_NODE.format(team_id=team_id), {"key_id": key_id})
    except AccessError:
        return False
    return True


async def find_batch_team(client: AccessClient, *, batch_id: str, system_team_id: str) -> str | None:
    """批次属于哪个 Team —— 用 System 工作区的跨 Team 视图查一次。

    ``take`` 的节点是 ``team.<team_id>.keys``，所以必须知道 Team；而命令里只给批次号更顺手。

    两种「查不到」要分开：
    * **批次不存在/查不到行** → 返回 ``None``：提示核对批次号或显式传 team；
    * **读不了**（没权限 `team.system.keys.read`、连不上）→ 抛 :class:`KeyPoolError`：
      该做的是**去加权限**，而不是让人反复核对批次号。
    """
    try:
        payload = await client.call(
            "read",
            KEYS_NODE.format(team_id=system_team_id),
            {"batch_id": batch_id, "limit": 1},
            team_id=system_team_id,
        )
    except AccessError as exc:
        raise KeyPoolError(exc.code, exc.message) from exc
    for item in payload.get("keys") or []:
        if isinstance(item, dict) and item.get("team_id"):
            return str(item["team_id"])
    return None


def first_plaintext(keys: list[TakenKey]) -> str:
    """只有一枚时取它的明文（调用方已经保证 count=1）。"""
    return keys[0].plaintext if keys else ""


__all__ = [
    "KEYS_NODE",
    "KeyPoolError",
    "TakenKey",
    "find_batch_team",
    "first_plaintext",
    "recipient_tag",
    "release_key",
    "take_keys",
]

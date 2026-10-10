"""表单的运行时状态：绑定、发布的卡片、分步草稿、正式提交。

答案一律以 JSON 存在一个 TEXT 列里（读整行比拆表简单，也省掉一批 join）：
文本/单选/多选直接是字符串或字符串列表，附件是 ``{"name", "stored", "size", "type"}`` 的列表
—— ``stored`` 是附件目录里的文件名，目录由所在行的 ``storage_key`` / ``attachment_dir`` 决定。
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from typing import Any

from bot.core.clock import utcnow_iso
from bot.core.database import Database

# 发布状态
OPEN = "open"
CLOSED = "closed"

# 提交状态。申请类会走 pending →（approved | rejected | needs_info）；needs_info 之后申请人补充，
# 回到 pending。收集与报名类提交即 received。
PENDING = "pending"
NEEDS_INFO = "needs_info"
APPROVED = "approved"
REJECTED = "rejected"
RECEIVED = "received"


def new_storage_key() -> str:
    """附件目录名。用随机值而不是自增 id，提交前就能定下目录，省掉「先插入再改名」。"""
    return secrets.token_hex(16)


def _dump(answers: dict[str, Any]) -> str:
    return json.dumps(answers, ensure_ascii=False)


def _load(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(str(raw))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class FormBinding:
    guild_id: int
    form_id: str
    review_channel_id: int | None = None
    reviewer_role_id: int | None = None
    notify_channel_id: int | None = None
    grant_role_id: int | None = None
    updated_at: str = ""

    @property
    def is_configured(self) -> bool:
        return any(
            item is not None
            for item in (self.review_channel_id, self.reviewer_role_id, self.notify_channel_id, self.grant_role_id)
        )


@dataclass(frozen=True)
class FormPublication:
    publication_id: int
    guild_id: int
    form_id: str
    channel_id: int
    message_id: int | None
    status: str
    capacity: int | None
    taken: int
    closes_at: str | None
    created_by: int
    created_at: str

    @property
    def is_open(self) -> bool:
        return self.status == OPEN

    @property
    def is_full(self) -> bool:
        return self.capacity is not None and self.taken >= self.capacity

    @property
    def remaining(self) -> int | None:
        return None if self.capacity is None else max(self.capacity - self.taken, 0)


@dataclass(frozen=True)
class FormDraft:
    guild_id: int
    form_id: str
    discord_user_id: int
    step: int
    answers: dict[str, Any]
    storage_key: str
    updated_at: str


@dataclass(frozen=True)
class FormSubmission:
    submission_id: int
    guild_id: int
    form_id: str
    publication_id: int | None
    discord_user_id: int
    answers: dict[str, Any]
    status: str
    submitted_at: str
    reviewed_by: int | None
    reviewed_at: str | None
    decision_reason: str | None
    attachment_dir: str
    review_channel_id: int | None
    review_message_id: int | None

    @property
    def is_pending(self) -> bool:
        return self.status in (PENDING, NEEDS_INFO)


class FormBindingStore:
    """每个表单在服务器上的绑定。没有行 = 全都没绑，不写库。"""

    BINDABLE = ("review_channel_id", "reviewer_role_id", "notify_channel_id", "grant_role_id")

    def __init__(self, db: Database, *, logger: logging.Logger | None = None) -> None:
        self._db = db
        self._logger = logger or logging.getLogger("bot.forms")

    async def get(self, guild_id: int, form_id: str) -> FormBinding:
        row = await self._db.fetchone(
            "SELECT * FROM form_bindings WHERE guild_id = ? AND form_id = ?",
            (guild_id, form_id),
        )
        if row is None:
            return FormBinding(guild_id=guild_id, form_id=form_id)
        return FormBinding(
            guild_id=int(row["guild_id"]),
            form_id=str(row["form_id"]),
            review_channel_id=_optional_int(row["review_channel_id"]),
            reviewer_role_id=_optional_int(row["reviewer_role_id"]),
            notify_channel_id=_optional_int(row["notify_channel_id"]),
            grant_role_id=_optional_int(row["grant_role_id"]),
            updated_at=str(row["updated_at"]),
        )

    async def update(self, guild_id: int, form_id: str, **changes: Any) -> FormBinding:
        """只改传进来的项；传 ``None`` 表示解绑该项。"""
        unknown = set(changes) - set(self.BINDABLE)
        if unknown:
            raise ValueError(f"不支持的绑定项：{sorted(unknown)}")
        current = await self.get(guild_id, form_id)
        merged = {
            "review_channel_id": current.review_channel_id,
            "reviewer_role_id": current.reviewer_role_id,
            "notify_channel_id": current.notify_channel_id,
            "grant_role_id": current.grant_role_id,
        }
        merged.update(changes)
        await self._db.execute(
            """
            INSERT INTO form_bindings (
                guild_id, form_id, review_channel_id, reviewer_role_id,
                notify_channel_id, grant_role_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (guild_id, form_id) DO UPDATE SET
                review_channel_id = excluded.review_channel_id,
                reviewer_role_id = excluded.reviewer_role_id,
                notify_channel_id = excluded.notify_channel_id,
                grant_role_id = excluded.grant_role_id,
                updated_at = excluded.updated_at
            """,
            (
                guild_id,
                form_id,
                merged["review_channel_id"],
                merged["reviewer_role_id"],
                merged["notify_channel_id"],
                merged["grant_role_id"],
                utcnow_iso(),
            ),
        )
        self._logger.info("表单绑定已更新：form=%s %s", form_id, sorted(changes))
        return await self.get(guild_id, form_id)


class FormPublicationStore:
    """频道里那张卡片。按钮按 message_id 反查这里，所以重启后照样能点。"""

    def __init__(self, db: Database, *, logger: logging.Logger | None = None) -> None:
        self._db = db
        self._logger = logger or logging.getLogger("bot.forms")

    async def create(
        self,
        guild_id: int,
        *,
        form_id: str,
        channel_id: int,
        capacity: int | None,
        closes_at: str | None,
        created_by: int,
    ) -> FormPublication:
        await self._db.execute(
            """
            INSERT INTO form_publications
                (guild_id, form_id, channel_id, status, capacity, closes_at, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (guild_id, form_id, channel_id, OPEN, capacity, closes_at, created_by, utcnow_iso()),
        )
        row = await self._db.fetchone(
            """
            SELECT * FROM form_publications
             WHERE guild_id = ? AND form_id = ? AND channel_id = ?
             ORDER BY publication_id DESC LIMIT 1
            """,
            (guild_id, form_id, channel_id),
        )
        assert row is not None
        publication = _to_publication(row)
        self._logger.info(
            "发布了一张表单卡片：form=%s channel=%s publication=%s",
            form_id,
            channel_id,
            publication.publication_id,
        )
        return publication

    async def get(self, publication_id: int) -> FormPublication | None:
        row = await self._db.fetchone(
            "SELECT * FROM form_publications WHERE publication_id = ?",
            (publication_id,),
        )
        return _to_publication(row) if row is not None else None

    async def get_by_message(self, message_id: int) -> FormPublication | None:
        row = await self._db.fetchone(
            "SELECT * FROM form_publications WHERE message_id = ?",
            (message_id,),
        )
        return _to_publication(row) if row is not None else None

    async def set_message(self, publication_id: int, message_id: int) -> None:
        await self._db.execute(
            "UPDATE form_publications SET message_id = ? WHERE publication_id = ?",
            (message_id, publication_id),
        )

    async def set_status(self, publication_id: int, status: str) -> None:
        await self._db.execute(
            "UPDATE form_publications SET status = ? WHERE publication_id = ?",
            (status, publication_id),
        )

    async def open_in_guild(self, guild_id: int, form_id: str | None = None) -> list[FormPublication]:
        if form_id is None:
            rows = await self._db.fetchall(
                "SELECT * FROM form_publications WHERE guild_id = ? AND status = ? ORDER BY publication_id",
                (guild_id, OPEN),
            )
        else:
            rows = await self._db.fetchall(
                """
                SELECT * FROM form_publications
                 WHERE guild_id = ? AND form_id = ? AND status = ?
                 ORDER BY publication_id
                """,
                (guild_id, form_id, OPEN),
            )
        return [_to_publication(row) for row in rows]

    async def due(self, now_iso: str) -> list[FormPublication]:
        """到点还没关的发布（后台任务据此自动关闭，跨重启不会漏）。"""
        rows = await self._db.fetchall(
            """
            SELECT * FROM form_publications
             WHERE status = ? AND closes_at IS NOT NULL AND closes_at <= ?
             ORDER BY closes_at
            """,
            (OPEN, now_iso),
        )
        return [_to_publication(row) for row in rows]

    async def take_slot(self, publication_id: int) -> bool:
        """报名名额：**一条 SQL 里自增并封顶**，所以并发提交不会超员。

        返回 ``False`` 表示已经满了（或发布已关闭）。
        """
        cursor = await self._db.execute(
            """
            UPDATE form_publications SET taken = taken + 1
             WHERE publication_id = ? AND status = ? AND (capacity IS NULL OR taken < capacity)
            """,
            (publication_id, OPEN),
        )
        return bool(cursor)

    async def give_back_slot(self, publication_id: int) -> None:
        """占了名额但提交没落库时还回去 —— 否则会出现「一个人都没报上、名额却满了」。"""
        await self._db.execute(
            "UPDATE form_publications SET taken = MAX(taken - 1, 0) WHERE publication_id = ?",
            (publication_id,),
        )


class FormDraftStore:
    """分步草稿：每提交一步写一次。"""

    def __init__(self, db: Database, *, logger: logging.Logger | None = None) -> None:
        self._db = db
        self._logger = logger or logging.getLogger("bot.forms")

    async def get(self, guild_id: int, form_id: str, discord_user_id: int) -> FormDraft | None:
        row = await self._db.fetchone(
            "SELECT * FROM form_drafts WHERE guild_id = ? AND form_id = ? AND discord_user_id = ?",
            (guild_id, form_id, discord_user_id),
        )
        return _to_draft(row) if row is not None else None

    async def save(
        self,
        guild_id: int,
        *,
        form_id: str,
        discord_user_id: int,
        step: int,
        answers: dict[str, Any],
        storage_key: str | None = None,
    ) -> FormDraft:
        current = await self.get(guild_id, form_id, discord_user_id)
        key = storage_key or (current.storage_key if current is not None else new_storage_key())
        await self._db.execute(
            """
            INSERT INTO form_drafts (guild_id, form_id, discord_user_id, step, answers, storage_key, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (guild_id, form_id, discord_user_id) DO UPDATE SET
                step = excluded.step,
                answers = excluded.answers,
                storage_key = excluded.storage_key,
                updated_at = excluded.updated_at
            """,
            (guild_id, form_id, discord_user_id, step, _dump(answers), key, utcnow_iso()),
        )
        draft = await self.get(guild_id, form_id, discord_user_id)
        assert draft is not None
        return draft

    async def delete(self, guild_id: int, form_id: str, discord_user_id: int) -> None:
        await self._db.execute(
            "DELETE FROM form_drafts WHERE guild_id = ? AND form_id = ? AND discord_user_id = ?",
            (guild_id, form_id, discord_user_id),
        )

    async def stale(self, before_iso: str) -> list[FormDraft]:
        """太久没动的草稿（后台任务据此清掉它和它已经下下来的附件）。"""
        rows = await self._db.fetchall(
            "SELECT * FROM form_drafts WHERE updated_at < ?",
            (before_iso,),
        )
        return [_to_draft(row) for row in rows]


class FormSubmissionStore:
    def __init__(self, db: Database, *, logger: logging.Logger | None = None) -> None:
        self._db = db
        self._logger = logger or logging.getLogger("bot.forms")

    async def create(
        self,
        guild_id: int,
        *,
        form_id: str,
        publication_id: int | None,
        discord_user_id: int,
        answers: dict[str, Any],
        status: str,
        attachment_dir: str,
    ) -> FormSubmission:
        await self._db.execute(
            """
            INSERT INTO form_submissions
                (guild_id, form_id, publication_id, discord_user_id, answers, status,
                 submitted_at, attachment_dir)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                form_id,
                publication_id,
                discord_user_id,
                _dump(answers),
                status,
                utcnow_iso(),
                attachment_dir,
            ),
        )
        submission = await self.get_by_dir(attachment_dir)
        assert submission is not None
        self._logger.info(
            "收到一份表单：form=%s submission=%s status=%s",
            form_id,
            submission.submission_id,
            status,
        )
        return submission

    async def get(self, submission_id: int) -> FormSubmission | None:
        row = await self._db.fetchone(
            "SELECT * FROM form_submissions WHERE submission_id = ?",
            (submission_id,),
        )
        return _to_submission(row) if row is not None else None

    async def get_by_dir(self, attachment_dir: str) -> FormSubmission | None:
        row = await self._db.fetchone(
            "SELECT * FROM form_submissions WHERE attachment_dir = ?",
            (attachment_dir,),
        )
        return _to_submission(row) if row is not None else None

    async def get_by_review_message(self, message_id: int) -> FormSubmission | None:
        """审核按钮就走这条路 —— 状态不存在 view 里。"""
        row = await self._db.fetchone(
            "SELECT * FROM form_submissions WHERE review_message_id = ?",
            (message_id,),
        )
        return _to_submission(row) if row is not None else None

    async def latest_for_user(self, guild_id: int, form_id: str, discord_user_id: int) -> FormSubmission | None:
        row = await self._db.fetchone(
            """
            SELECT * FROM form_submissions
             WHERE guild_id = ? AND form_id = ? AND discord_user_id = ?
             ORDER BY submission_id DESC LIMIT 1
            """,
            (guild_id, form_id, discord_user_id),
        )
        return _to_submission(row) if row is not None else None

    async def replace_answers(
        self,
        submission_id: int,
        *,
        answers: dict[str, Any],
        status: str,
    ) -> None:
        """申请人补充材料后回填：内容整份替换，状态回到待审。"""
        await self._db.execute(
            """
            UPDATE form_submissions
               SET answers = ?, status = ?, submitted_at = ?, reviewed_by = NULL,
                   reviewed_at = NULL, decision_reason = NULL
             WHERE submission_id = ?
            """,
            (_dump(answers), status, utcnow_iso(), submission_id),
        )

    async def set_decision(
        self,
        submission_id: int,
        *,
        status: str,
        reviewer_id: int,
        reason: str | None,
    ) -> None:
        await self._db.execute(
            """
            UPDATE form_submissions
               SET status = ?, reviewed_by = ?, reviewed_at = ?, decision_reason = ?
             WHERE submission_id = ?
            """,
            (status, reviewer_id, utcnow_iso(), reason, submission_id),
        )

    async def set_review_message(self, submission_id: int, *, channel_id: int, message_id: int) -> None:
        await self._db.execute(
            """
            UPDATE form_submissions
               SET review_channel_id = ?, review_message_id = ?
             WHERE submission_id = ?
            """,
            (channel_id, message_id, submission_id),
        )

    async def list_for_form(
        self,
        guild_id: int,
        form_id: str,
        *,
        status: str | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> list[FormSubmission]:
        if status is None:
            rows = await self._db.fetchall(
                """
                SELECT * FROM form_submissions
                 WHERE guild_id = ? AND form_id = ?
                 ORDER BY submission_id DESC LIMIT ? OFFSET ?
                """,
                (guild_id, form_id, limit, offset),
            )
        else:
            rows = await self._db.fetchall(
                """
                SELECT * FROM form_submissions
                 WHERE guild_id = ? AND form_id = ? AND status = ?
                 ORDER BY submission_id DESC LIMIT ? OFFSET ?
                """,
                (guild_id, form_id, status, limit, offset),
            )
        return [_to_submission(row) for row in rows]

    async def count_for_form(self, guild_id: int, form_id: str, *, status: str | None = None) -> int:
        if status is None:
            row = await self._db.fetchone(
                "SELECT COUNT(*) AS total FROM form_submissions WHERE guild_id = ? AND form_id = ?",
                (guild_id, form_id),
            )
        else:
            row = await self._db.fetchone(
                """
                SELECT COUNT(*) AS total FROM form_submissions
                 WHERE guild_id = ? AND form_id = ? AND status = ?
                """,
                (guild_id, form_id, status),
            )
        return int(row["total"]) if row is not None else 0


@dataclass(frozen=True)
class FormStores:
    """四处运行时状态。装配时一起建，测试里也一次给全。"""

    bindings: FormBindingStore
    publications: FormPublicationStore
    drafts: FormDraftStore
    submissions: FormSubmissionStore


def build_stores(db: Database, *, logger: logging.Logger | None = None) -> FormStores:
    log = logger or logging.getLogger("bot.forms")
    return FormStores(
        bindings=FormBindingStore(db, logger=log),
        publications=FormPublicationStore(db, logger=log),
        drafts=FormDraftStore(db, logger=log),
        submissions=FormSubmissionStore(db, logger=log),
    )


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _to_publication(row: Any) -> FormPublication:
    raw_capacity = row["capacity"]
    raw_message = row["message_id"]
    raw_closes = row["closes_at"]
    return FormPublication(
        publication_id=int(row["publication_id"]),
        guild_id=int(row["guild_id"]),
        form_id=str(row["form_id"]),
        channel_id=int(row["channel_id"]),
        message_id=int(raw_message) if raw_message is not None else None,
        status=str(row["status"]),
        capacity=int(raw_capacity) if raw_capacity is not None else None,
        taken=int(row["taken"]),
        closes_at=str(raw_closes) if raw_closes is not None else None,
        created_by=int(row["created_by"]),
        created_at=str(row["created_at"]),
    )


def _to_draft(row: Any) -> FormDraft:
    return FormDraft(
        guild_id=int(row["guild_id"]),
        form_id=str(row["form_id"]),
        discord_user_id=int(row["discord_user_id"]),
        step=int(row["step"]),
        answers=_load(row["answers"]),
        storage_key=str(row["storage_key"]),
        updated_at=str(row["updated_at"]),
    )


def _to_submission(row: Any) -> FormSubmission:
    raw_reviewed_by = row["reviewed_by"]
    raw_reviewed_at = row["reviewed_at"]
    raw_reason = row["decision_reason"]
    raw_publication = row["publication_id"]
    return FormSubmission(
        submission_id=int(row["submission_id"]),
        guild_id=int(row["guild_id"]),
        form_id=str(row["form_id"]),
        publication_id=int(raw_publication) if raw_publication is not None else None,
        discord_user_id=int(row["discord_user_id"]),
        answers=_load(row["answers"]),
        status=str(row["status"]),
        submitted_at=str(row["submitted_at"]),
        reviewed_by=int(raw_reviewed_by) if raw_reviewed_by is not None else None,
        reviewed_at=str(raw_reviewed_at) if raw_reviewed_at is not None else None,
        decision_reason=str(raw_reason) if raw_reason is not None else None,
        attachment_dir=str(row["attachment_dir"]),
        review_channel_id=_optional_int(row["review_channel_id"]),
        review_message_id=_optional_int(row["review_message_id"]),
    )

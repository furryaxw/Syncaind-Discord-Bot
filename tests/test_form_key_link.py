"""表单 ↔ key 联动：一次提交发 N 枚、全有或全无、服务缺失时明确报错。"""

from __future__ import annotations

import pytest

from bot.integrations.keypool import COUNT_FOLLOW, MODE_FCFS
from bot.modules.access_keys.cog import AccessKeysCog
from bot.modules.forms.key_link import FormKeyLinker, KeyLinkError
from bot.modules.forms.schema import FIELD_SHORT, Field, FormDefinition
from bot.modules.forms.store import FormBinding, FormSubmission
from tests.conftest import GUILD_ID
from tests.discord_fakes import MODERATOR_ID, build_guild, setup_bot

FORM_ID = "apply"


def definition() -> FormDefinition:
    return FormDefinition(
        id=FORM_ID,
        mode="apply",
        title={"zh-CN": "申请"},
        description={"zh-CN": "说明"},
        fields=(Field(key="name", kind=FIELD_SHORT, label={"zh-CN": "名字"}),),
        grant_key=2,
    )


def submission(user_id: int = MODERATOR_ID) -> FormSubmission:
    return FormSubmission(
        submission_id=1,
        guild_id=GUILD_ID,
        form_id=FORM_ID,
        publication_id=1,
        discord_user_id=user_id,
        answers={},
        status="pending",
        submitted_at="now",
        reviewed_by=None,
        reviewed_at=None,
        decision_reason=None,
        attachment_dir="dir",
        review_channel_id=None,
        review_message_id=None,
    )


async def prepare(settings, *, keys: int = 3):
    bot = await setup_bot(settings)
    guild = build_guild()
    cog = bot.cogs["AccessKeysCog"]
    assert isinstance(cog, AccessKeysCog)
    pool = await cog.store.create_pool(GUILD_ID, name="A", note=None, created_by=MODERATOR_ID)
    await cog.store.add_keys(GUILD_ID, pool.id, tuple(f"K{index}" for index in range(keys)))
    drop = await cog.store.create_drop(
        GUILD_ID,
        pool_ids=[pool.id],
        mode=MODE_FCFS,
        count_mode=COUNT_FOLLOW,
        key_count=None,
        role_id=None,
        deny_role_id=None,
        channel_id=1,
        closes_at=None,
        created_by=MODERATOR_ID,
    )
    linker = FormKeyLinker(bot)
    binding = FormBinding(guild_id=GUILD_ID, form_id=FORM_ID, drop_id=drop.id)
    return bot, guild, cog, linker, binding


async def test_issue_two_keys(settings) -> None:
    bot, guild, cog, linker, binding = await prepare(settings)
    try:
        keys = await linker.issue(guild, definition(), binding, submission())

        assert len(keys) == 2
        member = guild.get_member(MODERATOR_ID)
        assert member is not None
        assert member._dms and "K" in member._dms[-1]
        assert await cog.store.available_count(GUILD_ID, [binding.drop_id]) == 1
        row = await bot.db.fetchone("SELECT COUNT(*) AS total FROM form_key_grants")
        assert row is not None and row["total"] == 2
    finally:
        await bot.db.close()


async def test_insufficient_stock_returns_nothing(settings) -> None:
    bot, guild, _cog, linker, binding = await prepare(settings, keys=1)
    try:
        with pytest.raises(KeyLinkError):
            await linker.issue(guild, definition(), binding, submission())
    finally:
        await bot.db.close()


async def test_missing_keys_service_is_explicit(settings) -> None:
    bot, guild, _cog, linker, binding = await prepare(settings)
    try:
        bot.services.unregister("keys")
        with pytest.raises(KeyLinkError):
            await linker.issue(guild, definition(), binding, submission())
    finally:
        await bot.db.close()


async def test_resume_held_delivers_fifo(settings) -> None:
    bot, guild, _cog, _linker, binding = await prepare(settings)
    try:
        forms = bot.get_cog("FormsCog")
        assert forms is not None
        await forms.store.bindings.update(GUILD_ID, FORM_ID, drop_id=binding.drop_id)
        await forms.store.submissions.create(
            GUILD_ID,
            form_id=FORM_ID,
            publication_id=1,
            discord_user_id=MODERATOR_ID,
            answers={},
            status="on_hold",
            attachment_dir="dir",
        )

        processed, remaining = await forms.resume_held(guild, definition())
        latest = await forms.store.submissions.latest_for_user(GUILD_ID, FORM_ID, MODERATOR_ID)
    finally:
        await bot.db.close()

    assert processed == 1
    assert remaining == 0
    assert latest is not None and latest.status == "approved"

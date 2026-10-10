"""表单流程：发布卡片、分步填写、附件落盘、审核结论、导出与后台任务。

这一层跑的是真实装配加载出来的真实 Cog，只把 Discord 那一侧换成替身；
附件用真 ``Attachment``（见 tests/forms_fakes.py），所以「提交即落盘」这条路径是**真跑**的。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

from bot.core.errors import UserError
from bot.modules.forms import cog as forms_module
from bot.modules.forms.cog import (
    APPROVE_ID,
    NEEDS_INFO_ID,
    OPEN_ID,
    REJECT_ID,
    FormOpenView,
    FormResumeView,
    FormReviewView,
    FormsCog,
)
from bot.modules.forms.store import (
    APPROVED,
    CLOSED,
    NEEDS_INFO,
    PENDING,
    RECEIVED,
    REJECTED,
    new_storage_key,
)
from tests.conftest import GUILD_ID
from tests.discord_fakes import MODERATOR_ID, FakeInteraction, add_message, run_command, setup_bot
from tests.forms_fakes import (
    APPLICANT_ID,
    NOTIFY_CHANNEL_ID,
    REVIEW_CHANNEL_ID,
    REVIEWER_ID,
    build_forms_world,
    make_attachment,
    write_test_forms,
)

APPLICATION = "team_application"

SUMMARY = {
    "work": ["translate"],
    "availability": "每周 10 小时",
    "experience": "翻过两个模组",
    "portfolio": "https://example.invalid/work",
}
CONTACT = {"contact": "@me"}


@pytest.fixture
async def world():
    return await build_forms_world()


@pytest.fixture
async def cog(settings):
    # 表单是数据文件，模块在 setup 时从数据目录读：所以要在装配之前放好。
    write_test_forms(Path(settings.database_path).parent / "forms")
    bot = await setup_bot(settings)
    # 公开卡片与私信都按**服务器语言**走，所以这里定死一种，断言才有意义。
    await bot.guild_settings.update(GUILD_ID, locale="zh-CN")
    forms = bot.get_cog("FormsCog")
    assert isinstance(forms, FormsCog)
    # 后台任务是常驻的；测试里手动收尾，免得留下悬着的任务。
    forms.stop_sweeping()
    yield forms
    await bot.db.close()


async def bind_all(forms, world, *, grant_role=True, reviewer=True, notify=True) -> None:
    await forms.store.bindings.update(
        GUILD_ID,
        APPLICATION,
        review_channel_id=REVIEW_CHANNEL_ID,
        reviewer_role_id=world.review_role.id if reviewer else None,
        notify_channel_id=NOTIFY_CHANNEL_ID if notify else None,
        grant_role_id=world.grant_role.id if grant_role else None,
    )


async def publish(forms, world, *, form=APPLICATION, channel=None, **kwargs):
    interaction = FakeInteraction(user=world.reviewer, guild=world.guild, channel=world.apply_channel)
    await run_command(
        forms.bot,
        "form post",
        interaction,
        form=form,
        channel=channel or world.apply_channel,
        **kwargs,
    )
    publications = await forms.store.publications.open_in_guild(GUILD_ID, form)
    assert publications, "发布之后应该有一条进行中的发布"
    return publications[-1]


async def submit_step(forms, world, publication, *, user, step_index, answers):
    interaction = FakeInteraction(user=user, guild=world.guild, channel=world.apply_channel)
    definition = forms.catalog.get(publication.form_id)
    assert definition is not None
    await forms.submit_step(
        interaction,
        publication=publication,
        definition=definition,
        step_index=step_index,
        answers=answers,
    )
    return interaction


async def approve_ready(forms, world, *, samples=None):
    """发布 + 提交一份申请，返回 (submission, 审核卡片消息)。"""
    await bind_all(forms, world)
    publication = await publish(forms, world)
    first = dict(SUMMARY)
    if samples is not None:
        first["samples"] = samples
    await submit_step(forms, world, publication, user=world.applicant, step_index=0, answers=first)
    await submit_step(forms, world, publication, user=world.applicant, step_index=1, answers=CONTACT)
    submission = await forms.store.submissions.latest_for_user(GUILD_ID, APPLICATION, APPLICANT_ID)
    assert submission is not None and submission.review_message_id is not None
    return submission, world.review_channel._messages[submission.review_message_id]


# ---------------------------------------------------------------- 命令


async def test_post_refuses_an_application_without_a_review_channel(cog, world) -> None:
    interaction = FakeInteraction(user=world.reviewer, guild=world.guild)

    with pytest.raises(UserError) as excinfo:
        await run_command(cog.bot, "form post", interaction, form=APPLICATION, channel=world.apply_channel)

    assert excinfo.value.key == "forms.bind.review_channel_missing"


async def test_post_refuses_a_missing_grant_role(cog, world) -> None:
    await bind_all(cog, world, grant_role=False)
    interaction = FakeInteraction(user=world.reviewer, guild=world.guild)

    with pytest.raises(UserError) as excinfo:
        await run_command(cog.bot, "form post", interaction, form=APPLICATION, channel=world.apply_channel)

    assert excinfo.value.key == "forms.bind.grant_role_missing"


async def test_post_rejects_an_unknown_form(cog, world) -> None:
    interaction = FakeInteraction(user=world.reviewer, guild=world.guild)

    with pytest.raises(UserError) as excinfo:
        await run_command(cog.bot, "form post", interaction, form="nope", channel=world.apply_channel)

    assert excinfo.value.key == "forms.unknown_form"


async def test_post_rejects_a_capacity_on_a_non_signup_form(cog, world) -> None:
    await bind_all(cog, world)
    interaction = FakeInteraction(user=world.reviewer, guild=world.guild)

    with pytest.raises(UserError) as excinfo:
        await run_command(cog.bot, "form post", interaction, form=APPLICATION, capacity=3)

    assert excinfo.value.key == "forms.capacity_only_signup"


async def test_post_sends_a_card_and_records_its_message(cog, world) -> None:
    await bind_all(cog, world)

    publication = await publish(cog, world)

    sent = world.apply_channel._sent[-1]
    assert sent["embed"].title == "团队申请"
    assert isinstance(sent["view"], FormOpenView)
    assert publication.message_id is not None
    assert world.apply_channel._messages[publication.message_id] is not None, "卡片要能按 id 找回来"


async def test_bind_updates_only_what_is_given(cog, world) -> None:
    interaction = FakeInteraction(user=world.reviewer, guild=world.guild)

    await run_command(
        cog.bot,
        "form bind",
        interaction,
        form=APPLICATION,
        review_channel=world.review_channel,
        grant_role=world.grant_role,
    )
    await run_command(cog.bot, "form bind", interaction, form=APPLICATION, reviewer_role=world.review_role)

    binding = await cog.store.bindings.get(GUILD_ID, APPLICATION)
    assert binding.review_channel_id == REVIEW_CHANNEL_ID
    assert binding.grant_role_id == world.grant_role.id
    assert binding.reviewer_role_id == world.review_role.id


async def test_list_shows_every_form_and_its_bindings(cog, world) -> None:
    await bind_all(cog, world)
    interaction = FakeInteraction(user=world.reviewer, guild=world.guild)

    await run_command(cog.bot, "form list", interaction)

    embed = interaction.response._messages[-1]["embed"]
    names = {field.name for field in embed.fields}
    assert any(APPLICATION in name for name in names)
    assert any("feedback" in name for name in names)


async def test_results_and_export(cog, world) -> None:
    await cog.store.submissions.create(
        GUILD_ID,
        form_id="feedback",
        publication_id=None,
        discord_user_id=APPLICANT_ID,
        answers={"topics": ["mods"], "detail": "很好"},
        status=RECEIVED,
        attachment_dir=new_storage_key(),
    )
    interaction = FakeInteraction(user=world.reviewer, guild=world.guild)

    await run_command(cog.bot, "form results", interaction, form="feedback")
    await run_command(cog.bot, "form export", interaction, form="feedback")

    results_embed = interaction.response._messages[-2]["embed"]
    assert "模组" in results_embed.description
    payload = interaction.response._messages[-1]["file"]
    assert payload.filename == "feedback.csv"
    assert payload.fp.read().startswith(b"\xef\xbb\xbf"), "CSV 带 BOM，Excel 才不会把中文读成乱码"


async def test_export_refuses_when_there_is_nothing(cog, world) -> None:
    interaction = FakeInteraction(user=world.reviewer, guild=world.guild)

    with pytest.raises(UserError) as excinfo:
        await run_command(cog.bot, "form export", interaction, form="feedback")

    assert excinfo.value.key == "forms.export.empty"


async def test_close_marks_the_publication_closed(cog, world) -> None:
    await bind_all(cog, world)
    publication = await publish(cog, world)
    card = world.apply_channel._messages[publication.message_id]
    interaction = FakeInteraction(user=world.reviewer, guild=world.guild)

    await run_command(cog.bot, "form close", interaction, publication=publication.publication_id)

    latest = await cog.store.publications.get(publication.publication_id)
    assert latest is not None and latest.status == CLOSED
    assert card._edits[-1]["view"] is None


# ---------------------------------------------------------------- 填写


async def test_open_button_sends_the_first_step_modal(cog, world) -> None:
    await bind_all(cog, world)
    publication = await publish(cog, world)
    interaction = FakeInteraction(user=world.applicant, guild=world.guild, channel=world.apply_channel)
    interaction.message = add_message(world.apply_channel, message_id=publication.message_id)

    await cog.handle_open(interaction)

    modal = interaction.response._modals[0]
    assert modal.step_index == 0
    assert modal.title == "团队申请"
    assert [item.component.custom_id for item in modal.children] == [
        "forms:field:work",
        "forms:field:availability",
        "forms:field:experience",
        "forms:field:portfolio",
        "forms:field:samples",
    ]


async def test_open_button_refuses_unknown_or_closed_cards(cog, world) -> None:
    await bind_all(cog, world)
    publication = await publish(cog, world)
    interaction = FakeInteraction(user=world.applicant, guild=world.guild)

    interaction.message = add_message(world.apply_channel, message_id=1)
    await cog.handle_open(interaction)
    assert "找不到" in interaction.response._messages[-1]["embed"].description

    interaction.message = add_message(world.apply_channel, message_id=publication.message_id)
    await cog.store.publications.set_status(publication.publication_id, CLOSED)
    await cog.handle_open(interaction)
    assert "已经关闭" in interaction.response._messages[-1]["embed"].description


async def test_open_button_refuses_a_second_submission(cog, world) -> None:
    await bind_all(cog, world)
    publication = await publish(cog, world)
    await cog.store.submissions.create(
        GUILD_ID,
        form_id=APPLICATION,
        publication_id=publication.publication_id,
        discord_user_id=APPLICANT_ID,
        answers=SUMMARY,
        status=PENDING,
        attachment_dir=new_storage_key(),
    )
    interaction = FakeInteraction(user=world.applicant, guild=world.guild)
    interaction.message = add_message(world.apply_channel, message_id=publication.message_id)

    await cog.handle_open(interaction)

    assert "等审核" in interaction.response._messages[-1]["embed"].description
    assert interaction.response._modals == []


async def test_open_button_resumes_a_half_filled_draft(cog, world) -> None:
    await bind_all(cog, world)
    publication = await publish(cog, world)
    await cog.store.drafts.save(
        GUILD_ID,
        form_id=APPLICATION,
        discord_user_id=APPLICANT_ID,
        step=1,
        answers=SUMMARY,
    )
    interaction = FakeInteraction(user=world.applicant, guild=world.guild)
    interaction.message = add_message(world.apply_channel, message_id=publication.message_id)

    await cog.handle_open(interaction)

    modal = interaction.response._modals[0]
    assert modal.step_index == 1
    assert [item.component.custom_id for item in modal.children] == ["forms:field:contact"]
    assert modal.children[0].component.value == ""
    assert modal.children[0].component.required is False


async def test_a_half_filled_form_is_kept_as_a_draft(cog, world) -> None:
    await bind_all(cog, world)
    publication = await publish(cog, world)

    interaction = await submit_step(cog, world, publication, user=world.applicant, step_index=0, answers=SUMMARY)

    draft = await cog.store.drafts.get(GUILD_ID, APPLICATION, APPLICANT_ID)
    assert draft is not None and draft.step == 1
    assert draft.answers["experience"] == "翻过两个模组"
    assert isinstance(interaction.response._edits[-1]["view"], FormResumeView)
    assert await cog.store.submissions.latest_for_user(GUILD_ID, APPLICATION, APPLICANT_ID) is None


async def test_the_last_step_creates_the_submission_and_posts_the_review_card(cog, world) -> None:
    await bind_all(cog, world)
    publication = await publish(cog, world)

    await submit_step(cog, world, publication, user=world.applicant, step_index=0, answers=SUMMARY)
    interaction = await submit_step(cog, world, publication, user=world.applicant, step_index=1, answers=CONTACT)

    submission = await cog.store.submissions.latest_for_user(GUILD_ID, APPLICATION, APPLICANT_ID)
    assert submission is not None
    assert submission.status == PENDING
    assert submission.answers["contact"] == "@me"
    assert await cog.store.drafts.get(GUILD_ID, APPLICATION, APPLICANT_ID) is None
    assert "私信" in interaction.response._edits[-1]["embed"].description

    card = world.review_channel._sent[-1]["embed"]
    assert {field.name: field.value for field in card.fields}["想做哪类工作"] == "翻译"
    assert isinstance(world.review_channel._sent[-1]["view"], FormReviewView)
    assert world.notify_channel._sent, "绑了通知频道就该收到一条新提交通知"
    assert world.applicant._dms, "申请人应该收到回执私信"


async def test_attachments_are_downloaded_into_the_storage_directory(cog, world) -> None:
    await bind_all(cog, world)
    publication = await publish(cog, world)
    attachment = make_attachment(filename="作品.png", data=b"png-bytes")

    await submit_step(
        cog,
        world,
        publication,
        user=world.applicant,
        step_index=0,
        answers={**SUMMARY, "samples": [attachment]},
    )
    await submit_step(cog, world, publication, user=world.applicant, step_index=1, answers=CONTACT)

    submission = await cog.store.submissions.latest_for_user(GUILD_ID, APPLICATION, APPLICANT_ID)
    assert submission is not None
    stored = submission.answers["samples"]
    assert [entry["name"] for entry in stored] == ["作品.png"]
    path = cog._dir_for(submission.attachment_dir) / stored[0]["stored"]
    assert path.read_bytes() == b"png-bytes"
    assert path.name.endswith("作品.png")


async def test_a_broken_attachment_stops_the_submission(cog, world) -> None:
    await bind_all(cog, world)
    publication = await publish(cog, world)

    interaction = await submit_step(
        cog,
        world,
        publication,
        user=world.applicant,
        step_index=0,
        answers={**SUMMARY, "samples": [make_attachment(fetchable=False)]},
    )
    # 上一步没落库，所以第二步不该被受理 —— 否则会存下一份缺字段的申请。
    later = await submit_step(cog, world, publication, user=world.applicant, step_index=1, answers=CONTACT)

    assert await cog.store.submissions.latest_for_user(GUILD_ID, APPLICATION, APPLICANT_ID) is None
    assert "附件" in interaction.followup._sent[-1]["embed"].description
    assert "第一步" in later.followup._sent[-1]["embed"].description


async def test_a_signup_stops_at_capacity(cog, world) -> None:
    await cog.store.bindings.update(GUILD_ID, "event_signup", notify_channel_id=NOTIFY_CHANNEL_ID)
    publication = await publish(cog, world, form="event_signup", capacity=1)

    first = await submit_step(
        cog, world, publication, user=world.applicant, step_index=0, answers={"slot": ["day1"], "note": "hi"}
    )
    second = await submit_step(
        cog, world, publication, user=world.reviewer, step_index=0, answers={"slot": ["day2"], "note": "me"}
    )

    assert "名额" in first.response._edits[-1]["embed"].description
    assert await cog.store.submissions.count_for_form(GUILD_ID, "event_signup") == 1
    assert "名额" in second.followup._sent[-1]["embed"].description


# ---------------------------------------------------------------- 审核


async def test_approving_asks_first_and_changes_nothing_until_confirmed(cog, world, monkeypatch) -> None:
    submission, card = await approve_ready(cog, world)
    calls: list[str] = []

    async def decline(interaction, *, embed, view):
        calls.append(embed.title)
        return False

    monkeypatch.setattr(forms_module, "ask_confirmation", decline)
    interaction = FakeInteraction(user=world.reviewer, guild=world.guild)
    interaction.message = card

    await cog.handle_review(interaction, APPROVE_ID)

    latest = await cog.store.submissions.get(submission.submission_id)
    assert calls == ["确认通过"]
    assert latest is not None and latest.status == PENDING
    assert world.grant_role not in world.applicant.roles


async def test_approving_grants_the_role_and_closes_the_card(cog, world, monkeypatch) -> None:
    submission, card = await approve_ready(cog, world)

    async def accept(interaction, *, embed, view):
        return True

    monkeypatch.setattr(forms_module, "ask_confirmation", accept)
    interaction = FakeInteraction(user=world.reviewer, guild=world.guild)
    interaction.message = card

    await cog.handle_review(interaction, APPROVE_ID)

    latest = await cog.store.submissions.get(submission.submission_id)
    assert latest is not None and latest.status == APPROVED
    assert latest.reviewed_by == REVIEWER_ID
    assert world.grant_role in world.applicant.roles
    assert world.notify_channel._sent[-1]["embed"].title == "团队申请已通过"
    assert card._edits[-1]["view"] is None
    assert any("通过" in message for message in world.applicant._dms)


async def test_a_failed_action_keeps_the_application_pending(cog, world, monkeypatch) -> None:
    submission, card = await approve_ready(cog, world)
    await cog.store.bindings.update(GUILD_ID, APPLICATION, grant_role_id=None)

    async def accept(interaction, *, embed, view):
        return True

    monkeypatch.setattr(forms_module, "ask_confirmation", accept)
    interaction = FakeInteraction(user=world.reviewer, guild=world.guild)
    interaction.message = card

    await cog.handle_review(interaction, APPROVE_ID)

    latest = await cog.store.submissions.get(submission.submission_id)
    assert latest is not None and latest.status == PENDING, "动作没成功就不能标记通过"
    # 确认卡片被拒掉了，所以回执走的是原先那次响应（不是 followup）。
    assert "先绑角色" in interaction.response._messages[-1]["embed"].description
    assert world.grant_role not in world.applicant.roles


async def test_a_role_grant_that_discord_refuses_keeps_it_pending(cog, world, monkeypatch) -> None:
    """发角色真的失败（403）时同样不能标记通过 —— 否则记录说通过了、实际什么都没发生。"""
    submission, card = await approve_ready(cog, world)

    async def accept(interaction, *, embed, view):
        return True

    async def refuse(*args, **kwargs):
        raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "missing permissions")

    monkeypatch.setattr(forms_module, "ask_confirmation", accept)
    monkeypatch.setattr(world.applicant, "add_roles", refuse)
    interaction = FakeInteraction(user=world.reviewer, guild=world.guild)
    interaction.message = card

    await cog.handle_review(interaction, APPROVE_ID)

    latest = await cog.store.submissions.get(submission.submission_id)
    assert latest is not None and latest.status == PENDING
    assert "待审" in interaction.response._messages[-1]["embed"].description


async def test_rejecting_records_the_reason_and_tells_the_applicant(cog, world) -> None:
    submission, card = await approve_ready(cog, world)
    interaction = FakeInteraction(user=world.reviewer, guild=world.guild)
    interaction.message = card

    await cog.decide(interaction, submission=submission, decision=REJECT_ID, reason="经验不足")

    latest = await cog.store.submissions.get(submission.submission_id)
    assert latest is not None and latest.status == REJECTED
    assert latest.decision_reason == "经验不足"
    assert card._edits[-1]["view"] is None
    assert any("经验不足" in message for message in world.applicant._dms)


async def test_needs_info_reopens_the_form_and_reuses_the_submission(cog, world) -> None:
    submission, card = await approve_ready(cog, world)
    interaction = FakeInteraction(user=world.reviewer, guild=world.guild)
    interaction.message = card

    await cog.decide(interaction, submission=submission, decision=NEEDS_INFO_ID, reason="补个作品链接")

    latest = await cog.store.submissions.get(submission.submission_id)
    assert latest is not None and latest.status == NEEDS_INFO
    assert any("补个作品链接" in message for message in world.applicant._dms)

    publication = (await cog.store.publications.open_in_guild(GUILD_ID, APPLICATION))[-1]
    open_interaction = FakeInteraction(user=world.applicant, guild=world.guild)
    open_interaction.message = add_message(world.apply_channel, message_id=publication.message_id)
    await cog.handle_open(open_interaction)
    modal = open_interaction.response._modals[0]
    work = next(item.component for item in modal.children if item.component.custom_id == "forms:field:work")
    assert [option.default for option in work.options] == [True, False, False], "要预填上一版的选择"

    await submit_step(cog, world, publication, user=world.applicant, step_index=0, answers=SUMMARY)
    await submit_step(cog, world, publication, user=world.applicant, step_index=1, answers=CONTACT)

    again = await cog.store.submissions.latest_for_user(GUILD_ID, APPLICATION, APPLICANT_ID)
    assert again is not None
    assert again.submission_id == submission.submission_id, "补充材料是同一条申请，不该新建一条"
    assert again.status == PENDING
    assert again.decision_reason is None
    assert len(world.review_channel._sent) == 1, "卡片是更新而不是重发"
    assert card._edits[-1]["view"] is not None, "回到待审后重新长出按钮"


async def test_a_resubmission_keeps_files_that_were_already_uploaded(cog, world) -> None:
    """弹窗没法预填附件，所以申请人回头补充时不能因为「这一步没重传」就把旧文件丢掉。"""
    submission, card = await approve_ready(cog, world, samples=[make_attachment(filename="作品.png", data=b"png")])
    reviewer_interaction = FakeInteraction(user=world.reviewer, guild=world.guild)
    reviewer_interaction.message = card

    await cog.decide(reviewer_interaction, submission=submission, decision=NEEDS_INFO_ID, reason="再补充一点")

    publication = (await cog.store.publications.open_in_guild(GUILD_ID, APPLICATION))[-1]
    open_interaction = FakeInteraction(user=world.applicant, guild=world.guild)
    open_interaction.message = add_message(world.apply_channel, message_id=publication.message_id)
    await cog.handle_open(open_interaction)
    await submit_step(cog, world, publication, user=world.applicant, step_index=0, answers={**SUMMARY, "samples": []})
    await submit_step(cog, world, publication, user=world.applicant, step_index=1, answers=CONTACT)

    again = await cog.store.submissions.get(submission.submission_id)
    assert again is not None
    stored = again.answers["samples"]
    assert [entry["name"] for entry in stored] == ["作品.png"]
    assert (cog._dir_for(again.attachment_dir) / stored[0]["stored"]).exists()


async def test_only_the_reviewer_role_may_decide(cog, world) -> None:
    submission, card = await approve_ready(cog, world)
    interaction = FakeInteraction(user=world.applicant, guild=world.guild)
    interaction.message = card

    await cog.decide(interaction, submission=submission, decision=REJECT_ID, reason="不")

    latest = await cog.store.submissions.get(submission.submission_id)
    assert latest is not None and latest.status == PENDING
    assert "不归你审" in interaction.followup._sent[-1]["embed"].description


async def test_the_owner_can_always_review_even_without_the_role(cog, world) -> None:
    submission, card = await approve_ready(cog, world)
    owner = world.guild.get_member(MODERATOR_ID)
    assert owner is not None
    interaction = FakeInteraction(user=owner, guild=world.guild)
    interaction.message = card

    await cog.decide(interaction, submission=submission, decision=REJECT_ID, reason="不合适")

    latest = await cog.store.submissions.get(submission.submission_id)
    assert latest is not None and latest.status == REJECTED


async def test_a_stale_review_button_does_nothing(cog, world) -> None:
    interaction = FakeInteraction(user=world.reviewer, guild=world.guild)
    interaction.message = add_message(world.review_channel, message_id=123456)

    await cog.handle_review(interaction, APPROVE_ID)

    assert "找不到" in interaction.response._messages[-1]["embed"].description


# ---------------------------------------------------------------- 后台任务


async def test_due_publications_are_closed_and_the_card_updated(cog, world, monkeypatch) -> None:
    await bind_all(cog, world)
    publication = await publish(cog, world, minutes=1)
    card = world.apply_channel._messages[publication.message_id]
    await cog.bot.db.execute(
        "UPDATE form_publications SET closes_at = ? WHERE publication_id = ?",
        ("2026-01-01T00:00:00+00:00", publication.publication_id),
    )
    monkeypatch.setattr(cog.bot, "get_guild", lambda guild_id: world.guild)

    closed = await cog.close_due()

    latest = await cog.store.publications.get(publication.publication_id)
    assert closed == 1
    assert latest is not None and latest.status == CLOSED
    assert card._edits[-1]["view"] is None
    assert "已经关闭" in card._edits[-1]["embed"].description


async def test_stale_drafts_are_purged_along_with_their_files(cog, world) -> None:
    draft = await cog.store.drafts.save(
        GUILD_ID,
        form_id=APPLICATION,
        discord_user_id=APPLICANT_ID,
        step=1,
        answers=SUMMARY,
    )
    directory = cog._dir_for(draft.storage_key)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "leftover.png").write_bytes(b"x")
    await cog.bot.db.execute("UPDATE form_drafts SET updated_at = ?", ("2026-01-01T00:00:00+00:00",))

    removed = await cog.purge_stale_drafts()

    assert removed == 1
    assert await cog.store.drafts.get(GUILD_ID, APPLICATION, APPLICANT_ID) is None
    assert not directory.exists()


async def test_attachment_directories_refuse_anything_but_our_own_keys(cog) -> None:
    assert cog._dir_for(new_storage_key()).name.isalnum()
    for bad in ("../../etc", "not-a-key", ""):
        with pytest.raises(ValueError):
            cog._dir_for(bad)


async def test_the_card_buttons_are_registered_as_persistent_views(cog) -> None:
    assert FormOpenView(cog).children[0].custom_id == OPEN_ID
    assert [item.custom_id for item in FormReviewView(cog).children] == [APPROVE_ID, REJECT_ID, NEEDS_INFO_ID]
    assert FormResumeView(cog, publication_id=7, label="继续").children[0].custom_id == "forms:resume:7"
    assert any(type(view) is FormOpenView for view in cog.bot.persistent_views), "卡片按钮要注册成持久化 view"
    assert any(type(view) is FormReviewView for view in cog.bot.persistent_views)

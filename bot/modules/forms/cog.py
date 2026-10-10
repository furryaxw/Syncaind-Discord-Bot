"""forms：表单 —— 申请（走审核）、收集、报名（占名额）。

三种用途共用同一套东西：定义、分步弹窗、字段类型（短文本/长文/单选/多选/附件）、
草稿、落库、附件转存、卡片与通知。差异只在定义里的 ``mode``。

几条不能省的实现纪律，每条都有来由：

1. **按钮状态按 message_id 反查，不存在 view 里。** 持久化 view 只注册一个实例，
   点击时 discord.py 调用的是**那个实例**的回调；把卡片状态塞进 view 就会串台。见 ``store``。
2. **打开弹窗这条路上不能 defer。** ``send_modal`` 本身就是那次响应，先 defer 就没法再弹窗；
   所以这条路上只做几次本地读库，并且把「从交互创建起多少毫秒」记进日志（那才是和 3 秒窗口
   直接对应的数字）。提交弹窗是另一次交互，那里才是先 ``defer`` 再干活。
3. **附件一提交就落盘。** Discord 给的下载链接带签名、会过期，所以弹窗一提交就下载进
   ``data/attachments/<key>/``，答案里只留文件名。分步表单里每一步都这么处理，草稿被放弃时
   由后台任务连目录一起清掉。
4. **通过后动作先做、状态后改。** 发角色或通知失败时申请**保持待审**，审核员再点一次即可
   （发角色是幂等的，重复点不会发两遍）。反过来先标记通过再动作，就会出现「记录说通过了、
   实际什么都没发生」。
5. **报名名额用一条 SQL 自增封顶**，并发提交不会超员；占了名额但落库失败要把名额还回去。
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import secrets
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot.core.audit import audit_reason
from bot.core.clock import utcnow_iso
from bot.core.errors import UserError
from bot.core.translator import localized
from bot.core.ui import ConfirmView, ask_confirmation, error_embed, info_embed, ok_embed, warn_embed

from . import rendering, schema
from .schema import FormDefinition
from .store import (
    APPROVED,
    CLOSED,
    NEEDS_INFO,
    PENDING,
    RECEIVED,
    REJECTED,
    FormBinding,
    FormPublication,
    FormStores,
    FormSubmission,
    build_stores,
    new_storage_key,
)

LOGGER = logging.getLogger("bot.forms")

# 持久化按钮的 custom_id。卡片上的按钮不带状态，点进来按消息 id 反查。
OPEN_ID = "forms:open"
APPROVE_ID = "forms:approve"
REJECT_ID = "forms:reject"
NEEDS_INFO_ID = "forms:needs_info"
RESUME_PREFIX = "forms:resume:"

REASON_INPUT_ID = "forms:reason"

SWEEP_SECONDS = 30
DRAFT_TTL_HOURS = 24
EXPORT_LIMIT = 500
MAX_RESULTS = 25
DEFAULT_RESULTS = 5
MODAL_TITLE_LIMIT = 45
REASON_LIMIT = 400

# Discord 的「这条交互已经不存在了」：令牌过期（交互创建后超过 3 秒）时它就是这么回的。
EXPIRED_INTERACTION = 10062

# 附件目录名只由我们自己生成：形如 32 位十六进制。用它挡路径穿越。
STORAGE_KEY_PATTERN = re.compile(r"^[0-9a-f]{32}$")
UNSAFE_NAME_PATTERN = re.compile(r"[^\w.\- ]", re.UNICODE)


def _age_ms(interaction: discord.Interaction) -> float:
    created = getattr(interaction, "created_at", None)
    if created is None:
        return -1.0
    return (discord.utils.utcnow() - created).total_seconds() * 1000.0


def _now_plus(minutes: int) -> str:
    return (datetime.fromisoformat(utcnow_iso()) + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def _now_minus(hours: int) -> str:
    return (datetime.fromisoformat(utcnow_iso()) - timedelta(hours=hours)).isoformat(timespec="seconds")


def safe_filename(name: str) -> str:
    """把外部文件名洗成安全的、只含单层文件名的形式。"""
    cleaned = UNSAFE_NAME_PATTERN.sub("_", str(name or "")).strip() or "file"
    return cleaned[-80:]


def merge_dir(source: Path, destination: Path) -> None:
    """把 ``source`` 里的文件逐个搬进 ``destination``（文件名的唯一性由调用方保证）。"""
    destination.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        if item.is_file():
            os.replace(item, destination / item.name)
    try:
        source.rmdir()
    except OSError:
        # 还有东西剩下（理论上不会）：留着，让后台清理任务下次再看。
        LOGGER.warning("草稿附件目录没能删掉：%s", source)


async def form_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    i18n = getattr(interaction.client, "i18n", None)
    get_cog = getattr(interaction.client, "get_cog", None)
    cog = get_cog("FormsCog") if get_cog is not None else None
    catalog = getattr(cog, "catalog", None)
    if catalog is None:
        return []
    locale = getattr(interaction, "locale", None)
    lowered = current.lower()
    choices: list[app_commands.Choice[str]] = []
    for definition in catalog.all():
        if lowered not in definition.id.lower():
            continue
        name = schema.pick(definition.title, i18n.resolve(locale)) if i18n is not None else definition.id
        choices.append(app_commands.Choice(name=name[:100], value=definition.id))
    return choices[:25]


class FormOpenView(discord.ui.View):
    """卡片上那个按钮。**不带任何状态** —— 点进来按消息 id 反查。"""

    def __init__(self, cog: FormsCog, *, label: str | None = None) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        button = discord.ui.Button(
            style=discord.ButtonStyle.success,
            label=label or "Open",
            custom_id=OPEN_ID,
        )
        button.callback = self._on_click  # type: ignore[method-assign]
        self.add_item(button)

    async def _on_click(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_open(interaction)


class FormReviewView(discord.ui.View):
    """审核卡片上的三个按钮。同样不带状态：按消息 id 反查是哪一条申请。"""

    def __init__(self, cog: FormsCog, *, labels: dict[str, str] | None = None) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        text = labels or {}
        specs = (
            (APPROVE_ID, discord.ButtonStyle.success),
            (REJECT_ID, discord.ButtonStyle.danger),
            (NEEDS_INFO_ID, discord.ButtonStyle.secondary),
        )
        for custom_id, style in specs:
            button = discord.ui.Button(style=style, label=text.get(custom_id, custom_id), custom_id=custom_id)
            button.callback = self._make_callback(custom_id)
            self.add_item(button)

    def _make_callback(self, custom_id: str):
        async def callback(interaction: discord.Interaction) -> None:
            await self.cog.handle_review(interaction, custom_id)

        return callback


class FormResumeView(discord.ui.View):
    """「继续填写」那个按钮。custom_id 里带着发布编号，所以状态仍然不在 view 里。"""

    def __init__(self, cog: FormsCog, *, publication_id: int, label: str) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.publication_id = publication_id
        button = discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label=label,
            custom_id=f"{RESUME_PREFIX}{publication_id}",
        )
        button.callback = self._on_click  # type: ignore[method-assign]
        self.add_item(button)

    async def _on_click(self, interaction: discord.Interaction) -> None:
        await self.cog.resume_step(interaction, publication_id=self.publication_id)


class FormStepModal(discord.ui.Modal):
    """一步的弹窗。字段是按定义动态加的，所以类里不声明任何类级组件。"""

    def __init__(
        self,
        cog: FormsCog,
        *,
        publication: FormPublication,
        definition: FormDefinition,
        step_index: int,
        prefill: dict[str, Any],
        title: str,
        tr: rendering.Translate,
        text: rendering.FormText,
    ) -> None:
        super().__init__(title=rendering.clip(title, MODAL_TITLE_LIMIT), timeout=None)
        self.cog = cog
        self.publication = publication
        self.definition = definition
        self.step_index = step_index
        for item in rendering.build_step_items(definition, step_index, text=text, tr=tr, prefill=prefill):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        answers = rendering.collect_answers(self)
        await self.cog.submit_step(
            interaction,
            publication=self.publication,
            definition=self.definition,
            step_index=self.step_index,
            answers=answers,
        )


class FormReasonModal(discord.ui.Modal):
    """驳回或要求补充时把理由写下来。"""

    def __init__(
        self,
        cog: FormsCog,
        *,
        submission: FormSubmission,
        decision: str,
        title: str,
        label: str,
    ) -> None:
        super().__init__(title=rendering.clip(title, MODAL_TITLE_LIMIT), timeout=None)
        self.cog = cog
        self.submission = submission
        self.decision = decision
        self.input = discord.ui.TextInput(
            custom_id=REASON_INPUT_ID,
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=REASON_LIMIT,
        )
        self.add_item(discord.ui.Label(text=rendering.clip(label, MODAL_TITLE_LIMIT), component=self.input))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.decide(
            interaction,
            submission=self.submission,
            decision=self.decision,
            reason=str(self.input.value or "").strip(),
        )


@app_commands.guild_only()
class FormsCog(commands.Cog):
    """申请、收集、报名都用同一套表单。"""

    form_group = app_commands.Group(
        name="form",
        description=localized("Forms: applications, signups and surveys", "commands.form.description"),
    )

    def __init__(
        self,
        bot: Any,
        stores: FormStores | None = None,
        *,
        catalog: schema.FormCatalog | None = None,
        definitions_dir: Path | None = None,
        attachments_root: Path | None = None,
    ) -> None:
        self.bot = bot
        self.store = stores or build_stores(bot.db, logger=bot.log.getChild("forms"))
        self.catalog = catalog or schema.FormCatalog()
        self.definitions_dir = Path(definitions_dir) if definitions_dir is not None else None
        root = attachments_root or (Path(bot.settings.database_path).parent / "attachments")
        self.attachments_root = Path(root)
        self._sweeper: Any = None

    async def cog_unload(self) -> None:
        self.stop_sweeping()

    # ------------------------------------------------------------------ 语言与路径

    def translator(self, *, prefill_locale: Any = None) -> rendering.Translate:
        """返回一个 ``tr(key, **kwargs)``。语言在调用处定，默认跟随调用者。"""
        locale = prefill_locale

        def tr(key: str, **kwargs: Any) -> str:
            return self.bot.i18n.t(locale, key, **kwargs)

        return tr

    async def guild_tr(self, guild_id: int) -> rendering.Translate:
        """公开卡片与私信都用**服务器语言**（没配则用默认语言）。

        命令回执可以用 ``interaction.locale``（那是管理员自己的客户端语言），但频道里的卡片
        是给所有人看的：同一条消息里混两种语言比统一用服务器语言更糟。
        """
        settings = await self.bot.guild_settings.get(guild_id)
        return self.translator(prefill_locale=settings.locale)

    def form_text(self, locale: Any = None) -> rendering.FormText:
        """表单自己的文案：定义 JSON 里按语言标签写着，取当前语言，缺了就回落到第一条。"""
        tag = self.bot.i18n.resolve(locale)

        def text(value: schema.Localized) -> str:
            return schema.pick(value, tag)

        return text

    async def guild_texts(self, guild_id: int) -> tuple[rendering.Translate, rendering.FormText]:
        """一次拿到「模块文案」与「表单文案」两个查询函数 —— 它们必须用同一种语言。"""
        settings = await self.bot.guild_settings.get(guild_id)
        return self.translator(prefill_locale=settings.locale), self.form_text(settings.locale)

    def _dir_for(self, key: str) -> Path:
        if not STORAGE_KEY_PATTERN.match(key):
            raise ValueError(f"附件目录名非法：{key!r}")
        return self.attachments_root / key

    def _remove_dir(self, key: str) -> None:
        path = self._dir_for(key)
        if path.is_dir():
            shutil.rmtree(path)

    # ------------------------------------------------------------------ 后台任务

    def start_sweeping(self) -> None:
        if self._sweeper is None or self._sweeper.done():
            self._sweeper = asyncio.create_task(self._sweep_loop())

    def stop_sweeping(self) -> None:
        if self._sweeper is not None and not self._sweeper.done():
            self._sweeper.cancel()
        self._sweeper = None

    async def _sweep_loop(self) -> None:
        while True:
            try:
                await self.close_due()
                await self.purge_stale_drafts()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("表单后台任务出错，下一轮继续")
            await asyncio.sleep(SWEEP_SECONDS)

    async def close_due(self) -> int:
        """到点自动关闭。跨重启不会漏：判断依据是库里的 closes_at，不是内存里的计时器。"""
        closed = 0
        for publication in await self.store.publications.due(utcnow_iso()):
            await self.store.publications.set_status(publication.publication_id, CLOSED)
            closed += 1
            await self._update_card(publication, closed=True)
        return closed

    async def purge_stale_drafts(self) -> int:
        """清掉太久没动的草稿与它已经下下来的附件。"""
        removed = 0
        for draft in await self.store.drafts.stale(_now_minus(DRAFT_TTL_HOURS)):
            await self.store.drafts.delete(draft.guild_id, draft.form_id, draft.discord_user_id)
            self._remove_dir(draft.storage_key)
            removed += 1
        if removed:
            LOGGER.info("清掉了 %d 份过期的表单草稿", removed)
        return removed

    # ------------------------------------------------------------------ 入口：卡片按钮

    async def handle_open(self, interaction: discord.Interaction) -> None:
        """卡片按钮入口。**不能 defer**（下一步要 ``send_modal``）。"""
        publication: FormPublication | None = None
        try:
            message = getattr(interaction, "message", None)
            publication = await self.store.publications.get_by_message(getattr(message, "id", 0))
            if publication is None:
                raise UserError("forms.not_found")
            definition = self._definition(publication.form_id)
            guild = self._require_guild(interaction)
            if publication.guild_id != guild.id:
                raise UserError("forms.not_found")
            if not publication.is_open:
                raise UserError("forms.closed")
            if definition.is_signup and publication.is_full:
                raise UserError("forms.full")

            step, prefill = await self._open_state(interaction, publication, definition, guild.id)
            tr, text = await self.guild_texts(guild.id)
            await interaction.response.send_modal(
                FormStepModal(
                    self,
                    publication=publication,
                    definition=definition,
                    step_index=step,
                    prefill=prefill,
                    title=text(definition.title),
                    tr=tr,
                    text=text,
                )
            )
        except UserError as exc:
            await self._respond_error(interaction, exc)
        except Exception:
            LOGGER.exception("处理表单按钮失败：user=%s", getattr(interaction.user, "id", "?"))
            await self._respond_error(interaction, UserError("errors.unexpected"))
        finally:
            LOGGER.info(
                "表单按钮处理完成（从交互创建起 %.0f ms）：user=%s publication=%s",
                _age_ms(interaction),
                getattr(interaction.user, "id", "?"),
                publication.publication_id if publication is not None else "?",
            )

    async def resume_step(self, interaction: discord.Interaction, *, publication_id: int) -> None:
        """「继续填写」按钮入口，与卡片按钮同一条逻辑，只是发布编号来自 custom_id。"""
        try:
            publication = await self.store.publications.get(publication_id)
            if publication is None:
                raise UserError("forms.not_found")
            definition = self._definition(publication.form_id)
            guild = self._require_guild(interaction)
            if publication.guild_id != guild.id:
                raise UserError("forms.not_found")
            if not publication.is_open:
                raise UserError("forms.closed")
            step, prefill = await self._open_state(interaction, publication, definition, guild.id)
            tr, text = await self.guild_texts(guild.id)
            await interaction.response.send_modal(
                FormStepModal(
                    self,
                    publication=publication,
                    definition=definition,
                    step_index=step,
                    prefill=prefill,
                    title=text(definition.title),
                    tr=tr,
                    text=text,
                )
            )
        except UserError as exc:
            await self._respond_error(interaction, exc)
        except Exception:
            LOGGER.exception("处理继续填写按钮失败：user=%s", getattr(interaction.user, "id", "?"))
            await self._respond_error(interaction, UserError("errors.unexpected"))

    async def _open_state(
        self,
        interaction: discord.Interaction,
        publication: FormPublication,
        definition: FormDefinition,
        guild_id: int,
    ) -> tuple[int, dict[str, Any]]:
        """能不能填、从第几步开始、有没有已填的内容。"""
        user_id = interaction.user.id
        submission = await self.store.submissions.latest_for_user(guild_id, definition.id, user_id)
        if submission is not None:
            if submission.status == NEEDS_INFO:
                # 把上一版答案写进草稿：申请人重填时附件不必重新上传（弹窗没法预填文件），
                # 而且这样「补充材料」与普通填写走的是同一条合并逻辑。
                await self.store.drafts.save(
                    guild_id,
                    form_id=definition.id,
                    discord_user_id=user_id,
                    step=0,
                    answers=submission.answers,
                )
                return 0, submission.answers
            if submission.status == PENDING:
                raise UserError("forms.already_submitted")
            if submission.status == APPROVED:
                raise UserError("forms.already_approved")
            if submission.status == RECEIVED and not definition.allow_multiple:
                raise UserError("forms.already_answered")
        draft = await self.store.drafts.get(guild_id, definition.id, user_id)
        if draft is not None:
            return draft.step, draft.answers
        return 0, {}

    # ------------------------------------------------------------------ 提交弹窗

    async def submit_step(
        self,
        interaction: discord.Interaction,
        *,
        publication: FormPublication,
        definition: FormDefinition,
        step_index: int,
        answers: dict[str, Any],
    ) -> None:
        """处理一步的提交。**先 defer 再干活** —— 这一步之后要读库、下载附件、发消息。"""
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException as exc:
            self._log_expired(interaction, exc, "提交弹窗")
            return

        guild = self._require_guild(interaction)
        user_id = interaction.user.id
        try:
            latest = await self.store.publications.get(publication.publication_id)
            if latest is None or not latest.is_open:
                raise UserError("forms.closed")
            if definition.is_signup and latest.is_full:
                raise UserError("forms.full")

            draft = await self.store.drafts.get(guild.id, definition.id, user_id)
            if step_index > 0 and (draft is None or draft.step < step_index):
                # 前面几步没填完就从中间插进来，会落库一份缺字段的申请。
                raise UserError("forms.step_out_of_order")
            storage_key = draft.storage_key if draft is not None else new_storage_key()
            old = draft.answers if draft is not None else {}
            step_fields = definition.steps[step_index]
            stored = await self._store_attachments(answers, step_fields=step_fields, storage_key=storage_key)
            merged = self._merge(old, answers, step_fields=step_fields, stored=stored)

            tr, text = await self.guild_texts(guild.id)
            if step_index + 1 < definition.step_count:
                await self.store.drafts.save(
                    guild.id,
                    form_id=definition.id,
                    discord_user_id=user_id,
                    step=step_index + 1,
                    answers=merged,
                    storage_key=storage_key,
                )
                await interaction.edit_original_response(
                    embed=info_embed(
                        text(definition.title),
                        tr("forms.submitted.needs_more", done=step_index + 1, total=definition.step_count),
                    ),
                    view=FormResumeView(
                        self,
                        publication_id=latest.publication_id,
                        label=tr("forms.resume.button"),
                    ),
                )
                return

            await self._finalize(
                interaction,
                publication=latest,
                definition=definition,
                answers=merged,
                storage_key=storage_key,
                guild=guild,
                user_id=user_id,
            )
        except UserError as exc:
            await self._respond_error(interaction, exc)
        except Exception:
            LOGGER.exception(
                "处理表单提交失败：user=%s form=%s step=%s",
                user_id,
                definition.id,
                step_index,
            )
            await self._respond_error(interaction, UserError("errors.unexpected"))
        finally:
            LOGGER.info(
                "表单提交处理完成（从交互创建起 %.0f ms）：user=%s form=%s step=%s",
                _age_ms(interaction),
                user_id,
                definition.id,
                step_index,
            )

    async def _finalize(
        self,
        interaction: discord.Interaction,
        *,
        publication: FormPublication,
        definition: FormDefinition,
        answers: dict[str, Any],
        storage_key: str,
        guild: discord.Guild,
        user_id: int,
    ) -> None:
        """最后一步：占名额 → 落库 → 附件就位 → 通知。"""
        status = PENDING if definition.is_apply else RECEIVED
        existing = await self.store.submissions.latest_for_user(guild.id, definition.id, user_id)
        replacing = existing is not None and definition.is_apply and existing.status == NEEDS_INFO

        taken = False
        if definition.is_signup and publication.capacity is not None:
            if not await self.store.publications.take_slot(publication.publication_id):
                raise UserError("forms.full")
            taken = True

        draft_dir = self._dir_for(storage_key)
        target_key = existing.attachment_dir if replacing and existing is not None else new_storage_key()
        moved = False
        try:
            target_dir = self._dir_for(target_key)
            if draft_dir.is_dir():
                if replacing:
                    merge_dir(draft_dir, target_dir)
                else:
                    target_dir.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(draft_dir, target_dir)
                    moved = True
            if replacing and existing is not None:
                await self.store.submissions.replace_answers(existing.submission_id, answers=answers, status=PENDING)
                submission = await self.store.submissions.get(existing.submission_id)
            else:
                submission = await self.store.submissions.create(
                    guild.id,
                    form_id=definition.id,
                    publication_id=publication.publication_id,
                    discord_user_id=user_id,
                    answers=answers,
                    status=status,
                    attachment_dir=target_key,
                )
        except BaseException:
            if taken:
                await self.store.publications.give_back_slot(publication.publication_id)
            if moved:
                os.replace(self._dir_for(target_key), draft_dir)
            raise
        assert submission is not None

        await self.store.drafts.delete(guild.id, definition.id, user_id)
        tr, text = await self.guild_texts(guild.id)

        if definition.is_apply:
            if replacing and submission.review_message_id is not None:
                # 补充材料后还是同一条申请：更新原来那张卡片，不另发一张。
                await self._update_review_card(guild, definition, submission, tr=tr, text=text)
            else:
                await self._send_review_card(guild, definition, submission, tr=tr, text=text)
        await self._notify_new_submission(guild, definition, submission, tr=tr, text=text, replacing=replacing)
        await self._dm_applicant(
            guild,
            submission,
            tr=tr,
            key="forms.dm.received",
            form=text(definition.title),
        )

        if definition.is_apply:
            await interaction.edit_original_response(
                embed=ok_embed(tr("forms.submitted.apply_title"), tr("forms.submitted.apply")),
            )
            return
        if definition.is_signup and publication.capacity is not None:
            latest = await self.store.publications.get(publication.publication_id)
            remaining = latest.remaining if latest is not None else 0
            await interaction.edit_original_response(
                embed=ok_embed(
                    tr("forms.submitted.other_title"),
                    tr("forms.submitted.signup", remaining=remaining),
                ),
            )
            return
        await interaction.edit_original_response(
            embed=ok_embed(tr("forms.submitted.other_title"), tr("forms.submitted.other")),
        )

    # ------------------------------------------------------------------ 审核

    async def handle_review(self, interaction: discord.Interaction, decision: str) -> None:
        message = getattr(interaction, "message", None)
        submission = await self.store.submissions.get_by_review_message(getattr(message, "id", 0))
        if submission is None:
            await self._respond_error(interaction, UserError("forms.review.not_found"))
            return
        try:
            definition = self._definition(submission.form_id)
            guild = self._require_guild(interaction)
            await self._require_reviewer(interaction, definition, guild)
            if submission.status not in (PENDING, NEEDS_INFO):
                raise UserError("forms.review.already_decided")

            tr, text = await self.guild_texts(guild.id)
            if decision == APPROVE_ID:
                confirmed = await ask_confirmation(
                    interaction,
                    embed=warn_embed(
                        tr("forms.review.confirm_title"),
                        tr(
                            "forms.review.confirm_body",
                            form=text(definition.title),
                            user=f"<@{submission.discord_user_id}>",
                        ),
                    ),
                    view=ConfirmView(
                        author_id=interaction.user.id,
                        confirm_label=tr("forms.button.confirm"),
                        cancel_label=tr("forms.button.cancel"),
                        not_author_message=tr("forms.confirm.not_yours"),
                    ),
                )
                if not confirmed:
                    return
                await self._approve(
                    interaction, submission=submission, definition=definition, guild=guild, tr=tr, text=text
                )
                return

            if decision == REJECT_ID:
                title_key, label_key = "forms.review.reject_modal.title", "forms.review.reject_modal.label"
            else:
                title_key, label_key = (
                    "forms.review.needs_info_modal.title",
                    "forms.review.needs_info_modal.label",
                )
            await interaction.response.send_modal(
                FormReasonModal(
                    self,
                    submission=submission,
                    decision=decision,
                    title=tr(title_key),
                    label=tr(label_key),
                )
            )
        except UserError as exc:
            await self._respond_error(interaction, exc)
        except Exception:
            LOGGER.exception("处理审核按钮失败：submission=%s", submission.submission_id)
            await self._respond_error(interaction, UserError("errors.unexpected"))

    async def decide(
        self,
        interaction: discord.Interaction,
        *,
        submission: FormSubmission,
        decision: str,
        reason: str,
    ) -> None:
        """驳回 / 要求补充。理由来自弹窗，所以这里先 defer 再干活。"""
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException as exc:
            self._log_expired(interaction, exc, "审核理由")
            return

        status = REJECTED if decision == REJECT_ID else NEEDS_INFO
        try:
            definition = self._definition(submission.form_id)
            guild = self._require_guild(interaction)
            await self._require_reviewer(interaction, definition, guild)
            if not reason:
                raise UserError("forms.review.reason_required")
            current = await self.store.submissions.get(submission.submission_id)
            if current is None or current.status not in (PENDING, NEEDS_INFO):
                raise UserError("forms.review.already_decided")

            await self.store.submissions.set_decision(
                submission.submission_id,
                status=status,
                reviewer_id=interaction.user.id,
                reason=reason,
            )
            updated = await self.store.submissions.get(submission.submission_id)
            assert updated is not None
            tr, text = await self.guild_texts(guild.id)
            await self._update_review_card(guild, definition, updated, tr=tr, text=text)
            await self._dm_applicant(
                guild,
                updated,
                tr=tr,
                key="forms.dm.rejected" if status == REJECTED else "forms.dm.needs_info",
                form=text(definition.title),
                reason=reason,
                link=self._card_link(updated),
            )
            await interaction.edit_original_response(
                embed=ok_embed(
                    tr("forms.review.decided_title"),
                    tr("forms.review.decided_body", status=tr(f"forms.status.{status}")),
                ),
            )
        except UserError as exc:
            await self._respond_error(interaction, exc)
        except Exception:
            LOGGER.exception("处理审核结论失败：submission=%s", submission.submission_id)
            await self._respond_error(interaction, UserError("errors.unexpected"))

    async def _approve(
        self,
        interaction: discord.Interaction,
        *,
        submission: FormSubmission,
        definition: FormDefinition,
        guild: discord.Guild,
        tr: rendering.Translate,
        text: rendering.FormText,
    ) -> None:
        """先做通过后动作，成功了才改状态。失败时保持待审，再点一次就是重试。"""
        binding = await self.store.bindings.get(guild.id, definition.id)
        if definition.grants_role():
            if binding.grant_role_id is None:
                raise UserError("forms.bind.grant_role_missing", form=definition.id)
            role = guild.get_role(binding.grant_role_id)
            if role is None:
                raise UserError("forms.action.role_gone", form=definition.id)
            member = await self._find_member(guild, submission.discord_user_id)
            if member is None:
                raise UserError("forms.action.applicant_gone")
            try:
                await member.add_roles(role, reason=audit_reason(interaction, f"form {definition.id}"))
            except discord.HTTPException as exc:
                raise UserError("forms.action.failed", error=str(exc)) from exc

        if definition.notifies_channel() and binding.notify_channel_id is not None:
            channel = guild.get_channel(binding.notify_channel_id)
            if channel is None:
                raise UserError("forms.action.channel_gone", form=definition.id)
            try:
                await channel.send(
                    embed=ok_embed(
                        tr("forms.notify.approved_title", form=text(definition.title)),
                        tr(
                            "forms.notify.approved_body",
                            user=f"<@{submission.discord_user_id}>",
                            id=submission.submission_id,
                        ),
                    )
                )
            except discord.HTTPException as exc:
                raise UserError("forms.action.failed", error=str(exc)) from exc

        existing = await self.store.submissions.get(submission.submission_id)
        current_reason = existing.decision_reason if existing is not None else None
        await self.store.submissions.set_decision(
            submission.submission_id,
            status=APPROVED,
            reviewer_id=interaction.user.id,
            reason=current_reason,
        )
        updated = await self.store.submissions.get(submission.submission_id)
        assert updated is not None
        await self._update_review_card(guild, definition, updated, tr=tr, text=text)
        await self._dm_applicant(
            guild,
            updated,
            tr=tr,
            key="forms.dm.approved",
            form=text(definition.title),
        )
        await interaction.edit_original_response(
            embed=ok_embed(tr("forms.review.approved_title"), tr("forms.review.approved_body")),
        )

    # ------------------------------------------------------------------ 卡片与通知

    async def _send_review_card(
        self,
        guild: discord.Guild,
        definition: FormDefinition,
        submission: FormSubmission,
        *,
        tr: rendering.Translate,
        text: rendering.FormText,
    ) -> None:
        binding = await self.store.bindings.get(guild.id, definition.id)
        channel = guild.get_channel(binding.review_channel_id) if binding.review_channel_id else None
        if channel is None:
            # 发布时就检查过了，这里只可能是频道后来被删/改权限：记 ERROR，不丢这份提交。
            LOGGER.error(
                "审核频道不可用，提交只落库未送审：form=%s submission=%s channel=%s",
                definition.id,
                submission.submission_id,
                binding.review_channel_id,
            )
            return
        embed = rendering.review_card(
            definition,
            submission,
            text=text,
            tr=tr,
            applicant=f"<@{submission.discord_user_id}>",
        )
        view = FormReviewView(
            self,
            labels={
                APPROVE_ID: tr("forms.button.approve"),
                REJECT_ID: tr("forms.button.reject"),
                NEEDS_INFO_ID: tr("forms.button.needs_info"),
            },
        )
        try:
            message = await channel.send(embed=embed, view=view)
        except discord.HTTPException:
            LOGGER.exception("投递审核卡片失败：submission=%s", submission.submission_id)
            return
        await self.store.submissions.set_review_message(
            submission.submission_id, channel_id=channel.id, message_id=message.id
        )

    async def _update_review_card(
        self,
        guild: discord.Guild,
        definition: FormDefinition,
        submission: FormSubmission,
        *,
        tr: rendering.Translate,
        text: rendering.FormText,
    ) -> None:
        if submission.review_channel_id is None or submission.review_message_id is None:
            return
        channel = guild.get_channel(submission.review_channel_id)
        if channel is None:
            LOGGER.warning("审核卡片所在频道不见了：submission=%s", submission.submission_id)
            return
        try:
            message = await channel.fetch_message(submission.review_message_id)
        except discord.HTTPException:
            LOGGER.warning("找不到审核卡片：submission=%s", submission.submission_id)
            return
        reviewer = f"<@{submission.reviewed_by}>" if submission.reviewed_by else None
        embed = rendering.review_card(
            definition,
            submission,
            text=text,
            tr=tr,
            applicant=f"<@{submission.discord_user_id}>",
            reviewed_by=reviewer,
        )
        keep = submission.status in (PENDING, NEEDS_INFO)
        view = (
            FormReviewView(
                self,
                labels={
                    APPROVE_ID: tr("forms.button.approve"),
                    REJECT_ID: tr("forms.button.reject"),
                    NEEDS_INFO_ID: tr("forms.button.needs_info"),
                },
            )
            if keep
            else None
        )
        try:
            await message.edit(embed=embed, view=view)
        except discord.HTTPException:
            LOGGER.warning("更新审核卡片失败：submission=%s", submission.submission_id)

    async def _update_card(
        self,
        publication: FormPublication,
        *,
        closed: bool,
        guild: discord.Guild | None = None,
    ) -> None:
        """把卡片改成「已关闭」并摘掉按钮。``guild`` 由调用方给（命令那条路手里就有）。"""
        if publication.message_id is None:
            return
        guild = guild or self.bot.get_guild(publication.guild_id)
        if guild is None:
            return
        channel = guild.get_channel(publication.channel_id)
        if channel is None:
            return
        definition = self.catalog.get(publication.form_id)
        if definition is None:
            return
        tr, text = await self.guild_texts(publication.guild_id)
        try:
            message = await channel.fetch_message(publication.message_id)
            await message.edit(
                embed=rendering.open_card(definition, publication, text=text, tr=tr, closed=closed),
                view=None,
            )
        except discord.HTTPException:
            LOGGER.warning("更新表单卡片失败：publication=%s", publication.publication_id)

    async def _notify_new_submission(
        self,
        guild: discord.Guild,
        definition: FormDefinition,
        submission: FormSubmission,
        *,
        tr: rendering.Translate,
        text: rendering.FormText,
        replacing: bool,
    ) -> None:
        binding = await self.store.bindings.get(guild.id, definition.id)
        channel_id = binding.notify_channel_id
        if channel_id is None or (definition.is_apply and channel_id == binding.review_channel_id):
            return
        channel = guild.get_channel(channel_id)
        if channel is None:
            LOGGER.warning("通知频道不可用：form=%s channel=%s", definition.id, channel_id)
            return
        key = "forms.notify.new_resubmission" if replacing else "forms.notify.new_submission"
        try:
            await channel.send(
                embed=info_embed(
                    tr("forms.notify.new_title", form=text(definition.title)),
                    tr(key, user=f"<@{submission.discord_user_id}>", id=submission.submission_id),
                )
            )
        except discord.HTTPException:
            LOGGER.warning("发送新提交通知失败：submission=%s", submission.submission_id)

    async def _dm_applicant(
        self,
        guild: discord.Guild,
        submission: FormSubmission,
        *,
        tr: rendering.Translate,
        key: str,
        **kwargs: Any,
    ) -> None:
        member = await self._find_member(guild, submission.discord_user_id)
        if member is None:
            LOGGER.warning("找不到当事人，私信没发出：user=%s", submission.discord_user_id)
            return
        try:
            await member.send(embed=info_embed(tr("forms.dm.title"), tr(key, **kwargs)))
        except discord.HTTPException:
            # 对方关了私信：这不是故障，申请本身照常成立。
            LOGGER.info("私信没发出去（对方可能关了私信）：user=%s", submission.discord_user_id)

    async def _find_member(self, guild: discord.Guild, user_id: int) -> discord.Member | None:
        member = guild.get_member(user_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(user_id)
        except discord.HTTPException:
            return None

    def _card_link(self, submission: FormSubmission) -> str:
        if submission.review_channel_id is None or submission.review_message_id is None:
            return ""
        return f"https://discord.com/channels/{submission.guild_id}/{submission.review_channel_id}/{submission.review_message_id}"

    # ------------------------------------------------------------------ 附件

    async def _store_attachments(
        self,
        answers: dict[str, Any],
        *,
        step_fields: tuple[Any, ...],
        storage_key: str,
    ) -> dict[str, list[dict[str, Any]]]:
        """把这一步上传的附件立刻下载落盘，返回可以直接存进答案的描述。

        先落盘再落库：Discord 给的下载链接带签名会过期，而答案是长期数据。
        """
        file_fields = [item for item in step_fields if item.kind == schema.FIELD_FILE]
        if not file_fields:
            return {}

        target = self._dir_for(storage_key)
        stored: dict[str, list[dict[str, Any]]] = {}
        total = 0
        for field in file_fields:
            raw = answers.get(field.key) or []
            attachments = [item for item in raw if isinstance(item, discord.Attachment)]
            if not attachments:
                continue
            entries: list[dict[str, Any]] = []
            for attachment in attachments:
                size = int(getattr(attachment, "size", 0) or 0)
                if size > schema.MAX_FILE_BYTES:
                    raise UserError(
                        "forms.attachment.too_big",
                        name=attachment.filename,
                        limit=rendering.format_size(schema.MAX_FILE_BYTES),
                    )
                total += size
                if total > schema.MAX_TOTAL_BYTES:
                    raise UserError(
                        "forms.attachment.total_too_big", limit=rendering.format_size(schema.MAX_TOTAL_BYTES)
                    )
                try:
                    data = await attachment.read()
                except discord.HTTPException as exc:
                    raise UserError("forms.attachment.failed", name=attachment.filename) from exc
                target.mkdir(parents=True, exist_ok=True)
                stored_name = f"{secrets.token_hex(4)}_{safe_filename(attachment.filename)}"
                (target / stored_name).write_bytes(data)
                entries.append(
                    {
                        "name": str(attachment.filename),
                        "stored": stored_name,
                        "size": len(data),
                        "type": str(attachment.content_type or ""),
                    }
                )
            stored[field.key] = entries
        return stored

    @staticmethod
    def _merge(
        old: dict[str, Any],
        submitted: dict[str, Any],
        *,
        step_fields: tuple[Any, ...],
        stored: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        """这一步的答案并入已填内容。

        附件特殊：这一步没重传就保留上一次的结果（弹窗没法预填文件）。多步表单里
        申请人回头补第 2 步时，第 1 步的文件不能因此消失。
        """
        merged = dict(old)
        for field in step_fields:
            if field.kind == schema.FIELD_FILE:
                entries = stored.get(field.key)
                if entries:
                    merged[field.key] = entries
                continue
            merged[field.key] = submitted.get(field.key)
        return merged

    # ------------------------------------------------------------------ 命令

    @form_group.command(
        name="post",
        description=localized("Post a form card so members can fill it in", "commands.form.post.description"),
        extras={"permissions": ("manage_guild",)},
    )
    @app_commands.autocomplete(form=form_autocomplete)
    @app_commands.describe(
        form=localized("Form id", "commands.form.param_form"),
        channel=localized("Where to post it; defaults to the current channel", "commands.form.param_channel"),
        capacity=localized("Signups only: how many people may take part", "commands.form.param_capacity"),
        minutes=localized("Close it automatically after this many minutes", "commands.form.param_minutes"),
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def form_post(
        self,
        interaction: discord.Interaction,
        form: str,
        channel: discord.TextChannel | None = None,
        capacity: app_commands.Range[int, 1, 1000] | None = None,
        minutes: app_commands.Range[int, 1, 20160] | None = None,
    ) -> None:
        definition = self._definition(form)
        guild = self._require_guild(interaction)
        target = channel or interaction.channel
        if not isinstance(target, discord.abc.Messageable):
            raise UserError("forms.bad_channel")
        if capacity is not None and not definition.is_signup:
            raise UserError("forms.capacity_only_signup")
        binding = await self.store.bindings.get(guild.id, definition.id)
        self._require_bindings(definition, binding)

        tr, text = await self.guild_texts(guild.id)
        await interaction.response.defer(ephemeral=True)
        publication = await self.store.publications.create(
            guild.id,
            form_id=definition.id,
            channel_id=target.id,
            capacity=int(capacity) if capacity is not None else None,
            closes_at=_now_plus(int(minutes)) if minutes is not None else None,
            created_by=interaction.user.id,
        )
        label = tr("forms.button.apply") if definition.is_apply else tr("forms.button.open")
        message = await target.send(
            embed=rendering.open_card(definition, publication, text=text, tr=tr),
            view=FormOpenView(self, label=label),
        )
        await self.store.publications.set_message(publication.publication_id, message.id)
        await interaction.edit_original_response(
            embed=ok_embed(
                tr("forms.posted.title"),
                tr(
                    "forms.posted.body",
                    form=text(definition.title),
                    id=publication.publication_id,
                    channel=target.mention,
                ),
            )
        )

    @form_group.command(
        name="close",
        description=localized("Close a posted form now", "commands.form.close.description"),
        extras={"permissions": ("manage_guild",)},
    )
    @app_commands.describe(publication=localized("Publication id", "commands.form.param_publication"))
    @app_commands.checks.has_permissions(manage_guild=True)
    async def form_close(self, interaction: discord.Interaction, publication: int) -> None:
        guild = self._require_guild(interaction)
        found = await self.store.publications.get(publication)
        if found is None or found.guild_id != guild.id:
            raise UserError("forms.not_found")
        if not found.is_open:
            raise UserError("forms.closed_already")
        await interaction.response.defer(ephemeral=True)
        await self.store.publications.set_status(found.publication_id, CLOSED)
        await self._update_card(found, closed=True, guild=guild)
        tr = await self.guild_tr(guild.id)
        await interaction.edit_original_response(
            embed=ok_embed(tr("forms.closed.title"), tr("forms.closed.body", id=found.publication_id)),
        )

    @form_group.command(
        name="bind",
        description=localized(
            "Bind the channels and roles a form needs (omitted items stay unchanged)",
            "commands.form.bind.description",
        ),
        extras={"permissions": ("manage_guild",)},
    )
    @app_commands.autocomplete(form=form_autocomplete)
    @app_commands.describe(
        form=localized("Form id", "commands.form.param_form"),
        review_channel=localized("Where applications are sent for review", "commands.form.param_review_channel"),
        reviewer_role=localized("Members with this role may review", "commands.form.param_reviewer_role"),
        notify_channel=localized("Where new submissions are announced", "commands.form.param_notify_channel"),
        grant_role=localized("Role handed out when an application is approved", "commands.form.param_grant_role"),
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def form_bind(
        self,
        interaction: discord.Interaction,
        form: str,
        review_channel: discord.TextChannel | None = None,
        reviewer_role: discord.Role | None = None,
        notify_channel: discord.TextChannel | None = None,
        grant_role: discord.Role | None = None,
    ) -> None:
        definition = self._definition(form)
        guild = self._require_guild(interaction)
        changes: dict[str, Any] = {}
        if review_channel is not None:
            changes["review_channel_id"] = review_channel.id
        if reviewer_role is not None:
            changes["reviewer_role_id"] = reviewer_role.id
        if notify_channel is not None:
            changes["notify_channel_id"] = notify_channel.id
        if grant_role is not None:
            changes["grant_role_id"] = grant_role.id
        if changes:
            await self.store.bindings.update(guild.id, definition.id, **changes)
        binding = await self.store.bindings.get(guild.id, definition.id)
        tr, text = await self.guild_texts(guild.id)
        await interaction.response.send_message(
            embed=info_embed(
                tr("forms.bound.title", form=text(definition.title)),
                self._binding_text(definition, binding, tr=tr),
            ),
            ephemeral=True,
        )

    @form_group.command(
        name="list",
        description=localized("List every form and how it is set up", "commands.form.list.description"),
        extras={"permissions": ("manage_guild",)},
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def form_list(self, interaction: discord.Interaction) -> None:
        guild = self._require_guild(interaction)
        tr, text = await self.guild_texts(guild.id)
        definitions = self.catalog.all()
        if not definitions:
            raise UserError("forms.list.empty", path=str(self.definitions_dir or "(未配置)"))
        embed = info_embed(tr("forms.list.title"))
        for definition in definitions:
            binding = await self.store.bindings.get(guild.id, definition.id)
            total = await self.store.submissions.count_for_form(guild.id, definition.id)
            pending = await self.store.submissions.count_for_form(guild.id, definition.id, status=PENDING)
            open_publications = await self.store.publications.open_in_guild(guild.id, definition.id)
            embed.add_field(
                name=f"`{definition.id}`　{text(definition.title)}",
                value="\n".join(
                    [
                        text(definition.description),
                        tr(
                            "forms.list.line",
                            mode=tr(f"forms.mode.{definition.mode}"),
                            steps=definition.step_count,
                            fields=len(definition.fields),
                            total=total,
                            pending=pending,
                            open=len(open_publications),
                        ),
                        self._binding_text(definition, binding, tr=tr),
                    ]
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @form_group.command(
        name="results",
        description=localized("List recent submissions of a form", "commands.form.results.description"),
        extras={"permissions": ("manage_guild",)},
    )
    @app_commands.autocomplete(form=form_autocomplete)
    @app_commands.describe(
        form=localized("Form id", "commands.form.param_form"),
        status=localized("Only show this state", "commands.form.param_status"),
        limit=localized("How many to show", "commands.form.param_limit"),
    )
    @app_commands.choices(
        status=[
            app_commands.Choice(name=localized("Pending review", "commands.form.status.pending"), value=PENDING),
            app_commands.Choice(
                name=localized("Waiting for the applicant", "commands.form.status.needs_info"), value=NEEDS_INFO
            ),
            app_commands.Choice(name=localized("Approved", "commands.form.status.approved"), value=APPROVED),
            app_commands.Choice(name=localized("Rejected", "commands.form.status.rejected"), value=REJECTED),
            app_commands.Choice(name=localized("Received", "commands.form.status.received"), value=RECEIVED),
        ]
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def form_results(
        self,
        interaction: discord.Interaction,
        form: str,
        status: app_commands.Choice[str] | None = None,
        limit: app_commands.Range[int, 1, MAX_RESULTS] | None = None,
    ) -> None:
        definition = self._definition(form)
        guild = self._require_guild(interaction)
        tr, text = await self.guild_texts(guild.id)
        count = int(limit) if limit is not None else DEFAULT_RESULTS
        rows = await self.store.submissions.list_for_form(
            guild.id,
            definition.id,
            status=status.value if status is not None else None,
            limit=count,
        )
        total = await self.store.submissions.count_for_form(
            guild.id,
            definition.id,
            status=status.value if status is not None else None,
        )
        embed = info_embed(tr("forms.results.title", form=text(definition.title), total=total))
        if not rows:
            embed.description = tr("forms.results.empty")
        else:
            embed.description = "\n".join(rendering.results_lines(definition, rows, text=text, tr=tr))
            embed.set_footer(text=tr("forms.results.footer", form=definition.id))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @form_group.command(
        name="export",
        description=localized("Export submissions as a CSV file", "commands.form.export.description"),
        extras={"permissions": ("manage_guild",)},
    )
    @app_commands.autocomplete(form=form_autocomplete)
    @app_commands.describe(
        form=localized("Form id", "commands.form.param_form"),
        status=localized("Only export this state", "commands.form.param_status"),
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def form_export(
        self,
        interaction: discord.Interaction,
        form: str,
        status: app_commands.Choice[str] | None = None,
    ) -> None:
        definition = self._definition(form)
        guild = self._require_guild(interaction)
        tr, text = await self.guild_texts(guild.id)
        rows = await self.store.submissions.list_for_form(
            guild.id,
            definition.id,
            status=status.value if status is not None else None,
            limit=EXPORT_LIMIT,
        )
        if not rows:
            raise UserError("forms.export.empty", form=text(definition.title))
        payload = rendering.build_csv(
            definition,
            rows,
            text=text,
            tr=tr,
            name_for=lambda user_id: self._display_name(guild, user_id),
        )
        await interaction.response.send_message(
            embed=info_embed(tr("forms.export.title"), tr("forms.export.body", count=len(rows))),
            file=discord.File(io.BytesIO(payload), filename=f"{definition.id}.csv"),
            ephemeral=True,
        )

    # ------------------------------------------------------------------ 内部工具

    def _display_name(self, guild: discord.Guild, user_id: int) -> str:
        member = guild.get_member(user_id)
        return str(member) if member is not None else str(user_id)

    def _binding_text(
        self,
        definition: FormDefinition,
        binding: FormBinding,
        *,
        tr: rendering.Translate,
    ) -> str:
        def mention(kind: str, value: int | None) -> str:
            if value is None:
                return tr("forms.bind.none")
            return f"<#{value}>" if kind == "channel" else f"<@&{value}>"

        lines = [
            tr(
                "forms.bind.line",
                review=mention("channel", binding.review_channel_id),
                reviewer=mention("role", binding.reviewer_role_id),
                notify=mention("channel", binding.notify_channel_id),
                grant=mention("role", binding.grant_role_id),
            )
        ]
        if definition.actions:
            needed = ", ".join(tr(f"forms.action.{action}") for action in definition.actions)
            lines.append(tr("forms.bind.actions", actions=needed))
        if definition.is_apply and binding.reviewer_role_id is None:
            lines.append(tr("forms.bind.reviewer_missing_warning"))
        return "\n".join(lines)

    def _require_bindings(self, definition: FormDefinition, binding: FormBinding) -> None:
        """发布前就把「发出去也审不了」的配置问题挡住。"""
        if definition.is_apply and binding.review_channel_id is None:
            raise UserError("forms.bind.review_channel_missing", form=definition.id)
        if definition.is_apply and definition.grants_role() and binding.grant_role_id is None:
            raise UserError("forms.bind.grant_role_missing", form=definition.id)

    async def _require_reviewer(
        self,
        interaction: discord.Interaction,
        definition: FormDefinition,
        guild: discord.Guild,
    ) -> None:
        binding = await self.store.bindings.get(guild.id, definition.id)
        if binding.reviewer_role_id is None:
            raise UserError("forms.bind.reviewer_role_missing", form=definition.id)
        if interaction.user.id == getattr(guild, "owner_id", None):
            return
        role_ids = {int(getattr(role, "id", 0)) for role in (getattr(interaction.user, "roles", None) or [])}
        if binding.reviewer_role_id not in role_ids:
            raise UserError("forms.not_reviewer")

    def _definition(self, form_id: str) -> FormDefinition:
        definition = self.catalog.get(str(form_id).strip())
        if definition is None:
            raise UserError("forms.unknown_form", form=str(form_id).strip())
        return definition

    def _log_expired(self, interaction: discord.Interaction, exc: discord.HTTPException, where: str) -> None:
        if getattr(exc, "code", None) == EXPIRED_INTERACTION:
            LOGGER.warning("交互已过期（%s，从交互创建起 %.0f ms）", where, _age_ms(interaction))
        else:
            LOGGER.error("交互响应失败（%s，从交互创建起 %.0f ms）：%s", where, _age_ms(interaction), exc)

    async def _respond_error(self, interaction: discord.Interaction, error: UserError) -> None:
        tr = await self.guild_tr(getattr(getattr(interaction, "guild", None), "id", 0))
        text = tr(error.key, **error.kwargs)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=error_embed(tr("errors.title"), text), ephemeral=True)
            else:
                await interaction.response.send_message(
                    embed=error_embed(tr("errors.title"), text),
                    ephemeral=True,
                )
        except discord.HTTPException as exc:
            # 交互过期时这句提示本来就没地方发；一行日志足够。
            LOGGER.warning("回传表单错误提示失败（%s）：%s", error.key, exc)

    @staticmethod
    def _require_guild(interaction: discord.Interaction) -> discord.Guild:
        guild = getattr(interaction, "guild", None)
        if guild is None:
            raise UserError("errors.guild_only")
        return guild


__all__ = [
    "APPROVE_ID",
    "NEEDS_INFO_ID",
    "OPEN_ID",
    "REJECT_ID",
    "FormOpenView",
    "FormReasonModal",
    "FormResumeView",
    "FormReviewView",
    "FormStepModal",
    "FormsCog",
    "form_autocomplete",
]

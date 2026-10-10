"""表单的界面层：把定义变成弹窗组件、把答案变成卡片与 CSV。

文字来源有两处，各管一边：

* ``text``（由调用方给）解析**表单自己的文案** —— 它写在定义 JSON 里，随表单一起走；
* ``tr`` 取**模块自己的文案** —— 按钮、卡片上的框架句子、错误提示，仍在模块的 ``locales/`` 里。

这里不碰数据库、也不碰机器人，所以离线就能验证「5 个字段怎么切步」「选项怎么翻译」
「长文怎么截断」这些容易出错的地方。
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable
from typing import Any

import discord

from bot.core.ui import COLOR_INFO, COLOR_OK, COLOR_WARN

from . import schema
from .schema import Field, FormDefinition, Localized
from .store import APPROVED, FormPublication, FormSubmission

# 文案查询：``tr(key, **kwargs)`` -> 模块自己的文案。
Translate = Callable[..., str]
# 表单文案查询：``text({"en-US": …, "zh-CN": …})`` -> 当前语言的那一句。
FormText = Callable[[Localized], str]

FIELD_ID_PREFIX = "forms:field:"

# 一条 embed 的字段值上限是 1024、整条 6000 字符。申请里的长文随便就超过，
# 所以卡片只放摘要，完整内容走 `/form export` 导出。
ANSWER_LIMIT = 700
FIELD_VALUE_LIMIT = 1024
EMBED_TOTAL_LIMIT = 5800


def field_id(key: str) -> str:
    return f"{FIELD_ID_PREFIX}{key}"


def is_blank(value: Any) -> bool:
    """空答案：``None``、空列表，或只有空白字符的文本。

    可选字段常常被交上来一个空格，那不该在卡片上占一行。
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set)):
        return not list(value)
    return False


def build_step_items(
    definition: FormDefinition,
    step_index: int,
    *,
    text: FormText,
    tr: Translate,
    prefill: dict[str, Any] | None = None,
) -> list[discord.ui.Label]:
    """把某一步的字段变成 ``Label`` 列表（一个弹窗最多 5 个）。"""
    steps = definition.steps
    if not 0 <= step_index < len(steps):
        raise ValueError(f"{definition.id}：没有第 {step_index} 步")
    stored = prefill or {}
    return [
        discord.ui.Label(
            text=clip(text(field.label), schema.LABEL_LIMIT),
            description=(
                clip(text(field.description), schema.DESCRIPTION_LIMIT) if field.description is not None else None
            ),
            component=_build_component(field, text=text, tr=tr, prefill=stored.get(field.key)),
        )
        for field in steps[step_index]
    ]


def _build_component(field: Field, *, text: FormText, tr: Translate, prefill: Any) -> discord.ui.Item:
    if field.kind == schema.FIELD_SHORT:
        return discord.ui.TextInput(
            custom_id=field_id(field.key),
            style=discord.TextStyle.short,
            required=field.required,
            max_length=schema.SHORT_TEXT_MAX,
            default=str(prefill) if isinstance(prefill, str) and prefill else None,
        )
    if field.kind == schema.FIELD_LONG:
        return discord.ui.TextInput(
            custom_id=field_id(field.key),
            style=discord.TextStyle.paragraph,
            required=field.required,
            max_length=schema.LONG_TEXT_MAX,
            default=str(prefill) if isinstance(prefill, str) and prefill else None,
        )
    if field.kind in (schema.FIELD_CHOICE, schema.FIELD_MULTI):
        chosen = as_list(prefill)
        options = [
            discord.SelectOption(
                label=clip(text(option.label), schema.OPTION_LIMIT),
                value=option.value,
                default=option.value in chosen,
            )
            for option in field.options
        ]
        single = field.kind == schema.FIELD_CHOICE
        return discord.ui.Select(
            custom_id=field_id(field.key),
            required=field.required,
            min_values=0 if not field.required else 1,
            max_values=1 if single else len(options),
            options=options,
        )
    if field.kind == schema.FIELD_FILE:
        return discord.ui.FileUpload(
            custom_id=field_id(field.key),
            required=field.required,
            min_values=1 if field.required else 0,
            max_values=field.max_files,
        )
    raise ValueError(f"{field.key}：未知字段类型 {field.kind!r}")


def collect_answers(modal: discord.ui.Modal) -> dict[str, Any]:
    """把弹窗里填好的值读出来，键是字段 key。

    附件读出来的是 :class:`discord.Attachment`（还没下载）——调用方必须**立刻**落盘：
    Discord 给的下载链接带签名，会过期。
    """
    answers: dict[str, Any] = {}
    for item in modal.walk_children():
        custom_id = getattr(item, "custom_id", None)
        if not isinstance(custom_id, str) or not custom_id.startswith(FIELD_ID_PREFIX):
            continue
        key = custom_id[len(FIELD_ID_PREFIX) :]
        if isinstance(item, discord.ui.FileUpload):
            answers[key] = list(item.values)
        elif isinstance(item, discord.ui.Select):
            answers[key] = list(item.values)
        elif isinstance(item, discord.ui.TextInput):
            answers[key] = str(item.value or "")
    return answers


# ---------------------------------------------------------------------- 展示


def format_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MiB"
    if size >= 1024:
        return f"{size / 1024:.1f} KiB"
    return f"{size} B"


def format_value(field: Field, value: Any, *, text: FormText, tr: Translate) -> str:
    """把一个字段的答案变成给人看的一行。"""
    if field.kind in (schema.FIELD_CHOICE, schema.FIELD_MULTI):
        chosen = as_list(value)
        if not chosen:
            return tr("forms.answers.empty")
        # 定义改过之后，旧提交里可能出现已经不存在的选项值：那就原样显示，别吞掉。
        labels = []
        for item in chosen:
            option = field.option(item)
            labels.append(text(option.label) if option is not None else item)
        return "、".join(labels)
    if field.kind == schema.FIELD_FILE:
        entries = as_attachments(value)
        if not entries:
            return tr("forms.answers.empty")
        names = "、".join(f"{item.get('name', '?')}（{format_size(int(item.get('size', 0)))}）" for item in entries)
        return tr("forms.answers.attachments", count=len(entries), names=names)
    return str(value or "").strip() or tr("forms.answers.empty")


def answer_fields(
    definition: FormDefinition,
    answers: dict[str, Any],
    *,
    text: FormText,
    tr: Translate,
    skipped: tuple[str, ...] = (),
) -> list[tuple[str, str]]:
    """``(字段名, 值)`` 列表；没填的字段不列出。"""
    pairs: list[tuple[str, str]] = []
    for field in definition.fields:
        if field.key in skipped:
            continue
        raw = answers.get(field.key)
        if is_blank(raw):
            continue
        pairs.append((text(field.label), clip(format_value(field, raw, text=text, tr=tr), ANSWER_LIMIT)))
    return pairs


def open_card(
    definition: FormDefinition,
    publication: FormPublication,
    *,
    text: FormText,
    tr: Translate,
    closed: bool = False,
) -> discord.Embed:
    lines = [text(definition.description)]
    if definition.step_count > 1:
        lines.append(tr("forms.card.open.steps", steps=definition.step_count))
    if publication.capacity is not None:
        lines.append(tr("forms.card.open.capacity", remaining=publication.remaining, capacity=publication.capacity))
    if publication.closes_at:
        lines.append(tr("forms.card.open.deadline", when=publication.closes_at))
    if closed:
        lines.append(tr("forms.card.open.closed"))
    elif publication.is_full:
        lines.append(tr("forms.card.open.full"))

    return discord.Embed(
        title=clip(text(definition.title), schema.LABEL_LIMIT),
        description="\n".join(lines),
        color=COLOR_WARN if closed else COLOR_INFO,
    )


def review_card(
    definition: FormDefinition,
    submission: FormSubmission,
    *,
    text: FormText,
    tr: Translate,
    applicant: str,
    reviewed_by: str | None = None,
) -> discord.Embed:
    header = [
        tr("forms.review.applicant", user=applicant),
        tr("forms.review.submitted_at", when=submission.submitted_at),
        tr("forms.review.status", status=tr(f"forms.status.{submission.status}")),
    ]
    if submission.decision_reason:
        header.append(tr("forms.review.reason", reason=clip(submission.decision_reason, ANSWER_LIMIT)))
    if reviewed_by:
        header.append(tr("forms.review.reviewed_by", user=reviewed_by))

    embed = discord.Embed(
        title=tr("forms.review.title", form=text(definition.title)),
        description="\n".join(header),
        color=COLOR_OK if submission.status == APPROVED else COLOR_INFO,
    )

    budget = EMBED_TOTAL_LIMIT - len(embed.description or "") - len(embed.title or "")
    truncated = False
    for name, value in answer_fields(definition, submission.answers, text=text, tr=tr):
        if budget - len(name) - len(value) <= 0:
            truncated = True
            break
        embed.add_field(name=clip(name, 256), value=clip(value, FIELD_VALUE_LIMIT), inline=False)
        budget -= len(name) + len(value)
    if truncated:
        embed.set_footer(text=tr("forms.review.truncated", form=definition.id))
    return embed


def results_lines(
    definition: FormDefinition,
    submissions: list[FormSubmission],
    *,
    text: FormText,
    tr: Translate,
) -> list[str]:
    lines: list[str] = []
    for submission in submissions:
        preview = next(
            (
                format_value(field, submission.answers.get(field.key), text=text, tr=tr)
                for field in definition.fields
                if not is_blank(submission.answers.get(field.key))
            ),
            tr("forms.answers.empty"),
        )
        lines.append(
            tr(
                "forms.results.entry",
                id=submission.submission_id,
                status=tr(f"forms.status.{submission.status}"),
                user=f"<@{submission.discord_user_id}>",
                when=submission.submitted_at,
                preview=clip(preview, 120),
            )
        )
    return lines


def build_csv(
    definition: FormDefinition,
    submissions: list[FormSubmission],
    *,
    text: FormText,
    tr: Translate,
    name_for: Callable[[int], str],
) -> bytes:
    """导出成 Excel 打得开的 CSV。

    带 UTF-8 BOM：不带的话 Excel（尤其中文 Windows）会按本地编码读，中文全变乱码。
    这是给用户下载的文件、不是仓库里的文本文件，所以这里带 BOM 是对的。
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            tr("forms.csv.id"),
            tr("forms.csv.status"),
            tr("forms.csv.user"),
            tr("forms.csv.user_id"),
            tr("forms.csv.submitted_at"),
            tr("forms.csv.reviewed_by"),
            tr("forms.csv.reviewed_at"),
            tr("forms.csv.reason"),
            *[text(field.label) for field in definition.fields],
        ]
    )
    for submission in submissions:
        writer.writerow(
            [
                submission.submission_id,
                tr(f"forms.status.{submission.status}"),
                name_for(submission.discord_user_id),
                submission.discord_user_id,
                submission.submitted_at,
                name_for(submission.reviewed_by) if submission.reviewed_by else "",
                submission.reviewed_at or "",
                submission.decision_reason or "",
                *[
                    ""
                    if is_blank(submission.answers.get(field.key))
                    else format_value(field, submission.answers.get(field.key), text=text, tr=tr)
                    for field in definition.fields
                ],
            ]
        )
    return buffer.getvalue().encode("utf-8-sig")


# ---------------------------------------------------------------------- 小工具


def clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    return [str(value)]


def as_attachments(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, dict)]

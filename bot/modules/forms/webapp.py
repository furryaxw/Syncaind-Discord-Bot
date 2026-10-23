"""HTML 表单的 HTTP 入口：GET 渲染、POST 落库。

只处理 `/f?token=<hex>`；审核页在下一阶段加 `/review`。
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path, PurePosixPath
from typing import Any

from aiohttp import web

from bot.core.clock import parse_iso, utcnow
from bot.core.web import BotWebServer
from bot.modules.forms import html as form_html
from bot.modules.forms import store as form_store
from bot.modules.forms.html import (
    UploadedFile,
    load_custom_template,
    parse_answers,
    render_stylesheet,
    validate_custom_template,
)
from bot.modules.forms.schema import FormCatalog, FormDefinition, pick
from bot.modules.forms.token_store import FormTokenStore
from bot.modules.forms.tokens import TokenError, csrf_token, decode_token, verify_csrf

LOGGER = logging.getLogger("bot.forms")
FORM_PATH = "/f"
CSS_PATH = "/form.css"
MAX_FILE_NAME = 120


class FormWebApp:
    def __init__(
        self,
        *,
        bot: Any,
        catalog: FormCatalog,
        stores: form_store.FormStores,
        attachments_root: Path,
        definitions_dir: Path,
        token_store: FormTokenStore,
        secret: str,
        base_url: str,
    ) -> None:
        self.bot = bot
        self.catalog = catalog
        self.stores = stores
        self.attachments_root = attachments_root
        self.definitions_dir = definitions_dir
        self.token_store = token_store
        self.secret = secret
        self.base_url = base_url.rstrip("/")

    def register(self, server: BotWebServer) -> None:
        server.add_route("GET", FORM_PATH, self.handle_get)
        server.add_route("POST", FORM_PATH, self.handle_post)
        server.add_route("GET", CSS_PATH, self.handle_css)

    async def handle_css(self, _request: web.Request) -> web.StreamResponse:
        try:
            css = render_stylesheet()
        except OSError:
            return web.Response(status=404, text="not found")
        return web.Response(
            text=css,
            content_type="text/css",
            headers={"Cache-Control": "public, max-age=300"},
        )

    # ------------------------------------------------------------------ GET

    async def handle_get(self, request: web.Request) -> web.StreamResponse:
        context = await self._context(request)
        if isinstance(context, web.Response):
            return context
        claims, definition, _publication, _guild, _member = context
        await self.token_store.remember(claims, kind="form")
        csrf = csrf_token(self.secret, str(claims["jti"]))
        locale = str(claims.get("loc") or "en-US")
        custom = load_custom_template(self.definitions_dir, definition.id)
        if custom is not None:
            missing = validate_custom_template(custom)
            if missing:
                LOGGER.error(
                    "自定义表单模板缺少占位符 %s，回退内置模板：form=%s", missing, definition.id
                )
                custom = None
        page = form_html.render_form(
            definition,
            action=f"{FORM_PATH}?token={request.query.get('token', '')}",
            csrf_field=f'<input type="hidden" name="csrf" value="{csrf}">',
            locale=locale,
            submit_label=self.bot.i18n.t(locale, "forms.html.submit"),
            custom_template=custom,
        )
        return web.Response(text=page, content_type="text/html", headers=_headers())

    # ------------------------------------------------------------------ POST

    async def handle_post(self, request: web.Request) -> web.StreamResponse:
        context = await self._context(request)
        if isinstance(context, web.Response):
            return context
        claims, definition, publication, guild, member = context

        try:
            post = await request.post()
        except Exception as exc:  # 请求体过大/格式坏
            return _page("提交失败", f"无法读取表单：{exc}", status=400)

        if not verify_csrf(self.secret, str(claims["jti"]), str(post.get("csrf", ""))):
            return _page("提交失败", "CSRF 校验失败，请回到 Discord 重新打开表单。", status=400)
        if await self.token_store.is_used(str(claims["jti"])):
            return _page("链接已使用", "这张表单已经提交过了。", status=400)

        text: dict[str, str] = {}
        multi: dict[str, list[str]] = {}
        files: dict[str, list[UploadedFile]] = {}
        for field in definition.fields:
            values = post.getall(field.key, [])
            uploads = [value for value in values if isinstance(value, web.FileField)]
            if uploads:
                files[field.key] = [
                    UploadedFile(
                        name=PurePosixPath(upload.filename or "file").name,
                        data=upload.file.read(),
                        content_type=upload.content_type or "application/octet-stream",
                    )
                    for upload in uploads
                ]
                continue
            plain = [str(value) for value in values if not isinstance(value, web.FileField)]
            if field.kind in {"multi"}:
                multi[field.key] = plain
            else:
                text[field.key] = plain[0] if plain else ""

        parsed = parse_answers(definition, text=text, multi=multi, files=files)
        if not parsed.ok:
            page = form_html.render_form(
                definition,
                action=f"{FORM_PATH}?token={request.query.get('token', '')}",
                csrf_field=f'<input type="hidden" name="csrf" value="{csrf_token(self.secret, str(claims["jti"]))}">',
                locale=str(claims.get("loc") or "en-US"),
                values=text,
                errors=parsed.errors,
            )
            return web.Response(
                text=page, content_type="text/html", status=400, headers=_headers()
            )

        existing = await self.stores.submissions.latest_for_user(
            guild.id, definition.id, member.id
        )
        replacing = bool(existing is not None and definition.is_apply and existing.status == form_store.NEEDS_INFO)
        if existing is not None and not replacing:
            if existing.status == form_store.PENDING:
                return _page("重复提交", "你已经在等审核了。", status=400)
            if existing.status == form_store.APPROVED:
                return _page("重复提交", "你的申请已经通过了。", status=400)
            if existing.status == form_store.RECEIVED and not definition.allow_multiple:
                return _page("重复提交", "你已经提交过了。", status=400)

        if definition.is_signup and publication.capacity is not None:
            if not await self.stores.publications.take_slot(publication.publication_id):
                return _page("名额已满", "这张表单已经满了。", status=400)

        attachment_dir = existing.attachment_dir if replacing and existing is not None else form_store.new_storage_key()
        directory = self.attachments_root / attachment_dir
        try:
            directory.mkdir(parents=True, exist_ok=True)
            answers = dict(parsed.answers)
            for field_key, uploads in parsed.files.items():
                entries = []
                for index, upload in enumerate(uploads):
                    stored = f"{index}-{secrets.token_hex(4)}-{_safe_name(upload.name)}"
                    (directory / stored).write_bytes(upload.data)
                    entries.append(
                        {
                            "name": upload.name,
                            "size": len(upload.data),
                            "type": upload.content_type,
                            "stored": stored,
                        }
                    )
                answers[field_key] = entries

            status = form_store.PENDING if definition.is_apply else form_store.RECEIVED
            if replacing and existing is not None:
                await self.stores.submissions.replace_answers(
                    existing.submission_id, answers=answers, status=form_store.PENDING
                )
                submission = await self.stores.submissions.get(existing.submission_id)
            else:
                submission = await self.stores.submissions.create(
                    guild.id,
                    form_id=definition.id,
                    publication_id=publication.publication_id,
                    discord_user_id=member.id,
                    answers=answers,
                    status=status,
                    attachment_dir=attachment_dir,
                )
        except Exception:
            LOGGER.exception("HTML 表单落库失败：form=%s user=%s", definition.id, member.id)
            if definition.is_signup and publication.capacity is not None:
                await self.stores.publications.give_back_slot(publication.publication_id)
            return _page("提交失败", "服务器暂时没能保存这份提交，请稍后重试。", status=500)

        await self.token_store.consume(str(claims["jti"]))
        warning = await self._after_submit(guild, definition, submission, replacing=replacing)
        message = f"「{pick(definition.title, str(claims.get('loc') or 'en-US'))}」已收到，可以回到 Discord 了。"
        if warning:
            message += "\n\n" + warning
        return _page("已提交", message)

    # ------------------------------------------------------------------ 内部

    async def _context(self, request: web.Request) -> tuple | web.Response:
        if self.bot.settings.form_token_secret is None:
            return _page("未配置", "表单 token 密钥未配置。", status=503)
        token = request.query.get("token", "")
        try:
            claims = decode_token(self.secret, token)
        except TokenError as exc:
            return _page("链接无效", str(exc), status=400)
        if claims.get("typ") != "form":
            return _page("链接无效", "这不是表单链接。", status=400)
        if int(claims.get("gid", 0)) != int(self.bot.settings.guild_id):
            return _page("链接无效", "链接不属于这台服务器。", status=400)
        definition = self.catalog.get(str(claims.get("fid", "")))
        if definition is None:
            return _page("表单不存在", "这个表单已经被删除或重命名。", status=404)
        publication = await self.stores.publications.get(int(claims.get("pid", 0)))
        if publication is None or publication.form_id != definition.id:
            return _page("发布不存在", "这次发布已经不存在了。", status=404)
        if not publication.is_open:
            return _page("已关闭", "这次填写已经关闭了。", status=400)
        if publication.is_full:
            return _page("名额已满", "这次填写已经满了。", status=400)
        closes = parse_iso(publication.closes_at)
        if closes is not None and closes <= utcnow():
            return _page("已关闭", "这次填写已经截止了。", status=400)
        guild = self.bot.get_guild(int(claims["gid"]))
        if guild is None:
            return _page("服务器不可用", "机器人不在这个服务器里。", status=503)
        member = guild.get_member(int(claims["sub"]))
        if member is None:
            return _page("成员不存在", "你不在这个服务器里，无法填写。", status=403)
        if definition.grant_key > 0:
            linker = self.bot.services.get("form_keys")
            binding = await self.stores.bindings.get(guild.id, definition.id)
            if linker is None or binding.drop_id is None:
                return _page("未配置", "这个表单还没有绑定可用的 key 活动。", status=503)
            if await linker.available(guild.id, binding) < definition.grant_key:
                return _page("已暂停", "key 库存不足，表单暂时停止收集。", status=400)
        return claims, definition, publication, guild, member

    async def _after_submit(
        self,
        guild: Any,
        definition: FormDefinition,
        submission: Any,
        *,
        replacing: bool,
    ) -> str | None:
        warning: str | None = None
        if definition.grant_key > 0 and not definition.is_apply:
            linker = self.bot.services.get("form_keys")
            binding = await self.stores.bindings.get(guild.id, definition.id)
            if linker is None or binding.drop_id is None:
                warning = "key 联动不可用，提交已收到但未发放 key。"
            else:
                try:
                    await linker.issue(guild, definition, binding, submission)
                except Exception as exc:
                    LOGGER.exception("HTML 表单发 key 失败：submission=%s", submission.submission_id)
                    warning = f"提交已收到，但 key 未发放：{exc}"
        cog = self.bot.get_cog("FormsCog")
        if cog is None:
            LOGGER.warning("FormsCog 不存在，HTML 提交只落库未通知")
            return warning
        try:
            tr, text = await cog.guild_texts(guild.id)
            if definition.is_apply:
                if replacing and submission.review_message_id is not None:
                    await cog._update_review_card(guild, definition, submission, tr=tr, text=text)
                else:
                    await cog._send_review_card(guild, definition, submission, tr=tr, text=text)
            await cog._notify_new_submission(
                guild, definition, submission, tr=tr, text=text, replacing=replacing
            )
            await cog._dm_applicant(
                guild,
                submission,
                tr=tr,
                key="forms.dm.received",
                form=text(definition.title),
            )
        except Exception:
            LOGGER.exception("HTML 提交后的通知失败：submission=%s", submission.submission_id)
        return warning


def _safe_name(name: str) -> str:
    safe = Path(name).name.replace("\\", "_").replace("/", "_")
    return (safe or "file")[:MAX_FILE_NAME]


def _headers() -> dict[str, str]:
    return {
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'",
    }


def _page(title: str, message: str, *, status: int = 200) -> web.Response:
    return web.Response(
        text=form_html.render_message(title=title, message=message),
        content_type="text/html",
        status=status,
        headers=_headers(),
    )

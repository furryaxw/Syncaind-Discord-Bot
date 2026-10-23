"""HTML 审核页：待审队列、单条详情、通过/驳回/要求补充与附件下载。

审核权限与 Discord 卡片一致：绑定的审核员角色，或服务器拥有者。
"""

from __future__ import annotations

import logging
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote

from aiohttp import web

from bot.core.web import BotWebServer
from bot.modules.forms import html as form_html
from bot.modules.forms import store as form_store
from bot.modules.forms.schema import FormCatalog, FormDefinition, pick
from bot.modules.forms.token_store import FormTokenStore
from bot.modules.forms.tokens import TokenError, csrf_token, decode_token, verify_csrf

LOGGER = logging.getLogger("bot.forms")
REVIEW_PATH = "/review"
FILE_PATH = "/review/file"


class FormReviewApp:
    def __init__(
        self,
        *,
        bot: Any,
        catalog: FormCatalog,
        stores: form_store.FormStores,
        attachments_root: Path,
        token_store: FormTokenStore,
        secret: str,
    ) -> None:
        self.bot = bot
        self.catalog = catalog
        self.stores = stores
        self.attachments_root = attachments_root
        self.token_store = token_store
        self.secret = secret

    def register(self, server: BotWebServer) -> None:
        server.add_route("GET", REVIEW_PATH, self.handle_get)
        server.add_route("POST", REVIEW_PATH, self.handle_post)
        server.add_route("GET", FILE_PATH, self.handle_file)

    # ------------------------------------------------------------------ GET

    async def handle_get(self, request: web.Request) -> web.StreamResponse:
        context = await self._context(request)
        if isinstance(context, web.Response):
            return context
        claims, guild, member = context
        submission_id = request.query.get("submission")
        if submission_id:
            return await self._detail(request, claims, guild, member, int(submission_id))
        return await self._queue(request, claims, guild, member)

    async def _queue(
        self, request: web.Request, claims: dict, guild: Any, member: Any
    ) -> web.StreamResponse:
        rows: list[str] = []
        for definition in self.catalog.all():
            if not await self._can_review(guild, member, definition.id):
                continue
            submissions = await self.stores.submissions.list_for_form(
                guild.id, definition.id, status=form_store.PENDING, limit=50
            )
            submissions += await self.stores.submissions.list_for_form(
                guild.id, definition.id, status=form_store.NEEDS_INFO, limit=50
            )
            for submission in submissions:
                link = (
                    f"{REVIEW_PATH}?token={escape(request.query.get('token', ''))}"
                    f"&submission={submission.submission_id}"
                )
                rows.append(
                    f'<li><a href="{link}">#{submission.submission_id}</a> '
                    f"{escape(pick(definition.title, self._locale(claims)))} · "
                    f"<span class=\"hint\">{escape(submission.status)}</span></li>"
                )
        content = "<h1>待审队列</h1>"
        content += "<ul>" + "".join(rows) + "</ul>" if rows else "<p>没有待审提交。</p>"
        return self._render(request, "待审队列", content, claims)

    async def _detail(
        self,
        request: web.Request,
        claims: dict,
        guild: Any,
        member: Any,
        submission_id: int,
    ) -> web.StreamResponse:
        submission = await self.stores.submissions.get(submission_id)
        if submission is None or submission.guild_id != guild.id:
            return self._message("找不到提交", "这条提交不存在。", status=404)
        definition = self.catalog.get(submission.form_id)
        if definition is None:
            return self._message("表单不存在", "这个表单已被删除。", status=404)
        if not await self._can_review(guild, member, definition.id):
            return self._message("没有权限", "你不负责审核这个表单。", status=403)

        locale = self._locale(claims)
        answers = "".join(
            f"<tr><th>{escape(pick(field.label, locale))}</th><td>{self._answer(field, submission, request, claims)}</td></tr>"
            for field in definition.fields
        )
        token = escape(request.query.get("token", ""))
        csrf = csrf_token(self.secret, str(claims["jti"]))
        content = [
            f"<h1>#{submission.submission_id} · {escape(pick(definition.title, locale))}</h1>",
            f"<p class=\"hint\">提交人：<@{submission.discord_user_id}> · 状态：{escape(submission.status)}</p>",
            f"<table>{answers}</table>",
            f'<form method="post" action="{REVIEW_PATH}?token={token}">',
            f'<input type="hidden" name="csrf" value="{csrf}">',
            f'<input type="hidden" name="submission" value="{submission.submission_id}">',
            '<label>驳回 / 要求补充的理由</label>',
            '<textarea name="reason" maxlength="500"></textarea>',
            '<button type="submit" name="decision" value="approve">通过</button>',
            '<button type="submit" name="decision" value="reject">驳回</button>',
            '<button type="submit" name="decision" value="needs_info">让他补充</button>',
            "</form>",
            f'<p><a href="{REVIEW_PATH}?token={token}">← 返回队列</a></p>',
        ]
        return self._render(request, f"审核 #{submission.submission_id}", "\n".join(content), claims)

    # ------------------------------------------------------------------ POST

    async def handle_post(self, request: web.Request) -> web.StreamResponse:
        context = await self._context(request)
        if isinstance(context, web.Response):
            return context
        claims, guild, member = context
        post = await request.post()
        if not verify_csrf(self.secret, str(claims["jti"]), str(post.get("csrf", ""))):
            return self._message("审核失败", "CSRF 校验失败，请重新打开审核页。", status=400)
        submission_id = int(str(post.get("submission", "0")))
        submission = await self.stores.submissions.get(submission_id)
        if submission is None or submission.guild_id != guild.id:
            return self._message("审核失败", "找不到这条提交。", status=404)
        definition = self.catalog.get(submission.form_id)
        if definition is None or not await self._can_review(guild, member, definition.id):
            return self._message("审核失败", "你没有权限审核这个表单。", status=403)
        if submission.status not in (form_store.PENDING, form_store.NEEDS_INFO):
            return self._message("审核失败", "这条提交已经处理过了。", status=400)

        decision = str(post.get("decision", ""))
        reason = str(post.get("reason", "")).strip()
        if decision == "approve":
            error = await self._approve(guild, member, definition, submission)
            if error:
                return self._message("通过失败", error, status=400)
            return self._message("已通过", f"提交 #{submission.submission_id} 已通过。")
        if decision not in {"reject", "needs_info"}:
            return self._message("审核失败", "未知操作。", status=400)
        if not reason:
            return self._message("审核失败", "驳回/要求补充必须写理由。", status=400)
        status = form_store.REJECTED if decision == "reject" else form_store.NEEDS_INFO
        await self.stores.submissions.set_decision(
            submission.submission_id, status=status, reviewer_id=member.id, reason=reason
        )
        await self._after_decision(guild, definition, submission, status, reason)
        return self._message(
            "已处理",
            f"提交 #{submission.submission_id} 已标记为 {status}。",
        )

    # ------------------------------------------------------------------ 附件

    async def handle_file(self, request: web.Request) -> web.StreamResponse:
        context = await self._context(request)
        if isinstance(context, web.Response):
            return context
        _claims, guild, member = context
        submission_id = int(request.query.get("submission", "0"))
        submission = await self.stores.submissions.get(submission_id)
        if submission is None or submission.guild_id != guild.id:
            return self._message("找不到文件", "提交不存在。", status=404)
        definition = self.catalog.get(submission.form_id)
        if definition is None or not await self._can_review(guild, member, definition.id):
            return self._message("没有权限", "你不负责审核这个表单。", status=403)
        name = request.query.get("name", "")
        if not self._is_attachment(submission, name):
            return self._message("找不到文件", "文件不存在。", status=404)
        path = self.attachments_root / submission.attachment_dir / name
        if not path.is_file():
            return self._message("找不到文件", "文件已被清理。", status=404)
        return web.FileResponse(
            path,
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
                "X-Content-Type-Options": "nosniff",
            },
        )

    # ------------------------------------------------------------------ 内部

    async def _context(self, request: web.Request) -> tuple | web.Response:
        try:
            claims = decode_token(self.secret, request.query.get("token", ""))
        except TokenError as exc:
            return self._message("链接无效", str(exc), status=400)
        if claims.get("typ") != "review":
            return self._message("链接无效", "这不是审核链接。", status=400)
        guild = self.bot.get_guild(int(claims["gid"]))
        if guild is None:
            return self._message("服务器不可用", "机器人不在这个服务器里。", status=503)
        member = guild.get_member(int(claims["sub"]))
        if member is None:
            return self._message("成员不存在", "你不在这个服务器里。", status=403)
        return claims, guild, member

    async def _can_review(self, guild: Any, member: Any, form_id: str) -> bool:
        if member.id == guild.owner_id:
            return True
        binding = await self.stores.bindings.get(guild.id, form_id)
        return binding.reviewer_role_id is not None and any(
            role.id == binding.reviewer_role_id for role in member.roles
        )

    async def _approve(
        self,
        guild: Any,
        member: Any,
        definition: FormDefinition,
        submission: Any,
    ) -> str | None:
        binding = await self.stores.bindings.get(guild.id, definition.id)
        applicant = guild.get_member(submission.discord_user_id)
        if definition.grants_role():
            if binding.grant_role_id is None:
                return "这个表单没有绑定发放角色。"
            role = guild.get_role(binding.grant_role_id)
            if role is None or applicant is None:
                return "发放角色或申请人已不存在。"
            try:
                await applicant.add_roles(role, reason=f"form {definition.id} approved by {member.id}")
            except Exception as exc:
                return f"发角色失败：{exc}"
        if definition.grant_key > 0:
            linker = self.bot.services.get("form_keys")
            if linker is None or binding.drop_id is None:
                return "key 联动不可用或未绑定活动。"
            try:
                await linker.issue(guild, definition, binding, submission)
            except Exception as exc:
                return f"发 key 失败：{exc}"
        await self.stores.submissions.set_decision(
            submission.submission_id, status=form_store.APPROVED, reviewer_id=member.id, reason=None
        )
        updated = await self.stores.submissions.get(submission.submission_id)
        if updated is not None:
            await self._after_decision(guild, definition, updated, form_store.APPROVED, None)
        return None

    async def _after_decision(
        self,
        guild: Any,
        definition: FormDefinition,
        submission: Any,
        status: str,
        reason: str | None,
    ) -> None:
        cog = self.bot.get_cog("FormsCog")
        if cog is None:
            return
        try:
            tr, text = await cog.guild_texts(guild.id)
            updated = await self.stores.submissions.get(submission.submission_id)
            if updated is None:
                return
            await cog._update_review_card(guild, definition, updated, tr=tr, text=text)
            key = {
                form_store.APPROVED: "forms.dm.approved",
                form_store.REJECTED: "forms.dm.rejected",
                form_store.NEEDS_INFO: "forms.dm.needs_info",
            }.get(status)
            if key is not None:
                await cog._dm_applicant(
                    guild,
                    updated,
                    tr=tr,
                    key=key,
                    form=text(definition.title),
                    reason=reason,
                    link=cog._card_link(updated),
                )
        except Exception:
            LOGGER.exception("HTML 审核后的通知失败：submission=%s", submission.submission_id)

    def _is_attachment(self, submission: Any, name: str) -> bool:
        if not name or "/" in name or "\\" in name:
            return False
        for value in submission.answers.values():
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict) and item.get("stored") == name:
                        return True
        return False

    def _answer(self, field: Any, submission: Any, request: web.Request, claims: dict) -> str:
        value = submission.answers.get(field.key)
        if field.kind == "file" and isinstance(value, list):
            links = []
            for item in value:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("stored", ""))
                label = escape(str(item.get("name", name)))
                href = (
                    f"{FILE_PATH}?token={escape(request.query.get('token', ''))}"
                    f"&submission={submission.submission_id}&name={quote(name)}"
                )
                links.append(f'<a href="{href}">{label}</a>')
            return "<br>".join(links) or "—"
        if isinstance(value, list):
            return escape("、".join(str(item) for item in value))
        return escape(str(value or "—"))

    def _render(self, request: web.Request, title: str, content: str, claims: dict) -> web.Response:
        page = form_html.apply_template(
            form_html.load_template("review.html"),
            {
                "lang": escape(self._locale(claims)),
                "title": escape(title),
                "css_url": form_html.DEFAULT_CSS_URL,
                "content": content,
            },
        )
        return web.Response(text=page, content_type="text/html", headers=_headers())

    def _message(self, title: str, message: str, *, status: int = 200) -> web.Response:
        return web.Response(
            text=form_html.render_message(title=title, message=message),
            content_type="text/html",
            status=status,
            headers=_headers(),
        )

    @staticmethod
    def _locale(claims: dict) -> str:
        return str(claims.get("loc") or "en-US")


def _headers() -> dict[str, str]:
    return {
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'",
    }

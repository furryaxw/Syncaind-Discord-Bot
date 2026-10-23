"""`/form` 面板：表单列表、发布、绑定、结果、导出与成员视图。

旧子命令仍保留为内部方法（供 web 审核、key 联动与测试使用），命令树里只保留 `/form`。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot.core.clock import utcnow_iso
from bot.core.errors import UserError
from bot.core.translator import localized
from bot.core.ui import error_embed, info_embed, ok_embed, send_paged_lines
from bot.modules.forms import rendering
from bot.modules.forms.cog import FormOpenView, FormsCog
from bot.modules.forms.schema import FormCatalog, FormDefinition
from bot.modules.forms.store import NEEDS_INFO, PENDING, FormBinding, FormStores

LOGGER = logging.getLogger("bot.forms")


class FormPanelCog(commands.Cog):
    def __init__(self, bot: Any, legacy: FormsCog, stores: FormStores, catalog: FormCatalog) -> None:
        self.bot = bot
        self.legacy = legacy
        self.stores = stores
        self.catalog = catalog

    def t(self, interaction: discord.Interaction, key: str, **kwargs: Any) -> str:
        return self.bot.t(interaction, key, **kwargs)

    def _admin(self, interaction: discord.Interaction) -> bool:
        return bool(getattr(interaction.permissions, "manage_guild", False))

    @app_commands.command(
        name="form",
        description=localized("Forms: applications, collection and signups", "commands.form.panel_description"),
    )
    @app_commands.describe(form=localized("Form id", "commands.form.param_form"))
    async def form(self, interaction: discord.Interaction, form: str | None = None) -> None:
        if interaction.guild is None:
            raise UserError("errors.guild_only")
        if form is not None:
            definition = self.catalog.get(form.strip())
            if definition is None:
                raise UserError("forms.unknown_form", form=form)
            await self._show_form(interaction, definition)
            return
        await self._show_main(interaction, edit=False)

    # ------------------------------------------------------------------ 主面板

    async def _show_main(self, interaction: discord.Interaction, *, edit: bool) -> None:
        if self._admin(interaction):
            definitions = self.catalog.all()
            pending = 0
            for definition in definitions:
                pending += await self.stores.submissions.count_for_form(
                    interaction.guild_id, definition.id, status=PENDING
                )
                pending += await self.stores.submissions.count_for_form(
                    interaction.guild_id, definition.id, status=NEEDS_INFO
                )
            embed = info_embed(
                self.t(interaction, "forms.panel.title"),
                self.t(
                    interaction,
                    "forms.panel.admin_body",
                    forms=len(definitions),
                    pending=pending,
                ),
            )
            view: discord.ui.View = FormAdminView(self, interaction.user.id, interaction.locale)
        else:
            embed = info_embed(
                self.t(interaction, "forms.panel.title"),
                self.t(interaction, "forms.panel.member_body"),
            )
            view = FormMemberView(self, interaction.user.id, interaction.locale)
        if edit:
            await interaction.response.edit_message(embed=embed, view=view)
        else:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def _show_form(self, interaction: discord.Interaction, definition: FormDefinition) -> None:
        if not self._admin(interaction):
            await self._show_open_publications(interaction, definition)
            return
        await self._show_detail(interaction, definition, edit=False)

    async def _show_detail(
        self, interaction: discord.Interaction, definition: FormDefinition, *, edit: bool
    ) -> None:
        binding = await self.stores.bindings.get(interaction.guild_id, definition.id)
        embed = info_embed(
            definition.id,
            await self._detail_body(interaction, definition, binding),
        )
        view = FormDetailView(self, interaction.user.id, interaction.locale, definition)
        if edit:
            await interaction.response.edit_message(embed=embed, view=view)
        else:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def _detail_body(
        self, interaction: discord.Interaction, definition: FormDefinition, binding: FormBinding
    ) -> str:
        lines = [
            self.t(interaction, "forms.panel.detail_mode", mode=definition.mode),
            self.t(interaction, "forms.panel.detail_fields", count=len(definition.fields)),
            self.t(
                interaction,
                "forms.panel.detail_binding",
                review=f"<#{binding.review_channel_id}>" if binding.review_channel_id else self.t(interaction, "forms.panel.none"),
                reviewer=f"<@&{binding.reviewer_role_id}>" if binding.reviewer_role_id else self.t(interaction, "forms.panel.none"),
                notify=f"<#{binding.notify_channel_id}>" if binding.notify_channel_id else self.t(interaction, "forms.panel.none"),
                grant=f"<@&{binding.grant_role_id}>" if binding.grant_role_id else self.t(interaction, "forms.panel.none"),
            ),
        ]
        if definition.grant_key > 0:
            drop = await self._drop_label(interaction, binding)
            lines.append(self.t(interaction, "forms.panel.detail_drop", drop=drop))
        return "\n".join(lines)

    async def _drop_label(self, interaction: discord.Interaction, binding: FormBinding) -> str:
        if binding.drop_id is None:
            return self.t(interaction, "forms.panel.none")
        keys = self.bot.services.get("keys")
        if keys is None:
            return self.t(interaction, "forms.key.unavailable")
        drop = await keys.drop(interaction.guild_id, binding.drop_id)
        return f"`{drop.code}`" if drop is not None else self.t(interaction, "forms.key.no_drop")

    # ------------------------------------------------------------------ 发布/结果/导出/恢复

    async def _publish(
        self,
        interaction: discord.Interaction,
        definition: FormDefinition,
        *,
        capacity: int | None,
        minutes: int | None,
    ) -> None:
        guild = interaction.guild
        channel = interaction.channel
        if guild is None or not isinstance(channel, discord.abc.Messageable):
            raise UserError("forms.bad_channel")
        binding = await self.stores.bindings.get(guild.id, definition.id)
        self.legacy._require_bindings(definition, binding)
        if capacity is not None and not definition.is_signup:
            raise UserError("forms.capacity_only_signup")
        tr, text = await self.legacy.guild_texts(guild.id)
        publication = await self.stores.publications.create(
            guild.id,
            form_id=definition.id,
            channel_id=channel.id,
            capacity=capacity,
            closes_at=_now_plus(minutes) if minutes is not None else None,
            created_by=interaction.user.id,
        )
        label = tr("forms.button.apply") if definition.is_apply else tr("forms.button.open")
        message = await channel.send(
            embed=rendering.open_card(definition, publication, text=text, tr=tr),
            view=FormOpenView(self.legacy, label=label),
        )
        await self.stores.publications.set_message(publication.publication_id, message.id)
        await interaction.response.send_message(
            embed=ok_embed(
                tr("forms.posted.title"),
                tr(
                    "forms.posted.body",
                    form=text(definition.title),
                    id=publication.publication_id,
                    channel=channel.mention,
                ),
            ),
            ephemeral=True,
        )

    # ------------------------------------------------------------------ 成员视图

    async def _show_open_publications(
        self, interaction: discord.Interaction, definition: FormDefinition | None = None
    ) -> None:
        definitions = [definition] if definition is not None else list(self.catalog.all())
        lines: list[str] = []
        for item in definitions:
            if item is None:
                continue
            for publication in await self.stores.publications.open_in_guild(interaction.guild_id, item.id):
                channel = interaction.guild.get_channel(publication.channel_id) if interaction.guild else None
                lines.append(
                    self.t(
                        interaction,
                        "forms.panel.member_open_line",
                        form=item.id,
                        channel=getattr(channel, "mention", str(publication.channel_id)),
                        id=publication.publication_id,
                    )
                )
        if not lines:
            await interaction.response.send_message(
                embed=info_embed(
                    self.t(interaction, "forms.panel.open_title"),
                    self.t(interaction, "forms.panel.open_none"),
                ),
                ephemeral=True,
            )
            return
        await send_paged_lines(
            interaction,
            title=self.t(interaction, "forms.panel.open_title"),
            lines=lines,
            translate=lambda key, **kw: self.t(interaction, key, **kw),
        )

    async def _show_my_submissions(self, interaction: discord.Interaction) -> None:
        lines: list[str] = []
        for definition in self.catalog.all():
            submission = await self.stores.submissions.latest_for_user(
                interaction.guild_id, definition.id, interaction.user.id
            )
            if submission is None:
                continue
            lines.append(
                self.t(
                    interaction,
                    "forms.panel.member_submission_line",
                    form=definition.id,
                    id=submission.submission_id,
                    status=self.t(interaction, f"forms.status.{submission.status}"),
                    time=submission.submitted_at,
                )
            )
        if not lines:
            await interaction.response.send_message(
                embed=info_embed(
                    self.t(interaction, "forms.panel.mine_title"),
                    self.t(interaction, "forms.panel.mine_none"),
                ),
                ephemeral=True,
            )
            return
        await send_paged_lines(
            interaction,
            title=self.t(interaction, "forms.panel.mine_title"),
            lines=lines,
            translate=lambda key, **kw: self.t(interaction, key, **kw),
        )


def _now_plus(minutes: int) -> str:
    return (datetime.fromisoformat(utcnow_iso()) + timedelta(minutes=minutes)).isoformat(timespec="seconds")


class PanelView(discord.ui.View):
    def __init__(self, cog: FormPanelCog, author_id: int, locale: Any, *, timeout: float = 300) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.author_id = author_id
        self.locale = locale

    def label(self, key: str, **kwargs: Any) -> str:
        return self.cog.bot.i18n.t(self.locale, key, **kwargs)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                self.cog.bot.i18n.t(self.locale, "forms.panel.not_yours"), ephemeral=True
            )
            return False
        return True


class FormAdminView(PanelView):
    def __init__(self, cog: FormPanelCog, author_id: int, locale: Any) -> None:
        super().__init__(cog, author_id, locale)
        self.add_item(_button(self.label("forms.panel.list"), discord.ButtonStyle.primary, self._list))
        self.add_item(_button(self.label("forms.panel.hint"), discord.ButtonStyle.secondary, self._hint))

    async def _list(self, interaction: discord.Interaction) -> None:
        definitions = list(self.cog.catalog.all())
        if not definitions:
            await interaction.response.send_message(
                embed=error_embed(self.cog.t(interaction, "forms.list.empty", path="data/forms")),
                ephemeral=True,
            )
            return
        options = [
            discord.SelectOption(
                label=definition.id[:100],
                value=definition.id,
                description=definition.mode,
            )
            for definition in definitions[:25]
        ]
        await interaction.response.edit_message(
            embed=info_embed(self.cog.t(interaction, "forms.panel.pick")),
            view=FormPickerView(self.cog, interaction.user.id, self.locale, options),
        )

    async def _hint(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=info_embed(
                self.cog.t(interaction, "forms.panel.hint_title"),
                self.cog.t(interaction, "forms.panel.hint_body"),
            ),
            ephemeral=True,
        )


class FormMemberView(PanelView):
    def __init__(self, cog: FormPanelCog, author_id: int, locale: Any) -> None:
        super().__init__(cog, author_id, locale)
        self.add_item(
            _button(self.label("forms.panel.open_title"), discord.ButtonStyle.primary, self._open)
        )
        self.add_item(
            _button(self.label("forms.panel.mine_title"), discord.ButtonStyle.secondary, self._mine)
        )

    async def _open(self, interaction: discord.Interaction) -> None:
        await self.cog._show_open_publications(interaction)

    async def _mine(self, interaction: discord.Interaction) -> None:
        await self.cog._show_my_submissions(interaction)


class FormPickerView(PanelView):
    def __init__(self, cog: FormPanelCog, author_id: int, locale: Any, options: list[discord.SelectOption]) -> None:
        super().__init__(cog, author_id, locale)
        select = discord.ui.Select(placeholder=self.label("forms.panel.pick"), options=options)
        select.callback = self._picked
        self.add_item(select)

    async def _picked(self, interaction: discord.Interaction) -> None:
        values = interaction.data.get("values", []) if interaction.data else []
        if not values:
            return
        definition = self.cog.catalog.get(values[0])
        if definition is None:
            await interaction.response.send_message(
                embed=error_embed(self.cog.t(interaction, "forms.unknown_form", form=values[0])),
                ephemeral=True,
            )
            return
        await self.cog._show_detail(interaction, definition, edit=True)


class FormDetailView(PanelView):
    def __init__(self, cog: FormPanelCog, author_id: int, locale: Any, definition: FormDefinition) -> None:
        super().__init__(cog, author_id, locale)
        self.definition = definition
        self.add_item(_button(self.label("forms.panel.publish"), discord.ButtonStyle.success, self._publish))
        self.add_item(_button(self.label("forms.panel.bind"), discord.ButtonStyle.secondary, self._bind))
        self.add_item(_button(self.label("forms.panel.results"), discord.ButtonStyle.secondary, self._results))
        self.add_item(_button(self.label("forms.panel.export"), discord.ButtonStyle.secondary, self._export))
        if definition.grant_key > 0:
            self.add_item(_button(self.label("forms.panel.resume"), discord.ButtonStyle.secondary, self._resume))
        self.add_item(_button(self.label("forms.panel.back"), discord.ButtonStyle.secondary, self._back))

    async def _publish(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(PublishModal(self.cog, self.definition))

    async def _bind(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            embed=info_embed(self.cog.t(interaction, "forms.panel.bind_pick")),
            view=BindFieldView(self.cog, interaction.user.id, self.locale, self.definition),
        )

    async def _results(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ResultsModal(self.cog, self.definition))

    async def _export(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ExportModal(self.cog, self.definition))

    async def _resume(self, interaction: discord.Interaction) -> None:
        await self.cog.legacy.form_resume.callback(self.cog.legacy, interaction, self.definition.id)

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.cog._show_main(interaction, edit=True)


class BindFieldView(PanelView):
    FIELDS = ("review_channel", "reviewer_role", "notify_channel", "grant_role", "drop")

    def __init__(self, cog: FormPanelCog, author_id: int, locale: Any, definition: FormDefinition) -> None:
        super().__init__(cog, author_id, locale)
        self.definition = definition
        options = [
            discord.SelectOption(
                label=self.label(f"forms.panel.bind_{field}"),
                value=field,
            )
            for field in self.FIELDS
        ]
        select = discord.ui.Select(placeholder=self.label("forms.panel.bind_pick"), options=options)
        select.callback = self._picked
        self.add_item(select)

    async def _picked(self, interaction: discord.Interaction) -> None:
        values = interaction.data.get("values", []) if interaction.data else []
        if not values:
            return
        field = values[0]
        options = await self._options(interaction, field)
        await interaction.response.edit_message(
            embed=info_embed(self.cog.t(interaction, "forms.panel.bind_value")),
            view=BindValueView(self.cog, interaction.user.id, self.locale, self.definition, field, options),
        )

    async def _options(self, interaction: discord.Interaction, field: str) -> list[discord.SelectOption]:
        guild = interaction.guild
        none = discord.SelectOption(label=self.cog.t(interaction, "forms.panel.none"), value="0")
        if field in {"review_channel", "notify_channel"}:
            channels = list(guild.text_channels)[:24]
            return [none] + [discord.SelectOption(label=f"#{channel.name}"[:100], value=str(channel.id)) for channel in channels]
        if field in {"reviewer_role", "grant_role"}:
            roles = [role for role in guild.roles if not role.is_default()][:24]
            return [none] + [discord.SelectOption(label=role.name[:100], value=str(role.id)) for role in roles]
        keys = self.cog.bot.services.get("keys")
        if keys is None:
            return [none]
        drops = [drop for drop in await keys.active_drops(guild.id) if drop.mode == "fcfs"][:24]
        return [none] + [discord.SelectOption(label=drop.code, value=str(drop.id)) for drop in drops]


class BindValueView(PanelView):
    def __init__(
        self,
        cog: FormPanelCog,
        author_id: int,
        locale: Any,
        definition: FormDefinition,
        field: str,
        options: list[discord.SelectOption],
    ) -> None:
        super().__init__(cog, author_id, locale)
        self.definition = definition
        self.field = field
        select = discord.ui.Select(placeholder=self.label("forms.panel.bind_value"), options=options)
        select.callback = self._picked
        self.add_item(select)

    async def _picked(self, interaction: discord.Interaction) -> None:
        values = interaction.data.get("values", []) if interaction.data else []
        if not values:
            return
        column = {
            "review_channel": "review_channel_id",
            "reviewer_role": "reviewer_role_id",
            "notify_channel": "notify_channel_id",
            "grant_role": "grant_role_id",
            "drop": "drop_id",
        }[self.field]
        await self.cog.stores.bindings.update(
            interaction.guild_id, self.definition.id, **{column: int(values[0]) or None}
        )
        await self.cog._show_detail(interaction, self.definition, edit=True)


class PublishModal(discord.ui.Modal):
    def __init__(self, cog: FormPanelCog, definition: FormDefinition) -> None:
        super().__init__(title=f"{definition.id} · publish")
        self.cog = cog
        self.definition = definition
        self.capacity = discord.ui.TextInput(label="Capacity (signup only, optional)", required=False, max_length=4)
        self.minutes = discord.ui.TextInput(label="Auto-close minutes (optional)", required=False, max_length=5)
        self.add_item(self.capacity)
        self.add_item(self.minutes)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            capacity = int(self.capacity.value) if self.capacity.value.strip() else None
            minutes = int(self.minutes.value) if self.minutes.value.strip() else None
        except ValueError:
            await interaction.response.send_message(
                embed=error_embed("capacity/minutes must be numbers"), ephemeral=True
            )
            return
        await self.cog._publish(interaction, self.definition, capacity=capacity, minutes=minutes)


class ResultsModal(discord.ui.Modal):
    def __init__(self, cog: FormPanelCog, definition: FormDefinition) -> None:
        super().__init__(title=f"{definition.id} · results")
        self.cog = cog
        self.definition = definition
        self.limit = discord.ui.TextInput(label="How many (optional)", required=False, max_length=3)
        self.add_item(self.limit)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        limit = self.limit.value.strip()
        await self.cog.legacy.form_results.callback(
            self.cog.legacy,
            interaction,
            form=self.definition.id,
            limit=int(limit) if limit.isdigit() else None,
        )


class ExportModal(discord.ui.Modal):
    def __init__(self, cog: FormPanelCog, definition: FormDefinition) -> None:
        super().__init__(title=f"{definition.id} · export")
        self.cog = cog
        self.definition = definition

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.legacy.form_export.callback(
            self.cog.legacy, interaction, form=self.definition.id
        )


def _button(label: str, style: discord.ButtonStyle, callback) -> discord.ui.Button:
    button = discord.ui.Button(label=label, style=style)
    button.callback = callback
    return button


__all__ = ["FormPanelCog"]

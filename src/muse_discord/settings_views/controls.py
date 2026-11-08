"""Native buttons, selects and value modals dispatching to a screen session."""

import asyncio
from typing import cast

import discord
from discord import ui

from muse_discord import muse_settings as ms
from muse_discord.settings_views.contracts import PREFIX, TTL, SettingsClient
from muse_discord.settings_views.interactions import check_output, error
from muse_discord.settings_views.labels import TITLES
from muse_discord.settings_views.model import Screen, current_value


class Action(ui.DynamicItem, template=r"mset:(?P<token>[A-Za-z0-9_-]{12}):(?P<action>[a-z]+)"):
    """discord.py routes both buttons and selects; the session owns state."""

    def __init__(self, item):
        super().__init__(item)
        match = self.template.fullmatch(item.custom_id)
        if match is None:
            raise ValueError("invalid settings action id")
        self.token, self.action = match["token"], match["action"]

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(item)

    async def callback(self, interaction):
        try:
            ctx = await asyncio.to_thread(cast(SettingsClient, interaction.client).settings_context)
            screen = ctx.session_peek(str(interaction.user.id), self.token)
            if not isinstance(screen, Screen):
                raise ms.SettingsError("この画面は期限切れです。/settingsを開き直してください。")
            screen.authorize(interaction, ctx)
            if self.action == "modal":
                modal = ValueModal(screen, ctx)
                check_output(interaction, modal.to_dict())
                await interaction.response.send_modal(modal)
                return
            await interaction.response.defer()
            async with screen.lock:
                # 同じ画面で連続クリックしても、最新の設定・下書きから処理する。
                ctx = await asyncio.to_thread(
                    cast(SettingsClient, interaction.client).settings_context
                )
                role = screen.authorize(interaction, ctx)
                vals = (interaction.data or {}).get("values", [])
                if self.action == "people":
                    vals = [
                        vals,
                        ((interaction.data or {}).get("resolved") or {}).get("roles", {}),
                    ]
                banner = await asyncio.to_thread(screen.act, ctx, role, self.action, vals)
                await cast(SettingsClient, interaction.client).settings_redraw(
                    interaction, screen, banner
                )
        except (ms.SettingsError, ValueError, KeyError, IndexError) as exc:
            await error(interaction, str(exc))
        except Exception as exc:
            cast(SettingsClient, interaction.client).settings_log(
                f"settings action failed: {type(exc).__name__}: {exc}"
            )
            await error(
                interaction,
                "設定画面でエラーが発生しました。/settingsを開き直してください。",
            )


class ValueModal(ui.Modal):
    def __init__(self, screen: Screen, ctx):
        self.screen, self.key = screen, screen.key
        spec = ctx.settings.spec(self.key)
        current = screen.draft.get(
            self.key, current_value(ctx.settings, screen.place(ctx), self.key)
        )
        if (
            self.key == "style.notes"
            and screen.place(ctx).kind == "channel"
            and self.key not in screen.draft
        ):
            path = ms.place_path(ctx.settings, screen.place(ctx), self.key)
            supplied, local, _ = ctx.settings.layer(path, self.key)
            current = local if supplied else ""
        if current == "inherit":
            current = ""
        if (spec.type == "people" or spec.ids) and not isinstance(current, list):
            current = []
        default = (
            ", ".join(current)
            if isinstance(current, list)
            else ""
            if current is None
            else str(current)
        )
        super().__init__(
            title=TITLES.get(self.key, ms.label(self.key))[:45],
            timeout=TTL,
            custom_id=f"{PREFIX}{screen.token}:submit",
        )
        hint = (
            "空欄で無制限"
            if spec.type == "number" and spec.none
            else (
                "ユーザーID / role:ロールIDをカンマか改行で区切る"
                if spec.type == "people" or spec.ids
                else "設定する値"
            )
        )
        if spec.type == "number":
            hint = f"{spec.min}〜{spec.max}の整数。" + ("空欄で無制限" if spec.none else "")
        self.input = ui.TextInput(
            style=discord.TextStyle.paragraph if spec.type != "number" else discord.TextStyle.short,
            default=default[: spec.maxlen or 4000],
            required=False,
            max_length=spec.maxlen or 4000,
        )
        self.add_item(ui.Label(text="新しい値", description=hint, component=self.input))

    async def on_submit(self, interaction):
        await interaction.response.defer()
        try:
            async with self.screen.lock:
                ctx = await asyncio.to_thread(
                    cast(SettingsClient, interaction.client).settings_context
                )
                active = ctx.session_peek(str(interaction.user.id), self.screen.token)
                if active is not self.screen:
                    raise ms.SettingsError(
                        "画面の有効期限が切れました。/settingsを開き直してください。"
                    )
                role = self.screen.authorize(interaction, ctx)
                if self.screen.key != self.key:
                    raise ms.SettingsError(
                        "編集中の設定項目が変わりました。入力画面を開き直してください。"
                    )
                await asyncio.to_thread(self.screen.act, ctx, role, "input", [self.input.value])
                await cast(SettingsClient, interaction.client).settings_redraw(
                    interaction,
                    self.screen,
                    "下書きに追加しました。保存するまで反映されません。",
                )
        except (ms.SettingsError, ValueError) as exc:
            await error(interaction, str(exc))

    async def on_error(self, interaction, exc):
        cast(SettingsClient, interaction.client).settings_log(
            f"settings modal failed: {type(exc).__name__}: {exc}"
        )
        await error(interaction, "入力を保存できませんでした。設定画面を開き直してください。")

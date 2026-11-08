"""Command entrypoints and asynchronous redraw of private settings panels."""

import asyncio
from typing import cast

import discord

from muse_discord import muse_settings as ms
from muse_discord.settings_views.contracts import TTL, SettingsClient
from muse_discord.settings_views.interactions import check_output
from muse_discord.settings_views.model import Screen, role_for
from muse_discord.settings_views.panel import Panel
from muse_discord.settings_views.presentation import outcome_text, text


async def redraw(interaction, screen, banner=""):
    ctx = await asyncio.to_thread(cast(SettingsClient, interaction.client).settings_context)
    role = screen.authorize(interaction, ctx)
    view = Panel(screen, ctx, role, banner, interaction.guild_id)
    check_output(interaction, view.to_components())
    ctx.session_put(screen.uid, screen.token, screen, ttl=TTL)
    await interaction.edit_original_response(
        view=view, allowed_mentions=discord.AllowedMentions.none()
    )


async def open_settings(interaction, ctx, place=None):
    """Native command entry; all filesystem work follows an acknowledgement."""
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        role = role_for(interaction, ctx)
        if place is None:
            if role == "admin":
                place = ms.Place.server(str(interaction.guild_id))
            elif interaction.guild_id:
                channel = interaction.channel
                cid = str(
                    channel.parent_id
                    if isinstance(channel, discord.Thread)
                    else interaction.channel_id
                )
                place = ms.parse_place(f"channel:{interaction.guild_id}:{cid}", ctx)
            else:
                place = ms.Place.dms()
        screen = Screen(str(interaction.user.id), ms.format_place(place))
        screen.authorize(interaction, ctx)
        await redraw(interaction, screen)
    except ms.SettingsError as exc:
        await interaction.edit_original_response(
            content=text(exc), allowed_mentions=discord.AllowedMentions.none()
        )


async def why_message(interaction, ctx):
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        role_for(interaction, ctx)
        data = interaction.data or {}
        mid = str(data.get("target_id", ""))
        outcome = await asyncio.to_thread(ctx.outcome, mid)
        if outcome and (outcome.get("guild_id") or None) == (
            str(interaction.guild_id) if interaction.guild_id else None
        ):
            body = outcome_text(outcome)
        else:
            body = (
                "このメッセージの処理記録はありません。"
                "/settingsで返信条件やDiscordの権限を確認してください。"
            )
        check_output(interaction, body)
        await interaction.edit_original_response(
            content=text(body, 1500), allowed_mentions=discord.AllowedMentions.none()
        )
    except ms.SettingsError as exc:
        await interaction.edit_original_response(
            content=text(exc), allowed_mentions=discord.AllowedMentions.none()
        )

"""Guard outgoing UI payloads and acknowledge native interaction errors."""

import discord

from muse_discord.settings_views.presentation import text


def check_output(interaction, payload):
    interaction.client.settings_guard(payload, "settings UI")


async def error(interaction, message):
    kwargs = dict(
        content=text(message, 1200),
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    check_output(interaction, kwargs)
    if interaction.response.is_done():
        await interaction.followup.send(**kwargs)
    else:
        await interaction.response.send_message(**kwargs)

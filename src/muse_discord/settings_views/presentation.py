"""Render untrusted names, values and reply diagnostics as safe display text."""

import re

import discord

from muse_discord.settings_display import clip
from muse_discord.settings_views.labels import REASONS, WORDS


def text(value, limit=500):
    """Names and values are untrusted Discord text; never allow mentions."""
    value = re.sub(r"[\u202a-\u202e\u2066-\u2069]", "", str(value))
    return clip(discord.utils.escape_markdown(discord.utils.escape_mentions(value)), limit)


def value_text(value):
    if value is None:
        return "無制限"
    if isinstance(value, bool):
        return "有効" if value else "無効"
    if isinstance(value, list):
        return text(", ".join(value) if value else "なし", 350)
    return text(WORDS.get(value, value) if value != "" else "未設定", 350)


def outcome_text(outcome):
    status = outcome.get("status")
    if status == "replied":
        return "返信済みです。"
    if status == "queued":
        return "返信を処理中です。"
    reason = REASONS.get(
        outcome.get("code"), "返信しなかった理由の詳細はmonitor.logを確認してください。"
    )
    return f"{outcome.get('author') or '投稿者不明'}：{reason}"

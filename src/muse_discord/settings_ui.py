"""Discord settings built with discord.py's native components.

One private screen: choose a place, a category and a setting. Every edit
waits in the same draft until Save. The settings engine still owns validation,
inheritance, permissions and undo; settings_assessment evaluates change risks.
Sessions are user-bound and expire after 15 minutes; restarting never resurrects
a pending change.
"""

import asyncio
import json
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, NotRequired, Protocol, TypedDict, cast

import discord
from discord import ui

from muse_discord import muse_settings as ms
from muse_discord.settings_assessment import assess
from muse_discord.settings_display import clip
from muse_discord.settings_presets import preset_changes

MAX_CONFIRMATION_WARNINGS = 5


def action_row(*children: ui.Item[ui.View | ui.LayoutView]) -> ui.ActionRow[ui.LayoutView]:
    # DynamicItem is typed for either kind of view; these items belong to
    # this module's LayoutView. Keep the SDK's type adaptation in one place.
    items = cast(tuple[ui.Item[ui.LayoutView], ...], children)
    return ui.ActionRow[ui.LayoutView](*items)


class Pending(TypedDict):
    warnings: list[str]
    undo: NotRequired[str | None]


class SettingsClient(Protocol):
    settings_context: Callable[[], Any]
    settings_log: Callable[[str], None]
    settings_guard: Callable[[Any, str], Any]
    settings_redraw: Callable[..., Awaitable[None]]


TTL = 900
PREFIX = "mset:"
CATEGORIES = {
    "reply": (
        "返信・話し方",
        (
            "who.replies",
            "style.length",
            "style.notes",
            "limits.per_day",
            "limits.per_person_per_day",
        ),
    ),
    "people": (
        "利用者・権限",
        (
            "who.dms_from",
            "who.allow",
            "who.ignore",
            "who.bots",
            "who.bot_exchange_limit",
            "abilities.reminders",
            "abilities.owner_commands",
            "abilities.external_apps",
        ),
    ),
    "proactive": (
        "自発的な発言",
        tuple(k for k in ms.CORE if k.startswith("proactive.")),
    ),
    "media": ("画像・動画", tuple(k for k in ms.CORE if k.startswith("media."))),
    "privacy": (
        "プライバシー",
        ("privacy.memory", "privacy.suspicious", "memory.save_dms"),
    ),
    "other": (
        "その他",
        ("presence.status", "presence.kind", "updates.notify", "telemetry.usage_stats"),
    ),
}
TITLES = {
    "who.replies": "返信するタイミング",
    "style.length": "返信の長さ",
    "style.notes": "話し方の指示",
    "limits.per_day": "1日の返信上限",
    "limits.per_person_per_day": "1人あたりの1日の返信上限",
    "who.dms_from": "DMを許可する相手",
    "who.allow": "返信を許可する相手",
    "who.ignore": "無視する相手",
    "who.bots": "他のBotへの返信",
    "who.bot_exchange_limit": "Botとの連続返信上限",
    "abilities.reminders": "リマインダー・監視を使える相手",
    "abilities.owner_commands": "所有者からの操作依頼",
    "abilities.external_apps": "外部アプリへのアクセス",
    "privacy.memory": "会話で使える個人情報",
    "privacy.suspicious": "攻撃が疑われるメッセージ",
    "memory.save_dms": "DMの内容を記憶に保存",
    "media.enabled": "画像・動画の生成",
    "media.who": "生成を依頼できる相手",
    "media.per_day": "1日の生成上限",
    "proactive.enabled": "自発的な発言を許可",
    "proactive.frequency": "自発的に話しかける頻度",
    "proactive.per_person_per_day": "1人あたりの1日上限",
    "proactive.per_person_per_week": "1人あたりの1週間上限",
    "proactive.per_channel_per_day": "1チャンネルあたりの1日上限",
    "proactive.quiet_hours": "自発的な発言を控える時間帯",
    "proactive.timezone": "タイムゾーン",
    "presence.status": "ステータスの文章",
    "presence.kind": "ステータスの種類",
    "updates.notify": "更新のお知らせ",
    "telemetry.usage_stats": "匿名の利用統計",
}
WORDS = {
    "off": "無効",
    "when-addressed": "呼びかけられたとき",
    "conversational": "呼びかけと会話の続き",
    "freely": "呼びかけなしで会話に参加・自発的に発言",
    "brief": "短め",
    "normal": "標準",
    "detailed": "詳しく",
    "owners": "所有者のみ",
    "everyone": "全員",
    "anyone": "誰でも",
    "shared-servers": "共通サーバーの参加者",
    "ignore": "無視する",
    "hold-for-review": "所有者の確認を待つ",
    "everything": "記憶している情報すべて",
    "public-only": "公開されている情報のみ",
    "nothing": "個人情報を使わない",
    "rarely": "少なめ（1日最大2件）",
    "sometimes": "ときどき（1日最大5件）",
    "often": "多め（1日最大10件）",
    "read-only": "読み取りのみ",
    "confirm": "書き込みは確認する",
    "write": "指定範囲の書き込みを許可",
    "unconditional": "登録済み操作を確認なしで許可",
}
REASONS = {
    "replies-off": "この場所では返信が無効です。",
    "dm-off": "DMへの返信が無効です。",
    "bots-ignore": "他のBotには返信しない設定です。",
    "not-allowed": "許可リスト・無視リストの設定で返信しませんでした。",
    "dm-not-allowed": "この相手からのDMは許可されていません。",
    "cap": "返信の日次上限に達しました。",
    "quarantined": "所有者の確認待ちです。DMを確認してください。",
    "suspicious-ignored": "攻撃の可能性があるため無視しました。",
    "no-send": "Discordのチャンネル権限でメッセージを送信できません。",
    "internal": "内部エラーです。monitor.logを確認してください。",
    "worker-skipped": "メッセージを読み、返信しないと判断しました。",
    "send-failed": "送信に失敗しました。monitor.logを確認してください。",
    "never-share": "共有禁止の文章を含むため返信を保留しました。",
    "predates-join": "Botが参加する前のメッセージです。",
    "ownerless": "サーバー内に所有者がおらず、返信が有効化されていません。",
    "bot-exchange": "Botとの連続返信上限に達しました。",
    "watch-cap": "リマインダー・監視の上限に達しました。",
    "watch-held": "リマインダーの配信を保留しています。",
    "watch-not-allowed": "リマインダー・監視の利用が許可されていません。",
}


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


def current_value(st, place, key):
    return st.top(key) if key in ms.TOP else st.get(key, place.scope(key))


def check_output(interaction, payload):
    interaction.client.settings_guard(payload, "settings UI")


def role_for(interaction, ctx):
    if str(interaction.user.id) in set(map(str, ctx.settings.owners)):
        return "owner"
    if interaction.guild_id and interaction.permissions.manage_guild:
        return "admin"
    raise ms.SettingsError("設定を変更できるのはBotの所有者とサーバー管理者です。")


def available(ctx, place, role):
    """Use the schema's edit policy; never keep another list of valid scopes."""
    keys = []
    for key, spec in (ms.CORE | ms.TOP).items():
        if not spec.discord_editable or spec.hidden:
            continue
        if place.kind == "defaults" and key == "who.dms_from":
            continue
        if role == "admin" and key not in (
            "who.replies",
            "media.enabled",
            "proactive.enabled",
            "who.ignore",
        ):
            continue
        try:
            ms.place_path(ctx.settings, place, key)
        except ms.SettingsError:
            continue
        keys.append(key)
    return keys


@dataclass
class Screen:
    uid: str
    place_text: str
    token: str = field(default_factory=lambda: secrets.token_urlsafe(9))
    category: str = "reply"
    key: str = "who.replies"
    draft: dict = field(default_factory=dict)
    baseline: dict = field(default_factory=dict)
    pending: Pending | None = None
    browse: str = ""
    page: int = 0
    undo_id: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def place(self, ctx):
        p = ms.parse_place(self.place_text, ctx)
        if p.guild_id and p.guild_id not in {str(g["id"]) for g in ctx.guilds()}:
            raise ms.SettingsError("このサーバーにはもう参加していません。")
        if p.channel_id and not any(
            str(c["id"]) == p.channel_id and c.get("readable", True)
            for c in ctx.channels(p.guild_id)
        ):
            raise ms.SettingsError("このチャンネルを確認できません。")
        return p

    def authorize(self, interaction, ctx):
        if str(interaction.user.id) != self.uid:
            raise ms.SettingsError(
                "この画面は別の人のものです。/settingsで自分の画面を開いてください。"
            )
        role = role_for(interaction, ctx)
        place = self.place(ctx)
        if role == "admin" and (
            place.kind not in ("server", "channel") or place.guild_id != str(interaction.guild_id)
        ):
            raise ms.SettingsError("管理しているサーバー以外の設定は変更できません。")
        return role

    def changes(self, ctx):
        place = self.place(ctx)
        return [ms.Change(place, key, val) for key, val in self.draft.items()]

    def stage(self, ctx, role, key, val):
        place = self.place(ctx)
        if key not in available(ctx, place, role):
            raise ms.SettingsError("この場所では変更できない設定です。")
        changes = [ms.Change(place, key, val)]
        if role == "admin":
            changes = ms.admin_changes(ctx.settings, place.guild_id, changes, ctx)
        planned = ms.plan_changes(ctx.settings, changes, role)
        path = ms.place_path(ctx.settings, place, key)
        # 値だけでなく継承元の変更も検知し、古い画面から上書きしない。
        self.baseline.setdefault(
            key,
            (ctx.settings.layer(path, key), current_value(ctx.settings, place, key)),
        )
        if planned[0].noop:
            self.draft.pop(key, None)
            self.baseline.pop(key, None)
        else:
            self.draft[key] = changes[0].value
        self.pending = None

    def clear(self):
        self.draft.clear()
        self.baseline.clear()
        self.pending = None

    def act(self, ctx, role, action, values):
        """Dispatch one deferred UI action without mixing navigation and writes."""
        place = self.place(ctx)
        self.pending = self.pending if action in ("confirm", "back") else None
        handlers = {
            "category": self._select_setting,
            "key": self._select_setting,
            "scope": self._navigate,
            "browse": self._navigate,
            "previous": self._navigate,
            "next": self._navigate,
            "preset": self._edit_draft,
            "set": self._edit_draft,
            "inherit": self._edit_draft,
            "people": self._edit_draft,
            "input": self._edit_draft,
            "discard": self._edit_draft,
            "save": self._finish_edit,
            "confirm": self._finish_edit,
            "undo": self._finish_edit,
            "back": self._finish_edit,
            "why": self._finish_edit,
        }
        handler = handlers.get(action)
        if handler is None:
            raise ms.SettingsError("この操作は利用できません。/settingsを開き直してください。")
        return handler(ctx, role, action, values, place)

    def _select_setting(self, ctx, role, action, values, place):
        if action in ("category", "key"):
            if action == "category":
                if values[0] not in CATEGORIES:
                    raise ms.SettingsError("不明な分類です。")
                self.category = values[0]
            else:
                if values[0] not in available(ctx, place, role):
                    raise ms.SettingsError("この設定は選べません。")
                self.key = values[0]
        return ""

    def _navigate(self, ctx, role, action, values, place):
        if action in ("scope", "browse", "previous", "next"):
            if self.draft:
                raise ms.SettingsError("適用先を変える前に、下書きを保存するか破棄してください。")
            if action == "scope" and values[0] not in ("servers", "channels"):
                new = ms.parse_place(values[0], ctx)
                if role == "admin" and (
                    new.guild_id != place.guild_id or new.kind not in ("server", "channel")
                ):
                    raise ms.SettingsError("管理しているサーバー以外は選べません。")
                self.place_text = ms.format_place(new)
                self.place(ctx)
                self.browse = ""
            elif action in ("previous", "next"):
                self.page = max(0, self.page + (1 if action == "next" else -1))
            else:
                self.browse = values[0]
                self.page = 0
        return ""

    def _edit_draft(self, ctx, role, action, values, place):
        if action == "preset":
            if role != "owner" or place.kind != "server":
                raise ms.SettingsError("プリセットは所有者がサーバー全体に設定できます。")
            staged = ms.simulate(ctx.settings, self.changes(ctx))
            for change in preset_changes(staged, values[0], place, ctx):
                self.stage(ctx, role, change.key, change.value)
            return "プリセットを下書きに追加しました。保存前に変更内容を確認してください。"
        elif action == "set":
            self.stage(ctx, role, self.key, json.loads(values[0]))
        elif action == "inherit":
            self.stage(ctx, role, self.key, "inherit")
        elif action == "people":
            resolved_roles = values[1]
            ids = [("role:" if v in resolved_roles else "") + v for v in values[0]]
            self.stage(ctx, role, self.key, ids)
        elif action == "input":
            self.stage(ctx, role, self.key, parse_input(ctx.settings.spec(self.key), values[0]))
        elif action == "discard":
            self.clear()
            return "下書きを破棄しました。"
        return ""

    def _finish_edit(self, ctx, role, action, values, place):
        if action in ("save", "confirm"):
            return self.save(ctx, role, confirmed=action == "confirm")
        elif action == "undo":
            if self.draft:
                raise ms.SettingsError("先に下書きを保存するか破棄してください。")
            children, admin = (
                place.kind == "server",
                place.guild_id if role == "admin" else None,
            )
            plan = ms.undo_plan(ctx.settings, place, children, admin)
            _, current = ms.undo_preview(
                ctx.settings, place, include_children=children, admin_gid=admin
            )
            if not current or current != self.undo_id:
                raise ms.SettingsError(
                    "設定の履歴が変わりました。画面を更新してからやり直してください。"
                )
            assessment = assess(ctx.settings, plan, ctx) if role == "owner" else None
            self.pending = {
                "undo": current,
                "warnings": assessment.warnings if assessment else [],
            }
        elif action == "back":
            self.pending, self.browse = None, ""
        elif action == "why":
            self.browse = "why"
        else:
            raise ms.SettingsError("この操作は利用できません。/settingsを開き直してください。")
        return ""

    def save(self, ctx, role, confirmed=False):
        place = self.place(ctx)
        if ctx.settings.overlay_error:
            raise ms.SettingsError("config.discord.yamlを読めないため、保存を停止しています。")
        if confirmed and not self.pending:
            raise ms.SettingsError("確認内容の有効期限が切れました。保存し直してください。")
        pending = self.pending or Pending(warnings=[])
        if confirmed and len(pending.get("warnings", [])) > MAX_CONFIRMATION_WARNINGS:
            raise ms.SettingsError("変更を少数ずつ保存して、すべての警告を確認してください。")
        if confirmed and "undo" in pending:
            return self.save_undo(ctx, role, pending, place)
        changes = self.changes(ctx)
        if not changes:
            return "変更はありません。"
        self.check_baseline(ctx, place)
        if role == "admin":
            changes = ms.admin_changes(ctx.settings, place.guild_id, changes, ctx)
        assessment = assess(ctx.settings, changes, ctx) if role == "owner" else None
        if assessment and assessment.risky:
            # 確認中に条件が変わった場合は、新しい警告をもう一度表示する。
            warnings = assessment.warnings
            if not confirmed or pending.get("warnings") != warnings:
                self.pending = {"warnings": warnings}
                return ""
        res = ctx.apply(
            changes,
            self.uid,
            role,
            admin_gid=place.guild_id if role == "admin" else None,
        )
        notify_admin(ctx, role, place, self.uid, res.written)
        self.clear()
        return "保存しました。次のメッセージから反映されます。"

    def check_baseline(self, ctx, place):
        for key, old in self.baseline.items():
            path = ms.place_path(ctx.settings, place, key)
            if old != (
                ctx.settings.layer(path, key),
                current_value(ctx.settings, place, key),
            ):
                raise ms.SettingsError(
                    "編集中に設定が変更されました。下書きを破棄して最新の値からやり直してください。"
                )

    def save_undo(self, ctx, role, pending, place):
        expected = pending["undo"]
        kw = {"children": place.kind == "server", "expect": expected}
        if role == "admin":
            kw["admin_gid"] = place.guild_id
        plan = ms.undo_plan(ctx.settings, place, kw["children"], kw.get("admin_gid"))
        assessment = assess(ctx.settings, plan, ctx) if role == "owner" else None
        if assessment and assessment.warnings != pending["warnings"]:
            pending["warnings"] = assessment.warnings
            return ""
        planned = ms.plan_changes(ctx.settings, plan, role)
        ctx.undo(place, self.uid, **kw)
        self.pending = None
        notify_admin(ctx, role, place, self.uid, planned)
        return "直前の変更を取り消しました。"


def notify_admin(ctx, role, place, uid, planned):
    if role == "admin":
        for entry in planned:
            try:
                key = entry.keystr if hasattr(entry, "keystr") else entry.key
                ctx.notify_owners_admin_change(
                    place.guild_id,
                    uid,
                    key,
                    getattr(entry, "old", None),
                    getattr(entry, "new", None),
                )
            except Exception as exc:
                ctx.log(f"admin notification: {exc}")


def parse_input(spec, raw):
    if spec.type == "number":
        value = None if not raw.strip() and spec.none else json.loads(raw)
    elif spec.type == "people" or spec.ids:
        value = [v for v in re.split(r"[\s,]+", raw.strip()) if v]
    else:
        value = raw
    ok, value = spec.normalize(value)
    if not ok:
        raise ms.SettingsError(value)
    return value


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


async def redraw(interaction, screen, banner=""):
    ctx = await asyncio.to_thread(cast(SettingsClient, interaction.client).settings_context)
    role = screen.authorize(interaction, ctx)
    view = Panel(screen, ctx, role, banner, interaction.guild_id)
    check_output(interaction, view.to_components())
    ctx.session_put(screen.uid, screen.token, screen, ttl=TTL)
    await interaction.edit_original_response(
        view=view, allowed_mentions=discord.AllowedMentions.none()
    )


class Panel(ui.LayoutView):
    def __init__(self, screen, ctx, role, banner="", interaction_gid=None):
        super().__init__(timeout=None)
        self.screen, self.ctx, self.role = screen, ctx, role
        self.place = screen.place(ctx)
        self.interaction_gid = str(interaction_gid) if interaction_gid else None
        self.body = ui.Container(accent_colour=discord.Colour.blurple())
        self.add_item(self.body)
        self.body.add_item(
            ui.TextDisplay(f"## Museの設定 · {text(ms.place_name(self.place, ctx.names), 100)}")
        )
        self.body.add_item(
            ui.TextDisplay(
                "変更は下書きに保存されます。最後に「保存」を押してください。"
                if role == "owner"
                else "サーバー管理者は、返信を控える方向の変更だけ行えます。"
            )
        )
        if banner:
            self.body.add_item(ui.TextDisplay(text(banner, 600)))
        if ctx.settings.overlay_error:
            self.body.add_item(
                ui.TextDisplay(
                    "⚠ config.discord.yamlを読めません。ファイルを修正するまで保存できません。"
                )
            )
        if screen.pending is not None:
            self.confirmation()
        elif screen.browse in ("servers", "channels"):
            self.browse()
        elif screen.browse == "why":
            self.diagnostics()
        else:
            self.editor()

    def button(self, label, action, *, disabled=False, style=discord.ButtonStyle.secondary):
        return Action(
            ui.Button(
                label=label,
                style=style,
                disabled=disabled,
                custom_id=f"{PREFIX}{self.screen.token}:{action}",
            )
        )

    def select(self, action, options, placeholder):
        self.body.add_item(
            action_row(
                Action(
                    ui.Select(
                        custom_id=f"{PREFIX}{self.screen.token}:{action}",
                        options=options,
                        placeholder=placeholder,
                    )
                )
            )
        )

    def options(self, values, current=None):
        return [
            discord.SelectOption(label=clip(label, 100), value=val, default=val == current)
            for label, val in values
        ]

    def setting_source(self, key, path):
        st, p, ctx = self.ctx.settings, self.place, self.ctx
        if key in ms.TOP:
            current = st.top(key)
            source = "設定ファイル / Discord" if st.layer(path, key)[0] else "組み込みの初期値"
        else:
            current, trace = st.trace(p.scope(key), key)
            source_path = next(
                (path_ for _, path_, supplied, _, _ in reversed(trace) if supplied),
                ("builtin",),
            )
            source_place = ms.place_of_path(source_path)
            source = (
                ms.place_name(source_place, ctx.names)
                if source_place
                else "組み込み / サーバー共通の初期値"
            )
        return current, source

    def edit_footer(self):
        s, ctx, p = self.screen, self.ctx, self.place
        st = ctx.settings
        if s.draft:
            lines = [
                f"• {TITLES[k]} → {'継承' if v == 'inherit' else value_text(v)}"
                for k, v in s.draft.items()
            ]
            self.body.add_item(
                ui.TextDisplay(f"**未保存の変更：{len(lines)}件**\n" + clip("\n".join(lines), 700))
            )
        _, s.undo_id = ms.undo_preview(
            st,
            p,
            include_children=p.kind == "server",
            admin_gid=p.guild_id if self.role == "admin" else None,
        )
        self.body.add_item(
            action_row(
                self.button(
                    f"保存（{len(s.draft)}件）",
                    "save",
                    disabled=not s.draft or bool(st.overlay_error),
                    style=discord.ButtonStyle.success,
                ),
                self.button("下書きを破棄", "discard", disabled=not s.draft),
                self.button(
                    "直前の変更を取り消す",
                    "undo",
                    disabled=not s.undo_id or bool(s.draft) or bool(st.overlay_error),
                ),
            )
        )

    def editor(self):
        s, ctx, p = self.screen, self.ctx, self.place
        scopes = [("全体の初期設定", "defaults"), ("DM", "dms")]
        if p.guild_id:
            scopes += [
                (
                    ms.place_name(ms.Place.server(p.guild_id), ctx.names),
                    f"server:{p.guild_id}",
                ),
                ("このサーバーのチャンネルを選ぶ…", "channels"),
            ]
        if p.kind == "channel":
            scopes += [(ms.place_name(p, ctx.names), s.place_text)]
        if self.role == "owner":
            scopes += [("別のサーバーを選ぶ…", "servers")]
        else:
            scopes = [
                (label, val)
                for label, val in scopes
                if val == "channels" or val.startswith(("server:", "channel:"))
            ]
        self.select("scope", self.options(scopes, s.place_text), "① 適用先")
        keys = available(ctx, p, self.role)
        cats = [
            (label, cat)
            for cat, (label, items) in CATEGORIES.items()
            if any(k in keys for k in items)
        ]
        if s.category not in dict((v, k) for k, v in cats):
            s.category = cats[0][1]
        self.select("category", self.options(cats, s.category), "② 分類")
        choices = [k for k in CATEGORIES[s.category][1] if k in keys]
        if s.key not in choices:
            s.key = choices[0]
        self.select("key", self.options([(TITLES[k], k) for k in choices], s.key), "③ 設定項目")
        if self.role == "owner" and p.kind == "server" and s.category == "reply":
            self.select(
                "preset",
                self.options(
                    [
                        ("友人向け", "friends"),
                        ("コミュニティ向け", "community"),
                        ("仕事向け", "work"),
                    ]
                ),
                "サーバー全体のプリセット（任意）",
            )
        key, st = s.key, ctx.settings
        path = ms.place_path(st, p, key)
        current, source = self.setting_source(key, path)
        inherited = st.value_without(p.scope(key), key, path)
        if path == ("all_servers",):
            self.body.add_item(
                ui.TextDisplay("サーバー共通の初期設定です。DMは適用先「DM」で変更します。")
            )
        self.body.add_item(ui.Separator())
        self.body.add_item(
            ui.TextDisplay(
                f"### {TITLES[key]}\n現在：**{value_text(current)}**"
                f"\n-# 設定元：{text(source, 180)}\n継承する値：{value_text(inherited)}"
            )
        )
        if key in s.draft:
            new = "上の設定を継承" if s.draft[key] == "inherit" else value_text(s.draft[key])
            self.body.add_item(ui.TextDisplay(f"✎ 保存後：**{new}**"))
        self.control(key)
        buttons = [self.button("上の設定を継承", "inherit")]
        if p.kind != "defaults":
            buttons.append(self.button("返信しなかった理由", "why"))
        self.body.add_item(action_row(*buttons))
        self.edit_footer()

    def control(self, key):
        spec = self.ctx.settings.spec(key)
        val = self.screen.draft.get(key, current_value(self.ctx.settings, self.place, key))
        if spec.type in ("choice", "on-off", "people"):
            vals = (True, False) if spec.type == "on-off" else spec.values
            if self.role == "admin":
                vals = (
                    ("when-addressed", "off")
                    if key == "who.replies"
                    else (False,)
                    if spec.type == "on-off"
                    else ()
                )
            if vals:
                self.select(
                    "set",
                    [
                        discord.SelectOption(
                            label=clip(value_text(v), 100),
                            value=json.dumps(v),
                            default=v == val,
                        )
                        for v in vals
                    ],
                    "新しい値を選択",
                )
        if spec.type in ("number", "text", "people") or spec.ids:
            self.body.add_item(
                action_row(
                    self.button(
                        "値を入力…" if spec.type in ("number", "text") else "IDのリストを編集…",
                        "modal",
                        style=discord.ButtonStyle.primary,
                    )
                )
            )
        if (spec.type == "people" or spec.ids) and self.place.guild_id == self.interaction_gid:
            picker = ui.UserSelect if spec.ids else ui.MentionableSelect
            self.body.add_item(
                action_row(
                    Action(
                        picker(
                            custom_id=f"{PREFIX}{self.screen.token}:people",
                            placeholder="相手を選んでリストを置換（最大25件）",
                            min_values=0,
                            max_values=25,
                        )
                    )
                )
            )

    def confirmation(self):
        s = self.screen
        self.body.add_item(ui.TextDisplay("### 変更内容の確認"))
        if "undo" in s.pending:
            plan = ms.undo_plan(
                self.ctx.settings,
                self.place,
                self.place.kind == "server",
                self.place.guild_id if self.role == "admin" else None,
            )
            lines = ms.undo_lines(self.ctx.settings, plan, self.ctx.names)
        else:
            lines = [
                f"{TITLES[k]} → {'継承' if v == 'inherit' else value_text(v)}"
                for k, v in s.draft.items()
            ]
        self.body.add_item(ui.TextDisplay(clip("\n".join(f"• {line}" for line in lines), 1200)))
        warnings = s.pending.get("warnings", [])
        for warning in warnings[:5]:
            self.body.add_item(ui.TextDisplay("⚠ " + text(warning, 350)))
        if len(warnings) > MAX_CONFIRMATION_WARNINGS:
            self.body.add_item(ui.TextDisplay("警告が多いため、変更を少数ずつ保存してください。"))
        self.body.add_item(
            action_row(
                self.button(
                    "確認して適用",
                    "confirm",
                    disabled=len(warnings) > MAX_CONFIRMATION_WARNINGS,
                    style=discord.ButtonStyle.danger,
                ),
                self.button("編集に戻る", "back"),
            )
        )

    def browse(self):
        s, p = self.screen, self.place
        if s.browse == "servers":
            entries = [(g["name"], f"server:{g['id']}") for g in self.ctx.guilds()]
        else:
            entries = [
                ("#" + c["name"], f"channel:{p.guild_id}:{c['id']}")
                for c in self.ctx.channels(p.guild_id)
                if c.get("type") != ms.CATEGORY_TYPE and c.get("readable", True)
            ]
        entries.sort(key=lambda x: (x[0], x[1]))
        pages = max(1, (len(entries) + 24) // 25)
        s.page = min(s.page, pages - 1)
        if entries:
            self.select(
                "scope",
                self.options(entries[s.page * 25 : (s.page + 1) * 25]),
                f"適用先を選択 · {s.page + 1}/{pages}",
            )
        else:
            self.body.add_item(ui.TextDisplay("選択できる適用先がありません。"))
        # Two navigation actions need distinct custom_ids in the same view.
        self.body.add_item(
            action_row(
                self.button("前へ", "previous", disabled=s.page == 0),
                self.button("次へ", "next", disabled=s.page == pages - 1),
                self.button("戻る", "back"),
            )
        )

    def diagnostics(self):
        self.body.add_item(
            ui.TextDisplay(
                "### 返信しなかった理由\n記録された当時の理由です。"
                "設定変更後も過去のメッセージへ自動返信はしません。"
            )
        )
        if self.place.kind == "dms":
            self.body.add_item(
                ui.TextDisplay(
                    "許可されていない相手からのDMは記録しません。「利用者・権限」でDMの許可設定を確認してください。"
                )
            )
        outcomes = self.ctx.recent_outcomes(self.place, 50)
        refused = [
            o for o in outcomes if o.get("status") in ("skipped", "quarantined", "enqueue_failed")
        ][:5]
        for o in refused:
            self.body.add_item(ui.TextDisplay(text(outcome_text(o), 450)))
        if not refused:
            self.body.add_item(
                ui.TextDisplay(
                    "最近の記録はありません。Discordの閲覧・送信権限も確認してください。"
                )
            )
        self.body.add_item(action_row(self.button("設定に戻る", "back")))


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

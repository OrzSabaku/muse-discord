"""User-bound drafts, authorization, concurrent-edit detection and save/undo decisions."""

import asyncio
import json
import re
import secrets
from dataclasses import dataclass, field

from muse_discord import muse_settings as ms
from muse_discord.settings_assessment import assess
from muse_discord.settings_presets import preset_changes
from muse_discord.settings_views.contracts import MAX_CONFIRMATION_WARNINGS, Pending
from muse_discord.settings_views.labels import CATEGORIES


def current_value(st, place, key):
    return st.top(key) if key in ms.TOP else st.get(key, place.scope(key))


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

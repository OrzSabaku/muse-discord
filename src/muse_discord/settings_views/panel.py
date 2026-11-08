"""Compose editor, browser, confirmation and diagnostic settings panels."""

import json
from typing import cast

import discord
from discord import ui

from muse_discord import muse_settings as ms
from muse_discord.settings_display import clip
from muse_discord.settings_views.contracts import MAX_CONFIRMATION_WARNINGS, PREFIX
from muse_discord.settings_views.controls import Action
from muse_discord.settings_views.labels import CATEGORIES, TITLES
from muse_discord.settings_views.model import available, current_value
from muse_discord.settings_views.presentation import outcome_text, text, value_text


def action_row(*children: ui.Item[ui.View | ui.LayoutView]) -> ui.ActionRow[ui.LayoutView]:
    # DynamicItem is typed for either kind of view; these items belong to
    # this module's LayoutView. Keep the SDK's type adaptation in one place.
    items = cast(tuple[ui.Item[ui.LayoutView], ...], children)
    return ui.ActionRow[ui.LayoutView](*items)


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

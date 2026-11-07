"""Native Discord UI: real components and engine, offline interactions."""

import json
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness as H
from muse_discord import settings_ui as UI

MS = H.MS


def interaction(ctx, user=H.OWNER, gid=H.G1, manage=False, data=None):
    response = types.SimpleNamespace(
        defer=AsyncMock(),
        send_message=AsyncMock(),
        send_modal=AsyncMock(),
        is_done=lambda: False,
    )

    def factory():
        ctx.refresh()
        return ctx

    return types.SimpleNamespace(
        user=types.SimpleNamespace(id=int(user)),
        guild_id=int(gid) if gid else None,
        channel_id=int(H.MUSECHAT),
        channel=types.SimpleNamespace(id=int(H.MUSECHAT)),
        permissions=types.SimpleNamespace(manage_guild=manage),
        data=data or {},
        response=response,
        client=types.SimpleNamespace(
            settings_context=factory,
            settings_guard=lambda *a: None,
            settings_log=ctx.log,
        ),
        followup=types.SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
    )


def components(view, kind):
    items = [i.item if isinstance(i, UI.Action) else i for i in view.walk_children()]
    return [i for i in items if isinstance(i, kind)]


def panel_text(view):
    return "\n".join(c.content for c in components(view, UI.ui.TextDisplay))


class NativeUI(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ctx = H.FakeCtx()
        self.screen = UI.Screen(H.OWNER, f"server:{H.G1}")

    def act(self, action, values=(), role="owner"):
        self.ctx.refresh()
        out = self.screen.act(self.ctx, role, action, values)
        self.ctx.refresh()
        return out

    def panel(self, role="owner"):
        self.ctx.refresh()
        return UI.Panel(self.screen, self.ctx, role, interaction_gid=H.G1)

    async def test_entry_is_private_and_uses_native_components(self):
        ix = interaction(self.ctx)
        await UI.open_settings(ix, self.ctx)
        ix.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        view = ix.edit_original_response.call_args.kwargs["view"]
        self.assertIsInstance(view, UI.ui.LayoutView)
        self.assertIn("継承する値", panel_text(view))
        self.assertLessEqual(len(list(view.walk_children())), 40)
        self.assertLessEqual(view.content_length(), 4000)
        self.assertEqual(view.to_components()[0]["type"], 17)

    async def test_regular_member_is_refused(self):
        ix = interaction(self.ctx, H.MEMBER)
        await UI.open_settings(ix, self.ctx)
        self.assertNotIn("view", ix.edit_original_response.call_args.kwargs)
        self.assertIn("所有者", ix.edit_original_response.call_args.kwargs["content"])

    async def test_native_callback_stages_then_save_applies_one_group(self):
        self.ctx.session_put(H.OWNER, self.screen.token, self.screen)
        self.screen.key = "style.length"
        for val in ["brief", "detailed"]:
            item = UI.Action(
                UI.ui.Select(
                    custom_id=f"mset:{self.screen.token}:set",
                    options=[UI.discord.SelectOption(label=val, value=json.dumps(val))],
                )
            )
            ix = interaction(self.ctx, data={"values": [json.dumps(val)]})
            await item.callback(ix)
            self.assertEqual(self.ctx.value("style.length", MS.Place.server(H.G1)), "normal")
        self.assertEqual(self.screen.draft, {"style.length": "detailed"})
        self.screen.key = "limits.per_day"
        self.act("input", ["73"])
        self.assertIn("2件", panel_text(self.panel()))
        self.act("save")
        self.assertEqual(len(self.ctx.applied[-1][0]), 2)
        self.assertEqual(self.ctx.value("limits.per_day", MS.Place.server(H.G1)), 73)
        self.assertEqual(self.screen.draft, {})

    async def test_discard_and_scope_change_do_not_lose_unsaved_edits(self):
        self.screen.key = "style.length"
        self.act("set", ['"brief"'])
        with self.assertRaises(MS.SettingsError):
            self.act("scope", ["dms"])
        self.assertEqual(self.screen.place_text, f"server:{H.G1}")
        self.act("discard")
        self.act("scope", ["dms"])
        self.assertEqual(self.ctx.value("style.length", MS.Place.server(H.G1)), "normal")
        self.assertEqual(self.screen.draft, {})

    async def test_exact_number_null_and_invalid_input(self):
        self.screen.key = "limits.per_day"
        for raw in ["true", '"12"', "-1", "100001", "1.5"]:
            with self.assertRaises((MS.SettingsError, ValueError)):
                self.act("input", [raw])
        self.act("input", ["73"])
        self.act("save")
        self.act("input", [""])
        self.assertIsNone(self.screen.draft["limits.per_day"])
        self.act("save")
        if self.screen.pending:
            self.act("confirm")
        self.assertIsNone(self.ctx.value("limits.per_day", MS.Place.server(H.G1)))

    async def test_modal_uses_actual_value_and_stages(self):
        self.screen.key = "style.notes"
        self.ctx.session_put(H.OWNER, self.screen.token, self.screen)
        modal = UI.ValueModal(self.screen, self.ctx)
        self.assertEqual(modal.input.default, "Friendly community. Playful is fine.")
        modal.input._value = "短く日本語で"
        await modal.on_submit(interaction(self.ctx))
        self.assertEqual(self.screen.draft["style.notes"], "短く日本語で")
        self.assertNotEqual(self.ctx.value("style.notes", MS.Place.server(H.G1)), "短く日本語で")

    async def test_modal_refuses_expired_or_changed_setting(self):
        self.screen.key = "style.notes"
        modal = UI.ValueModal(self.screen, self.ctx)
        await modal.on_submit(interaction(self.ctx))
        self.assertFalse(self.screen.draft)
        self.ctx.session_put(H.OWNER, self.screen.token, self.screen)
        self.screen.key = "limits.per_day"
        await modal.on_submit(interaction(self.ctx))
        self.assertFalse(self.screen.draft)

    async def test_risky_save_requires_confirmation(self):
        self.screen.key = "privacy.memory"
        self.act("set", ['"everything"'])
        self.act("save")
        self.assertIn("warnings", self.screen.pending)
        self.assertTrue(self.screen.pending["warnings"])
        self.assertEqual(self.ctx.value("privacy.memory", MS.Place.server(H.G1)), "public-only")
        self.assertIn("確認して適用", [c.label for c in components(self.panel(), UI.ui.Button)])
        self.act("confirm")
        self.assertEqual(self.ctx.value("privacy.memory", MS.Place.server(H.G1)), "everything")
        with self.assertRaises(MS.SettingsError):
            self.act("confirm")

    async def test_editing_after_confirmation_invalidates_it(self):
        self.screen.key = "privacy.memory"
        self.act("set", ['"everything"'])
        self.act("save")
        self.screen.key = "style.length"
        self.act("set", ['"brief"'])
        with self.assertRaises(MS.SettingsError):
            self.act("confirm")
        self.assertFalse(self.ctx.applied)

    async def test_changed_config_is_not_overwritten(self):
        self.screen.key = "style.length"
        self.act("set", ['"brief"'])
        MS.apply(
            self.ctx.cfg,
            [MS.Change(MS.Place.server(H.G1), "style.length", "detailed")],
            H.OWNER,
            role="owner",
        )
        with self.assertRaises(MS.SettingsError):
            self.act("save")
        self.assertEqual(self.ctx.value("style.length", MS.Place.server(H.G1)), "detailed")

    async def test_inherit_and_undo_preserve_engine_semantics(self):
        self.screen.key = "style.length"
        self.act("set", ['"brief"'])
        self.act("save")
        self.act("inherit")
        self.act("save")
        self.assertEqual(self.ctx.value("style.length", MS.Place.server(H.G1)), "normal")
        self.panel()
        self.act("undo")
        self.assertIn("undo", self.screen.pending)
        self.act("confirm")
        self.assertEqual(self.ctx.value("style.length", MS.Place.server(H.G1)), "brief")

    async def test_stale_undo_is_refused(self):
        self.panel()
        MS.apply(
            self.ctx.cfg,
            [MS.Change(MS.Place.server(H.G1), "style.length", "brief")],
            H.OWNER,
            role="owner",
        )
        with self.assertRaises(MS.SettingsError):
            self.act("undo")

    async def test_admin_can_only_quiet_their_server_and_gets_digest(self):
        self.screen.uid = H.ADMIN
        self.screen.authorize(interaction(self.ctx, H.ADMIN, manage=True), self.ctx)
        self.screen.key = "who.replies"
        for raw in ['"freely"', '"conversational"']:
            with self.assertRaises(MS.SettingsError):
                self.act("set", [raw], "admin")
        self.act("set", ['"off"'], "admin")
        self.act("save", role="admin")
        self.assertTrue(self.ctx.notified)
        with self.assertRaises(MS.SettingsError):
            self.act("scope", [f"server:{H.G2}"], "admin")
        self.screen.key = "privacy.memory"
        with self.assertRaises(MS.SettingsError):
            self.act("set", ['"everything"'], "admin")

    async def test_revoked_owner_and_cross_user_cannot_click(self):
        with self.assertRaises(MS.SettingsError):
            self.screen.authorize(interaction(self.ctx, H.OWNER2), self.ctx)
        self.ctx.settings.owners.clear()
        with self.assertRaises(MS.SettingsError):
            self.screen.authorize(interaction(self.ctx), self.ctx)

    async def test_expired_and_wrong_user_dynamic_buttons_write_nothing(self):
        item = UI.Action(UI.ui.Button(label="保存", custom_id=f"mset:{self.screen.token}:save"))
        for user in [H.OWNER, H.OWNER2]:
            ix = interaction(self.ctx, user)
            await item.callback(ix)
            ix.response.send_message.assert_awaited_once()
        self.assertFalse(self.ctx.applied)
        self.ctx.session_put(H.OWNER, self.screen.token, self.screen)
        ix = interaction(self.ctx, H.OWNER2)
        await item.callback(ix)
        self.assertFalse(self.ctx.applied)

    async def test_people_picker_and_all_editable_schema_fields(self):
        self.screen.key = "who.ignore"
        self.act("people", [[H.SAM, H.ROLE], {H.ROLE: {}}])
        self.assertEqual(self.screen.draft["who.ignore"], [H.SAM, "role:" + H.ROLE])
        for place in [
            "defaults",
            "dms",
            f"server:{H.G1}",
            f"channel:{H.G1}:{H.MUSECHAT}",
        ]:
            screen = UI.Screen(H.OWNER, place)
            keys = UI.available(self.ctx, screen.place(self.ctx), "owner")
            for category, (_, candidates) in UI.CATEGORIES.items():
                for key in set(candidates) & set(keys):
                    screen.category, screen.key = category, key
                    view = UI.Panel(screen, self.ctx, "owner", interaction_gid=H.G1)
                    self.assertEqual(screen.key, key)
                    self.assertLessEqual(view.content_length(), 4000)
                    self.assertLessEqual(len(list(view.walk_children())), 40)
                    ids = [i.custom_id for i in view.walk_children() if hasattr(i, "custom_id")]
                    self.assertEqual(len(ids), len(set(ids)))
                    self.assertTrue(all(len(i) <= 100 for i in ids))

    async def test_pagination_reaches_every_server_and_channel(self):
        for kind, expected in [
            ("servers", len(self.ctx.guilds())),
            (
                "channels",
                len([c for c in self.ctx.channels(H.G1) if c["type"] != 4 and c["readable"]]),
            ),
        ]:
            self.screen.browse, self.screen.page = kind, 0
            seen = set()
            for _ in range(10):
                view = self.panel()
                for sel in components(view, UI.ui.Select):
                    seen.update(o.value for o in sel.options)
                    self.assertLessEqual(len(sel.options), 25)
                next_ = next(b for b in components(view, UI.ui.Button) if b.label == "次へ")
                if next_.disabled:
                    break
                self.act("next")
            self.assertEqual(len(seen), expected)

    async def test_hidden_category_unknown_channel_and_broken_overlay(self):
        for val in [f"channel:{H.G1}:{H.CAT}", f"channel:{H.G1}:{H.HIDDEN}"]:
            s = UI.Screen(H.OWNER, val)
            with self.assertRaises(MS.SettingsError):
                s.place(self.ctx)
        self.ctx = H.FakeCtx(broken_overlay=True)
        self.screen.key = "style.length"
        self.act("set", ['"brief"'])
        with self.assertRaises(MS.SettingsError):
            self.act("save")
        self.assertIn("読めません", panel_text(self.panel()))

    async def test_why_message_cannot_read_another_guilds_ledger(self):
        outcome = next(o for o in self.ctx.outcomes if o.get("guild_id") == H.G1)
        ix = interaction(self.ctx, gid=H.G2, data={"target_id": outcome["message_id"]})
        await UI.why_message(ix, self.ctx)
        self.assertIn("記録はありません", ix.edit_original_response.call_args.kwargs["content"])

    async def test_guard_checks_the_native_payload_before_send(self):
        ix = interaction(self.ctx)

        def blocked(*args):
            raise RuntimeError("blocked")

        ix.client.settings_guard = blocked
        with self.assertRaises(RuntimeError):
            await UI.redraw(ix, self.screen)
        ix.edit_original_response.assert_not_awaited()

    async def test_channel_notes_edit_only_local_instructions(self):
        self.screen.place_text = f"channel:{H.G1}:{H.MUSECHAT}"
        self.screen.key = "style.notes"
        modal = UI.ValueModal(self.screen, self.ctx)
        self.assertEqual(modal.input.default, "")
        self.act("input", ["このチャンネルでは日本語で"])
        self.act("save")
        value = self.ctx.value("style.notes", MS.Place.channel(H.G1, H.MUSECHAT))
        self.assertEqual(value.count("Friendly community"), 1)
        self.assertEqual(
            UI.ValueModal(self.screen, self.ctx).input.default,
            "このチャンネルでは日本語で",
        )

    async def test_many_warnings_cannot_be_confirmed_without_being_shown(self):
        self.screen.pending = {"warnings": [f"warning {n}" for n in range(6)]}
        view = self.panel()
        confirm = next(b for b in components(view, UI.ui.Button) if b.label == "確認して適用")
        self.assertTrue(confirm.disabled)
        with self.assertRaises(MS.SettingsError):
            self.act("confirm")

    async def test_long_untrusted_names_fit_discord_limits_without_mentions(self):
        self.ctx.names = lambda _: "@everyone **name** " + "x" * 4000
        for category in UI.CATEGORIES:
            self.screen.category = category
            view = self.panel()
            self.assertLessEqual(view.content_length(), 4000)
            self.assertNotIn("@everyone", panel_text(view))
            self.assertLessEqual(len(list(view.walk_children())), 40)

    async def test_server_preset_is_staged_and_admin_cannot_apply_it(self):
        self.act("preset", ["community"])
        self.assertTrue(self.screen.draft)
        self.assertEqual(self.ctx.value("who.replies", MS.Place.server(H.G1)), "conversational")
        self.act("save")
        if self.screen.pending:
            self.act("confirm")
        self.assertEqual(self.ctx.value("who.replies", MS.Place.server(H.G1)), "when-addressed")
        with self.assertRaises(MS.SettingsError):
            self.act("preset", ["work"], "admin")


if __name__ == "__main__":
    unittest.main()

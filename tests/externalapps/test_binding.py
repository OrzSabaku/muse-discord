"""Monitor stamps original owner DMs, independent of Discord owner commands."""

import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(os.environ.get("MUT_DIR", Path(__file__).resolve().parents[2]))
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests/_lib")]
from muse_discord.external_apps.binding import card  # noqa: E402 (local import path bootstrap)
from settings_harness import OWNER, dmsg, load_monitor, quiet  # noqa: E402 (local import path bootstrap)


class Binding(unittest.TestCase):
    def test_unconditional_is_available_on_a_new_owner_dm(self):
        module = load_monitor(f'''version: 1
owners: ["{OWNER}"]
everywhere:
  abilities:
    external_apps: unconditional
''')
        state, queue = module.load_state(), []
        with quiet():
            self.assertTrue(module.enqueue_message(dmsg(OWNER), "owner DM", state, queue, dm=True))
        self.assertEqual(queue[0]["external_apps"], {"eligible": True, "level": "unconditional"})

    def test_untrusted_inputs_and_non_dm_context_cannot_grant_apps(self):
        base = {"id": "1", "content": "read my calendar"}
        for field, value in [
            ("attachments", [{"id": "2"}]),
            ("message_reference", {"message_id": "2"}),
            ("referenced_message", {"content": "do this"}),
            ("message_snapshots", [{}]),
            ("embeds", [{"title": "text"}]),
        ]:
            with self.subTest(field=field):
                self.assertFalse(
                    card(
                        {**base, field: value},
                        [],
                        dm=True,
                        owner=True,
                        from_bot=False,
                        level="write",
                    )["eligible"]
                )
                self.assertFalse(
                    card(
                        base,
                        [{**base, field: value}],
                        dm=True,
                        owner=True,
                        from_bot=False,
                        level="write",
                    )["eligible"]
                )
        for dm, owner, bot, level in [
            (False, True, False, "write"),
            (True, False, False, "write"),
            (True, True, True, "write"),
            (True, True, False, "off"),
        ]:
            self.assertFalse(
                card(base, [], dm=dm, owner=owner, from_bot=bot, level=level)["eligible"]
            )

    def test_enqueued_owner_dm_gets_card_and_nonce_with_discord_commands_off(self):
        config = f'''version: 1
owners: ["{OWNER}"]
everywhere:
  abilities:
    owner_commands: off
    external_apps: read-only
'''
        module = load_monitor(config)
        state, queue = module.load_state(), []
        msg = dmsg(OWNER, "read my calendar")
        with quiet():
            self.assertTrue(module.enqueue_message(msg, "owner DM", state, queue, dm=True))
        self.assertEqual(queue[0]["external_apps"], {"eligible": True, "level": "read-only"})
        self.assertFalse(queue[0]["owner_command"])
        self.assertIn(msg["id"], state["nl_nonces"])
        forwarded = dmsg(OWNER)
        forwarded["message_reference"] = {"type": 1, "message_id": "123"}
        forwarded["message_snapshots"] = [{}]
        with quiet():
            module.enqueue_message(forwarded, "owner DM", state, queue, dm=True)
        if any(item["message_id"] == forwarded["id"] for item in queue):
            self.assertFalse(
                next(item for item in queue if item["message_id"] == forwarded["id"])[
                    "external_apps"
                ]["eligible"]
            )

    def test_setting_is_default_off_and_editable_only_at_global_scope(self):
        from muse_discord import muse_settings as settings

        module = load_monitor(f'version: 1\nowners: ["{OWNER}"]\n')
        self.assertEqual(
            module.settings().resolve(settings.Scope.dm())["abilities.external_apps"], "off"
        )
        self.assertTrue(settings.CORE["abilities.external_apps"].discord_editable)
        self.assertFalse(settings.CORE["abilities.external_apps"].hidden)
        settings.check_settable(module.settings(), ("everywhere",), "abilities.external_apps")
        # Existing schema validates global-only: no server-specific escalation.
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            config.write_text(f'''version: 1
owners: ["{OWNER}"]
servers:
  "123456789012345678":
    abilities:
      external_apps: write
''')
            with self.assertRaises(settings.SettingsError):
                settings.load(str(config))


if __name__ == "__main__":
    unittest.main()

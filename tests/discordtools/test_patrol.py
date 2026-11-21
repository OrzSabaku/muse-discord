"""Saved patrol permissions constrain shared CLI dispatch and message operations."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(os.environ.get("MUT_DIR", Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(ROOT / "src"))
from muse_discord.discord_tools.cli import JSON_COMMANDS, CommandCLI  # noqa: E402
from muse_discord.discord_tools.patrol import create_patrol_cli, main  # noqa: E402
from muse_discord.discord_tools.session import ToolSession  # noqa: E402
from muse_discord.discord_tools.state import read_state  # noqa: E402

CHANNEL = "200000000000000001"
OTHER = "200000000000000002"
ITEM = "300000000000000001"
BOT = "100000000000000001"
ALL_OPERATIONS = ["list-unread", "get-messages", "reply-message", "send-message", "mark-read"]


class Patrol(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = {
            "version": 1,
            "profiles": {
                "morning": {
                    "read_state": "patrol-morning.sqlite",
                    "channels": {CHANNEL: ALL_OPERATIONS, OTHER: ["get-messages"]},
                },
                "evening": {
                    "read_state": "patrol-evening.sqlite",
                    "channels": {CHANNEL: ["list-unread", "mark-read"]},
                },
            },
        }
        self.channel = {"id": CHANNEL, "guild_id": "500000000000000001", "type": 0}
        self.calls = []
        self.addCleanup(patch.stopall)
        patch.object(ToolSession, "load_token", return_value="test-token").start()
        patch.object(ToolSession, "api", side_effect=self.api).start()
        self.save()

    def save(self):
        (self.root / "patrol.yaml").write_text(yaml.safe_dump(self.config))

    def api(self, method, path, data=None):
        self.calls.append((method, path, data))
        if path == f"/channels/{CHANNEL}":
            return self.channel
        if path == f"/channels/{OTHER}":
            return {**self.channel, "id": OTHER}
        if path == "/users/@me":
            return {"id": BOT}
        if "/messages?" in path:
            return [{"id": ITEM, "content": "hello", "author": {"id": OTHER}}]
        return {"id": ITEM}

    def run_command(self, *args, profile="morning"):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = main(str(self.root), ["discord-patrol.py", profile, *args])
        return result, json.loads(output.getvalue()) if output.getvalue() else None

    def test_allowed_reads_reply_send_and_ack_use_shared_tools(self):
        self.assertEqual(self.run_command("get-messages", CHANNEL)[1]["messages"][0]["id"], ITEM)
        self.assertTrue(self.run_command("list-unread", CHANNEL)[1]["initialized"])
        self.assertEqual(self.run_command("reply-message", CHANNEL, ITEM, "reply")[0], 0)
        payload = self.calls[-1][2]
        self.assertEqual(payload["message_reference"]["message_id"], ITEM)
        self.assertFalse(payload["allowed_mentions"]["replied_user"])
        self.assertEqual(self.run_command("send-message", CHANNEL, "hello")[0], 0)
        self.assertEqual(self.run_command("mark-read", CHANNEL, ITEM)[0], 0)
        self.assertFalse((self.root / "read-state.sqlite").exists())

    def test_channel_and_operation_are_checked_together_before_message_io(self):
        for command, args in [
            ("send-message", (OTHER, "no")),
            ("reply-message", (OTHER, ITEM, "no")),
            ("mark-read", (OTHER, ITEM)),
            ("get-messages", ("200000000000000003",)),
        ]:
            with self.subTest(command=command):
                self.calls.clear()
                self.assertEqual(self.run_command(command, *args)[0], 1)
                self.assertEqual(self.calls, [])
        self.assertEqual(self.run_command("get-messages", OTHER)[0], 0)

    def test_every_other_owner_command_is_refused_before_io(self):
        cli = create_patrol_cli(str(self.root), "morning")
        for command, (_, min_args) in cli.commands.items():
            if command in ALL_OPERATIONS:
                continue
            with (
                self.subTest(command=command),
                contextlib.redirect_stderr(io.StringIO()),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.calls.clear()
                argv = ["patrol", command, *([CHANNEL] * min_args)]
                if command in JSON_COMMANDS:
                    self.assertEqual(cli.main(argv), 1)
                else:
                    with self.assertRaises(SystemExit):
                        cli.main(argv)
                self.assertEqual(self.calls, [])

    def test_dm_group_dm_and_unverified_channel_responses_fail_closed(self):
        for channel in [
            {"id": CHANNEL, "type": 1},
            {"id": CHANNEL, "type": 3},
            {"id": CHANNEL, "type": 0},
            {**self.channel, "type": 999},
            {**self.channel, "id": OTHER},
            {**self.channel, "type": 4},
            None,
            {},
        ]:
            with self.subTest(channel=channel):
                self.channel = channel
                for command, args in [
                    ("get-messages", (CHANNEL,)),
                    ("send-message", (CHANNEL, "no")),
                    ("mark-read", (CHANNEL, ITEM)),
                ]:
                    self.calls.clear()
                    self.assertEqual(self.run_command(command, *args)[0], 1)
                    self.assertEqual(self.calls, [("GET", f"/channels/{CHANNEL}", None)])
        self.assertFalse((self.root / "patrol-morning.sqlite").exists())

    def test_channel_lookup_failure_never_sends(self):
        with patch.object(ToolSession, "api", side_effect=SystemExit("unavailable")):
            self.assertEqual(self.run_command("send-message", CHANNEL, "no")[0], 1)

    def test_explicit_guild_thread_is_allowed(self):
        self.channel["type"] = 11
        self.assertEqual(self.run_command("reply-message", CHANNEL, ITEM, "reply")[0], 0)

    def test_owner_bindings_are_refused_even_with_empty_values(self):
        for option in ("--for", "--nonce"):
            for value in ("", ITEM):
                with self.subTest(option=option, value=value):
                    self.assertEqual(self.run_command(option, value, "get-messages", CHANNEL)[0], 1)
                    self.assertEqual(self.calls, [])

    def test_profiles_keep_state_separate_and_resolve_paths_from_package(self):
        self.run_command("mark-read", CHANNEL, ITEM)
        result = self.run_command("list-unread", CHANNEL, profile="evening")[1]
        self.assertTrue(result["initialized"])
        for name in ("morning", "evening"):
            with read_state(self.root / f"patrol-{name}.sqlite") as state:
                self.assertEqual(state.get(CHANNEL), ITEM)

    def test_missing_unknown_or_malformed_configuration_never_reads_credentials(self):
        invalid = [
            "[broken",
            "null",
            "version: 2\nprofiles: {}",
            yaml.safe_dump({**self.config, "unexpected": True}),
        ]
        for field, value in [
            ("channels", {CHANNEL: ["delete-own"]}),
            ("channels", {CHANNEL: []}),
            ("channels", {}),
            ("channels", {"not-an-id": ["get-messages"]}),
            ("read_state", ""),
            ("read_state", None),
        ]:
            profile = {**self.config["profiles"]["morning"], field: value}
            invalid.append(yaml.safe_dump({"version": 1, "profiles": {"morning": profile}}))
        with patch.object(ToolSession, "load_token") as token:
            with self.assertRaises(SystemExit):
                create_patrol_cli(str(self.root), "unknown")
            for contents in invalid:
                (self.root / "patrol.yaml").write_text(contents)
                with self.subTest(contents=contents), self.assertRaises(SystemExit):
                    create_patrol_cli(str(self.root), "morning")
            (self.root / "patrol.yaml").unlink()
            with self.assertRaises(SystemExit):
                create_patrol_cli(str(self.root), "morning")
            token.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_saved_grant_does_not_authorize_owner_cli(self):
        cli = CommandCLI(ToolSession(str(self.root)))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["owner", "get-messages", CHANNEL]), 1)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()

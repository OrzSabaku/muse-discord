"""Discord permission rules on fresh snapshots, plus fail-closed watch policy."""

import os
import sys
import subprocess
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_watches as H
from common import MUT_DIR  # noqa: E402 (local import path bootstrap)
from muse_discord.discord_permissions import channel_permissions, guild_permissions

G, R1, R2, U, OWNER = "100", "200", "201", "300", "400"
VIEW, SEND, ADMIN = 1 << 10, 1 << 11, 1 << 3


def overwrite(id_, allow=0, deny=0, type_=0):
    return {"id": id_, "type": type_, "allow": str(allow), "deny": str(deny)}


class PermissionRules(unittest.TestCase):
    def test_adapter_imports_and_resolves_in_a_fresh_interpreter(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from muse_discord.discord_permissions import channel_permissions; "
                "print(channel_permissions('100', {'100': 1024}, None, '300', [], []))",
            ],
            env=dict(os.environ, PYTHONPATH=os.path.join(MUT_DIR, "src")),
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "1024")

    def test_role_allow_wins_combined_role_deny_then_member_deny_wins(self):
        roles = {G: VIEW | SEND, R1: 0, R2: 0}
        ows = [overwrite(R1, allow=VIEW), overwrite(R2, deny=VIEW), overwrite(G, deny=VIEW)]
        bits = channel_permissions(G, roles, OWNER, U, [R1, R2], ows)
        self.assertTrue(bits & VIEW)
        ows.append(overwrite(U, deny=VIEW, type_=1))
        self.assertFalse(channel_permissions(G, roles, OWNER, U, [R1, R2], ows) & VIEW)

    def test_admin_and_guild_owner_bypass_channel_overwrites(self):
        for roles, uid, role_ids in [({G: 0, R1: ADMIN}, U, [R1]), ({G: 0}, OWNER, [])]:
            bits = channel_permissions(G, roles, OWNER, uid, role_ids, [overwrite(G, deny=VIEW)])
            self.assertTrue(bits & VIEW)
            self.assertTrue(bits & SEND)
            self.assertTrue(guild_permissions(G, roles, OWNER, uid, role_ids) & ADMIN)

    def test_everyone_projection_ignores_member_and_other_role_grants(self):
        self.assertFalse(
            channel_permissions(
                G,
                {G: 0, R1: 0},
                OWNER,
                None,
                [],
                [overwrite(R1, allow=VIEW), overwrite(U, allow=VIEW, type_=1)],
            )
            & VIEW
        )
        self.assertTrue(channel_permissions(G, {G: VIEW}, OWNER, None, [], []) & VIEW)

    def test_missing_default_role_does_not_grant_permissions(self):
        self.assertEqual(channel_permissions(G, {R1: VIEW}, OWNER, U, [R1], []), 0)

    def test_cached_guild_is_rebuilt_after_role_or_owner_changes(self):
        self.assertFalse(channel_permissions(G, {G: 0, R1: 0}, OWNER, U, [R1], []) & VIEW)
        self.assertTrue(channel_permissions(G, {G: 0, R1: VIEW}, OWNER, U, [R1], []) & VIEW)
        self.assertTrue(channel_permissions(G, {G: 0}, U, U, [], []) & VIEW)
        self.assertFalse(channel_permissions(G, {G: 0}, OWNER, U, [], []) & VIEW)


class WatchPermissionPolicy(H.Base):
    def test_deleted_role_no_longer_grants_view_even_with_cached_membership(self):
        m = self.mod
        self.assertTrue(m.user_can_view(H.G1, H.U2, H.CH2))
        self.assertIn((H.G1, H.U2), m._WATCH_MEMBERS)
        m.watch_perm_event({"t": "GUILD_ROLE_DELETE", "d": {"guild_id": H.G1, "role_id": H.ROLE}})
        self.assertFalse(m.user_can_view(H.G1, H.U2, H.CH2))

    def test_role_and_channel_events_change_view_immediately(self):
        m = self.mod
        m._CHANNEL_OVERWRITES[H.CH2] = []
        m._GUILD_PERMS[H.G1]["roles"][H.G1] = 0
        self.assertFalse(m.user_can_view(H.G1, H.U2, H.CH2))
        m.watch_perm_event(
            {
                "t": "GUILD_ROLE_UPDATE",
                "d": {"guild_id": H.G1, "role": {"id": H.ROLE, "permissions": str(VIEW)}},
            }
        )
        self.assertTrue(m.user_can_view(H.G1, H.U2, H.CH2))
        m.watch_perm_event(
            {
                "t": "CHANNEL_UPDATE",
                "d": {
                    "guild_id": H.G1,
                    "id": H.CH2,
                    "permission_overwrites": [overwrite(H.U2, deny=VIEW, type_=1)],
                },
            }
        )
        self.assertFalse(m.user_can_view(H.G1, H.U2, H.CH2))

    def test_owner_transfer_invalidates_owner_bypass(self):
        m = self.mod
        m._GUILD_PERMS[H.G1]["roles"] = {H.G1: 0}
        m.watch_perm_event({"t": "GUILD_UPDATE", "d": {"id": H.G1, "owner_id": H.U1}})
        self.assertTrue(m.user_can_view(H.G1, H.U1, H.CH))
        m.watch_perm_event({"t": "GUILD_UPDATE", "d": {"id": H.G1, "owner_id": H.U2}})
        self.assertFalse(m.user_can_view(H.G1, H.U1, H.CH))

    def test_unknown_nonmember_private_thread_and_malformed_overwrite_deny_view(self):
        m = self.mod
        self.assertFalse(m.user_can_view(H.G1, H.U1, "unknown"))
        self.assertFalse(m.user_can_view(H.G1, "100000000000000099", H.CH))
        m.CHANNELS[H.THREAD]["private"] = True
        self.assertFalse(m.user_can_view(H.G1, H.U1, H.THREAD))
        m._CHANNEL_OVERWRITES[H.CH] = [{"id": "bad", "type": 0, "allow": str(VIEW), "deny": "0"}]
        self.assertIsNone(m.user_channel_perms(H.G1, H.U1, H.CH))
        self.assertFalse(m.channel_public(H.G1, H.CH))

    def test_missing_member_data_is_unknown_and_failure_is_cached(self):
        m = self.mod
        m.api_get = lambda path: {}
        self.assertIsNone(m.user_channel_perms(H.G1, H.U1, H.CH))
        m.api_get = lambda path: (_ for _ in ()).throw(AssertionError("failure must be cached"))
        self.assertFalse(m.user_can_view(H.G1, H.U1, H.CH))


if __name__ == "__main__":
    unittest.main()

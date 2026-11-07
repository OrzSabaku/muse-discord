"""Effects, global/app ceilings, finite write targets, and strict policies."""

import os
from pathlib import Path
import sys
import tempfile
import unittest

import yaml

ROOT = Path(os.environ.get("MUT_DIR", Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(ROOT / "src"))
from muse_discord.external_apps.policy import LEVELS, Refused, evaluate, load_policy  # noqa: E402 (local import path bootstrap)


def policy():
    return {
        "version": 1,
        "apps": {
            "calendar": {
                "level": "write",
                "command": ["/trusted/calendar"],
                "tools": {
                    "list": {"effect": "read"},
                    "draft": {
                        "effect": "write",
                        "auto_write": True,
                        "constraints": {"calendar_id": ["personal"]},
                    },
                    "edit": {"effect": "write"},
                    "delete": {"effect": "dangerous"},
                },
            }
        },
    }


class Policy(unittest.TestCase):
    def test_stage_matrix_and_app_ceiling(self):
        for global_level in LEVELS:
            for app_level in LEVELS:
                data = policy()
                data["apps"]["calendar"]["level"] = app_level
                ceiling = min(LEVELS.index(global_level), LEVELS.index(app_level))
                for tool in ("list", "draft", "edit", "delete"):
                    with self.subTest(global_level=global_level, app_level=app_level, tool=tool):
                        if ceiling == 0 or (tool != "list" and ceiling < 2):
                            with self.assertRaises(Refused):
                                evaluate(
                                    data,
                                    global_level,
                                    "calendar",
                                    tool,
                                    {"calendar_id": "personal"},
                                )
                        else:
                            _app, _effect, approval = evaluate(
                                data, global_level, "calendar", tool, {"calendar_id": "personal"}
                            )
                            self.assertEqual(
                                approval,
                                ceiling < 4
                                and tool != "list"
                                and not (ceiling == 3 and tool == "draft"),
                            )

    def test_unconditional_still_requires_registered_tools_and_declared_targets(self):
        data = policy()
        data["apps"]["calendar"]["level"] = "unconditional"
        for tool in ("list", "edit", "delete"):
            self.assertFalse(evaluate(data, "unconditional", "calendar", tool, {})[2])
        for app, tool, arguments in [
            ("unknown", "delete", {}),
            ("calendar", "unknown", {}),
            ("calendar", "draft", {"calendar_id": "work"}),
        ]:
            with self.assertRaises(Refused):
                evaluate(data, "unconditional", app, tool, arguments)
        self.assertEqual(
            evaluate(data, "unconditional", "calendar", "draft", {"calendar_id": "personal"})[2],
            False,
        )

    def test_unregistered_targets_and_type_confusion_fail_closed(self):
        for app, tool, args in [
            ("unknown", "list", {}),
            ("calendar", "unknown", {}),
            ("calendar", "draft", {}),
            ("calendar", "draft", {"calendar_id": "work"}),
        ]:
            with self.assertRaises(Refused):
                evaluate(policy(), "write", app, tool, args)
        data = policy()
        data["apps"]["calendar"]["tools"]["draft"]["constraints"] = {"calendar_id": [1]}
        for value in (True, 1.0, "1"):
            with self.assertRaises(Refused):
                evaluate(data, "write", "calendar", "draft", {"calendar_id": value})
        for args in ([], {"bad": float("nan")}, {"large": "x" * 65537}):
            with self.assertRaises(Refused):
                evaluate(data, "write", "calendar", "list", args)

    def test_invalid_effects_commands_and_unbounded_auto_write_are_rejected(self):
        bad = []
        for field, value in [
            ("level", "everything"),
            ("command", ["sh", "-c", "x"]),
            ("command", []),
            ("tools", {"x": {"effect": "guess"}}),
            ("tools", {"x": {"effect": "write", "auto_write": True}}),
            (
                "tools",
                {"x": {"effect": "dangerous", "auto_write": True, "constraints": {"id": ["1"]}}},
            ),
            ("env", {"TOKEN": "literal secret value"}),
        ]:
            data = policy()
            data["apps"]["calendar"][field] = value
            bad.append(data)
        data = policy()
        data["apps"]["calendar"]["tools"]["list"]["typo"] = True
        bad.append(data)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.yaml"
            for data in bad:
                with self.subTest(data=data):
                    path.write_text(yaml.safe_dump(data))
                    with self.assertRaises(Refused):
                        load_policy(path)
            path.write_text(yaml.safe_dump(policy()))
            self.assertEqual(load_policy(path), policy())


if __name__ == "__main__":
    unittest.main()

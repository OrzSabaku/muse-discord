"""Pydantic schema contract and integration with sparse setting layers."""

import json
import os
import sys
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_lib")
)
from settings_harness import MS, G1, CH, OWNER, tmpdir, write  # noqa: E402
from muse_discord.settings_schema import SettingsSchema  # noqa: E402
from pydantic import ValidationError  # noqa: E402


class Schema(unittest.TestCase):
    def test_typed_values_and_independent_defaults(self):
        model = SettingsSchema.model_validate(
            {
                "who": {"dms_from": ["123"], "ignore": ["role:456"]},
                "proactive": {"enabled": False, "frequency": "often", "per_day": 35},
                "limits": {"per_day": None},
            }
        )
        self.assertEqual(model.who.dms_from, ["123"])
        self.assertEqual(model.who.ignore, ["role:456"])
        self.assertFalse(model.proactive.enabled)
        self.assertEqual(model.proactive.frequency, "often")
        self.assertEqual(model.proactive.per_day, 35)
        self.assertIsNone(model.limits.per_day)
        model.who.ignore.append("789")
        self.assertEqual(SettingsSchema().who.ignore, [])

    def test_invalid_values_and_unknown_fields_are_rejected(self):
        for values in (
            {"proactive": {"per_day": True}},
            {"proactive": {"per_day": -1}},
            {"proactive": {"per_day": 1.5}},
            {"proactive": {"frequency": "hourly"}},
            {"proactive": {"per_day": None}},
            {"who": {"ignore": ["alice"]}},
            {"who": {"ignore": [-1]}},
            {"proactive": {"quiet_hours": "09:00-09:00"}},
            {"proactive": {"quiet_hours": " " * 25 + "off"}},
            {"proactive": {"timezone": "../UTC"}},
            {"proactive": {"frequncy": "often"}},
            {"proactive": {"per_day": "35"}},
            {"proactive": {"frequency": "OFTEN"}},
            {"limits": {"per_day": "none"}},
            {"who": {"dms_from": 123}},
            {"who": {"ignore": "role:456"}},
            {"privacy": {"suspicious": "answer"}},
            {"unknown": {}},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                SettingsSchema.model_validate(values)

    def test_json_schema_exposes_constraints_and_edit_policy(self):
        schema = json.loads(json.dumps(SettingsSchema.model_json_schema()))
        properties = schema["$defs"]["ProactiveSettings"]["properties"]
        self.assertEqual(properties["frequency"]["enum"], ["rarely", "sometimes", "often"])
        self.assertEqual(properties["per_day"]["minimum"], 0)
        self.assertEqual(properties["per_day"]["maximum"], 100000)
        self.assertFalse(properties["per_day"]["discord_editable"])
        self.assertEqual(properties["frequency"]["scopes"], ["everywhere", "dms", "server"])
        self.assertFalse(schema["additionalProperties"])

    def test_missing_override_inherits_but_explicit_zero_overrides(self):
        directory = tmpdir("pydantic-inheritance-")
        config = write(
            os.path.join(directory, "config.yaml"),
            f'''version: 1
owners: ["{OWNER}"]
everywhere:
  proactive:
    per_channel_per_day: 20
servers:
  "{G1}":
    proactive:
      frequency: often
    channels:
      "{CH}":
        proactive:
          enabled: on
''',
        )
        scope = MS.Scope.server(G1, channel_id=CH)
        st = MS.load(config)
        self.assertEqual(st.get("proactive.per_channel_per_day", scope), 20)
        self.assertEqual(st.get("proactive.frequency", scope), "often")
        self.assertTrue(st.get("proactive.enabled", scope))
        self.assertNotIn((("servers", G1), "proactive.per_channel_per_day"), st.file_values)
        MS.set_value(config, f'servers."{G1}".proactive.per_channel_per_day', 0, OWNER)
        self.assertEqual(MS.load(config).get("proactive.per_channel_per_day", scope), 0)


if __name__ == "__main__":
    unittest.main()

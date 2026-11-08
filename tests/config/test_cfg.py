"""Config resolution, offline: resolved constants for missing keys,
explicit nulls and zeros, and out-of-range and garbage values. Each case
runs in a fresh subprocess and explicitly initializes the monitor.
MUT_DIR = the package tree under test (default: this checkout)."""

import json
import os
import subprocess
import sys
import textwrap
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_lib")
)
from common import MUT_DIR, scratch

PROBE = r"""
import sys, json
sys.argv = ["x", "dummy-token"]
sys.path.insert(0, %(repo)r + "/src")
from muse_discord import monitor
monitor.initialize(token_override="dummy-token")
out = {
    'COALESCE_SECS': monitor._SETTINGS.engine.coalesce_seconds,
    'BOT_COALESCE_SECS': monitor._SETTINGS.engine.bot_coalesce_seconds,
    'MAX_REPLY_CHARS': monitor._SETTINGS.engine.max_reply_chars,
    'POLL_INTERVAL': monitor._SETTINGS.engine.rest_poll_seconds,
    'SWEEP_EVERY_CYCLES': monitor._SETTINGS.engine.sweep.every_cycles,
    'SWEEP_GRACE_SECS': monitor._SETTINGS.engine.sweep.grace_seconds,
    'SWEEP_MAX_AGE_SECS': monitor._SETTINGS.engine.sweep.max_age_seconds,
    'SWEEP_MAX_RECOVERIES': monitor._SETTINGS.engine.sweep.max_recoveries,
    'BACKOFF_INITIAL_SECS': monitor._SETTINGS.engine.backoff.initial_seconds,
    'BACKOFF_MAX_SECS': monitor._SETTINGS.engine.backoff.max_seconds,
    'CHANNEL_REFRESH_CYCLES': monitor._SETTINGS.engine.discovery_refresh_cycles,
}
caps = monitor._SETTINGS.initial.resolve(monitor.MS.Scope.everywhere())
out["DAILY_REPLY_CAP"] = caps["limits.per_day"]
out["PER_USER_DAILY_CAP"] = caps["limits.per_person_per_day"]
out["HAS_MAX_BOT_TURNS"] = hasattr(monitor, "MAX_BOT_TURNS")
print("JSON" + json.dumps(out))
"""


def run(name, yaml_text):
    d = scratch("muse-cfg-")
    path = os.path.join(d, f"cfg_{name}.yaml")
    with open(path, "w") as f:
        f.write(textwrap.dedent(yaml_text))
    env = dict(os.environ, MUSE_DISCORD_CONFIG=path)
    p = subprocess.run(
        [sys.executable, "-c", PROBE % {"repo": MUT_DIR}],
        capture_output=True,
        text=True,
        env=env,
        cwd=d,
        timeout=60,
    )
    if p.returncode != 0:
        raise AssertionError(f"case {name} crashed:\n{p.stdout}{p.stderr}")
    lines = p.stdout.splitlines()
    data = json.loads(next(ln for ln in lines if ln.startswith("JSON"))[4:])
    warnings = [ln for ln in lines if ln.startswith("WARNING")]
    return data, warnings, lines


class Config(unittest.TestCase):
    def check(self, label, got, want):
        with self.subTest(label):
            self.assertEqual(got, want, label)

    def test_defaults_and_nulls(self):
        """Missing keys and explicit nulls -> defaults."""
        for name, text in (
            ("empty", "{}\n"),
            (
                "nulls",
                """engine:
  rest_poll_seconds: null
  sweep:
    every_cycles: null
    grace_seconds: null
  coalesce_seconds: null
  max_reply_chars: null
""",
            ),
        ):
            d, w, _ = run(name, text)
            self.check(f"{name} COALESCE_SECS", d["COALESCE_SECS"], 5.0)
            self.check(f"{name} MAX_REPLY_CHARS", d["MAX_REPLY_CHARS"], 1800)
            self.check(f"{name} POLL_INTERVAL", d["POLL_INTERVAL"], 30)
            self.check(f"{name} SWEEP_EVERY_CYCLES", d["SWEEP_EVERY_CYCLES"], 6)
            self.check(f"{name} DAILY_REPLY_CAP", d["DAILY_REPLY_CAP"], None)
            self.check(f"{name} warnings", w, [])

    def test_zeros_out_of_range_and_garbage(self):
        """Explicit zeros: honored where meaningful (caps), clamped where
        not (no hot loops)."""
        d, _w, _ = run(
            "zeros",
            """engine:
  rest_poll_seconds: 0
  discovery_refresh_minutes: 0
  sweep:
    every_cycles: 0
    grace_seconds: 0
    max_age_hours: 0
    max_recoveries: 0
  backoff:
    initial_seconds: 0
    max_seconds: 0
  coalesce_seconds: 0
  max_reply_chars: 0
everywhere:
  limits:
    per_day: 0
    per_person_per_day: 0
""",
        )
        self.check("COALESCE_SECS", d["COALESCE_SECS"], 0.0)
        self.check("MAX_REPLY_CHARS", d["MAX_REPLY_CHARS"], 1)
        self.check("POLL_INTERVAL", d["POLL_INTERVAL"], 10)
        self.check("CHANNEL_REFRESH_CYCLES", d["CHANNEL_REFRESH_CYCLES"], 6)
        self.check("SWEEP_EVERY_CYCLES", d["SWEEP_EVERY_CYCLES"], 1)
        self.check("BACKOFF_INITIAL_SECS", d["BACKOFF_INITIAL_SECS"], 1.0)
        self.check("BACKOFF_MAX_SECS", d["BACKOFF_MAX_SECS"], 1.0)
        self.check("DAILY_REPLY_CAP", d["DAILY_REPLY_CAP"], 0)
        self.check("PER_USER_DAILY_CAP", d["PER_USER_DAILY_CAP"], 0)
        # Out of range above (Discord's 2000-character limit) / garbage.
        d, _w, _ = run(
            "range",
            """engine:
  rest_poll_seconds: 45
  discovery_refresh_minutes: 3
  sweep:
    every_cycles: abc
  coalesce_seconds: -3
  max_reply_chars: 5000
""",
        )
        self.check("MAX_REPLY_CHARS", d["MAX_REPLY_CHARS"], 2000)
        self.check("COALESCE_SECS", d["COALESCE_SECS"], 0.0)
        self.check("POLL_INTERVAL", d["POLL_INTERVAL"], 45)
        self.check("SWEEP_EVERY_CYCLES", d["SWEEP_EVERY_CYCLES"], 6)


if __name__ == "__main__":
    unittest.main()

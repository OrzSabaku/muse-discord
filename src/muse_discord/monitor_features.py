"""Load optional monitor features without making their failure stop reception.

Imports and construction stay inside the loaders. Failure logs contain only the
exception class; configuration, credentials and exception messages stay private.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from muse_discord.interactions import ephemeral

if TYPE_CHECKING:
    from muse_discord.proactive import Proactive, ProactiveContext
    from muse_discord.telemetry import Recorder


class NoTelemetry:
    def __getattr__(self, name):
        return lambda *args, **kwargs: ""


class NoProactive:
    def __init__(self):
        self.store = self.consent = self.policy = self.delivery = self.actions = self

    def __getattr__(self, name):
        return lambda *args, **kwargs: None

    def on_button(self, ix, rest, uid, record):
        return ephemeral("That button is no longer active.")


def load_telemetry(
    paths: Callable[[], tuple[str, str, str]], package_dir: str
) -> Recorder | NoTelemetry:
    try:
        from muse_discord.settings_presets import PRESETS
        from muse_discord.telemetry import Recorder
        from muse_discord.usage_stats.settings_metrics import SettingsMetrics

        return Recorder(paths, metrics=SettingsMetrics(PRESETS), pkg_dir=package_dir)
    except Exception as exc:
        print(f"Telemetry not loaded ({type(exc).__name__}); usage counts are off.", flush=True)
        return NoTelemetry()


def load_proactive(context: Callable[[], ProactiveContext]) -> Proactive | NoProactive:
    try:
        from muse_discord.proactive import Proactive

        return Proactive(context())
    except Exception as exc:
        print(f"proactive.py not loaded ({type(exc).__name__}); the bot only replies.", flush=True)
        return NoProactive()

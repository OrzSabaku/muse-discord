"""The narrow optional settings integration used by the anonymous recorder."""

from typing import Any, Protocol


class SettingsMetricsSource(Protocol):
    def collect(self, record: dict, settings: Any, mark: Any) -> Any: ...

    def features(self, settings: Any) -> dict: ...

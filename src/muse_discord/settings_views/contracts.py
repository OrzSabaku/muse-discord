"""Screen session data and the client capabilities used by native UI components."""

from collections.abc import Awaitable, Callable
from typing import Any, NotRequired, Protocol, TypedDict

MAX_CONFIRMATION_WARNINGS = 5


class Pending(TypedDict):
    warnings: list[str]
    undo: NotRequired[str | None]


class SettingsClient(Protocol):
    settings_context: Callable[[], Any]
    settings_log: Callable[[str], None]
    settings_guard: Callable[[Any, str], Any]
    settings_redraw: Callable[..., Awaitable[None]]


TTL = 900


PREFIX = "mset:"

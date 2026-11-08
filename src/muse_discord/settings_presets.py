"""Named server profiles translated into ordinary, reviewable setting changes.

Profiles never write settings. The same planner, permission rules, assessment
and grouped application used by manual edits process their resulting changes.
"""

from muse_discord.muse_settings import (
    Change,
    Place,
    Scope,
    Settings,
    SettingsError,
    plan_changes,
)
from muse_discord.settings_assessment import BIG_FOR_MEMORY
from muse_discord.settings_display import ServerInfo

# -- presets ------------------------------------------------------------------
_IF_MEDIA, _IF_EMPTY = "if-media-on", "if-empty"
WORK_NOTES = "Keep a professional tone. Keep replies brief."
PRESETS = {
    "friends": (
        "Friends",
        {
            "who.replies": "conversational",
            "privacy.memory": "everything",
            "limits.per_person_per_day": None,
            "limits.per_day": None,
            "who.bots": "when-addressed",
            "who.bot_exchange_limit": None,
            "media.per_day": None,
            "proactive.enabled": True,
        },
    ),
    "community": (
        "Community",
        {
            "who.replies": "when-addressed",
            "privacy.memory": "public-only",
            "limits.per_person_per_day": 20,
            "limits.per_day": 300,
            "who.bots": "when-addressed",
            "who.bot_exchange_limit": 4,
            "media.per_day": (_IF_MEDIA, 10),
            "proactive.enabled": False,
        },
    ),
    "work": (
        "Work",
        {
            "who.replies": "when-addressed",
            "privacy.memory": "nothing",
            "limits.per_person_per_day": None,
            "limits.per_day": None,
            "who.bots": "ignore",
            "media.per_day": None,
            "proactive.enabled": False,
            "style.notes": (_IF_EMPTY, WORK_NOTES),
        },
    ),
}


def _preset(name):
    n = str(name or "").strip().lower()
    if n not in PRESETS:
        raise SettingsError(f"{name!r} is not a preset: Friends, Community or Work")
    return PRESETS[n]


def preset_changes(st: Settings, name: str, place: Place, info=None, big="refuse") -> list[Change]:
    """[Change] that apply preset name (friends | community | work) to a
    server place: one undo group when applied together. Keys a preset
    leaves unchanged are absent; Community's media cap only when media is
    on there; Work's instructions only when the server has none; changes
    that would change nothing are left out. Friends (anything it
    remembers) is refused for a server known to have more than 50
    members; with big="skip" it is applied without that one key
    instead (/settings' preview says so). Presets are an owner's tool
    (admin_changes refuses them)."""
    title, values = _preset(name)
    if place.kind != "server":
        raise SettingsError("Presets apply to a whole server.")
    members = ServerInfo(info).members(place.guild_id)
    skip = set()
    if title == "Friends" and members is not None and members > BIG_FOR_MEMORY:
        if big != "skip":
            raise SettingsError(
                f"The Friends preset lets replies use anything it "
                f"remembers about you, so it is only for servers of up to "
                f"{BIG_FOR_MEMORY} members; this one has {members:,}."
            )
        skip.add("privacy.memory")
    out = []
    sc = Scope.server(place.guild_id)
    for key, value in values.items():
        if st.spec(key) is None or key in skip:
            continue  # a setting omitted by this preset
        if isinstance(value, tuple):
            cond, value = value
            if cond == _IF_MEDIA and not st.get("media.enabled", sc):
                continue
            if cond == _IF_EMPTY and st.get(key, sc):
                continue
        out.append(Change(place, key, value))
    return [planned.change for planned in plan_changes(st, out) if not planned.noop]


def matching_preset(st: Settings, place: Place) -> str | None:
    """ "Friends" / "Community" / "Work" when a server's values in force
    match that preset, else None ("Custom"). Instructions and reaching
    out on its own (a server may keep Friends with it off) are not
    compared; the media cap only when media is on there."""
    if place.kind != "server":
        return None
    sc = Scope.server(place.guild_id)
    for title, values in PRESETS.values():
        ok = True
        for key, value in values.items():
            if key in ("style.notes", "proactive.enabled") or st.spec(key) is None:
                continue
            if isinstance(value, tuple):
                cond, value = value
                if cond == _IF_MEDIA and not st.get("media.enabled", sc):
                    continue
            if st.get(key, sc) != value:
                ok = False
                break
        if ok:
            return title
    return None

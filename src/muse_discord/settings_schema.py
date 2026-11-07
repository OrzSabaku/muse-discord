"""Typed setting definitions shared by files, the CLI and Discord settings.

Pydantic owns types, defaults and constraints. Policy metadata describes where
an item may be edited; authorization and inheritance remain in muse_settings.
Each layer validates only supplied values, so defaults never erase inheritance.
"""

from dataclasses import dataclass
from pydantic.fields import FieldInfo
from typing import Annotated, Literal
import re
from zoneinfo import ZoneInfo

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
    WrapValidator,
)

NOTES_MAX = 500
PRESENCE_KINDS = ("watching", "playing", "listening", "streaming", "competing", "custom")
_LETTER = {"E": "everywhere", "D": "dms", "A": "all_servers", "S": "server", "C": "channel"}


def _boolean(value):
    if isinstance(value, str) and value.strip().lower() in (
        "on",
        "off",
        "true",
        "false",
        "yes",
        "no",
    ):
        return value.strip().lower() in ("on", "true", "yes")
    return value


def _choice(value):
    if isinstance(value, bool):
        return "on" if value else "off"
    return value.strip().lower() if isinstance(value, str) else value


def _number(value):
    if isinstance(value, str):
        value = value.strip()
        if value.lower() == "none":
            return None
        if re.fullmatch(r"-?\d+", value):
            return int(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _text(value):
    if value is None:
        return ""
    return str(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else value


def _identifier(value):
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return value.strip() if isinstance(value, str) else value


def _person(value):
    value = _identifier(value)
    if isinstance(value, str) and value.lower().startswith("role:"):
        return "role:" + value[5:].strip()
    return value


def _people(value):
    if value is None:
        return []
    if isinstance(value, str):
        value = value.strip()
        if not value.isdigit() and not value.lower().startswith("role:"):
            return value.lower()
        return [value]
    if isinstance(value, int) and not isinstance(value, bool):
        return [value]
    return value


def _dm_access(value):
    value = _choice(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return [value]
    if isinstance(value, str) and value.isdigit():
        return [value]
    return value


def _unique_ids(value):
    return list(dict.fromkeys(value)) or "owners" if isinstance(value, list) else value


def _phrase(value):
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value).strip()
    return value


def _quiet_hours(value):
    value = value.strip().lower()
    if value in ("off", "none", ""):
        return "off"
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)\s*-\s*([01]?\d|2[0-3]):([0-5]\d)", value)
    if not m:
        raise ValueError("expected a time range like 22:00-09:00 (or off)")
    a, b = (int(m[1]), int(m[2])), (int(m[3]), int(m[4]))
    if a == b:
        raise ValueError("quiet hours need a start and an end that differ")
    return "%02d:%02d-%02d:%02d" % (a + b)


def _timezone(value):
    value = value.strip()
    if value.upper() in ("UTC", "Z", "GMT", ""):
        return "UTC"
    if (
        not re.fullmatch(r"[A-Za-z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+){0,2}", value)
        or ".." in value
    ):
        raise ValueError("expected a time zone like Europe/Paris")
    try:
        ZoneInfo(value)
    except (KeyError, ValueError):
        raise ValueError("expected a known IANA time zone like Europe/Paris") from None
    return value


ConfigBool = Annotated[StrictBool, BeforeValidator(_boolean)]
ConfigNumber = Annotated[StrictInt, BeforeValidator(_number)]
ConfigText = Annotated[StrictStr, BeforeValidator(_text)]
DiscordId = Annotated[StrictStr, Field(pattern=r"^[0-9]{1,25}$"), BeforeValidator(_identifier)]
PersonId = Annotated[StrictStr, Field(pattern=r"^(?:role:)?[0-9]{1,25}$"), BeforeValidator(_person)]
PeopleList = Annotated[list[PersonId], BeforeValidator(_people)]
DmAccess = Annotated[
    Literal["owners", "shared-servers", "anyone"] | list[DiscordId],
    BeforeValidator(_dm_access),
    AfterValidator(_unique_ids),
]
Phrase = Annotated[StrictStr, Field(min_length=1), BeforeValidator(_phrase)]
Phrases = Annotated[list[Phrase], BeforeValidator(lambda v: [] if v is None else v)]


def _legacy_empty_text(value, handler):
    # YAML null used to return an empty string before custom validation.
    return "" if value is None else handler(value)


QuietHours = Annotated[
    ConfigText,
    Field(max_length=20),
    AfterValidator(_quiet_hours),
    WrapValidator(_legacy_empty_text),
]
Timezone = Annotated[
    ConfigText, Field(max_length=64), AfterValidator(_timezone), WrapValidator(_legacy_empty_text)
]


def setting(default, scopes, *, editor=None, **options):
    """Field() with edit-policy metadata; value constraints are Pydantic's."""
    policy_keys = ("risky", "discord_editable", "hidden", "legacy", "hidden_values")
    policy = {key: options.pop(key) for key in policy_keys if key in options}
    policy["scopes"] = [_LETTER[c] for c in scopes]
    if editor:
        policy["editor"] = editor
    return Field(default=default, json_schema_extra=policy, **options)


class SettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True, frozen=True)


class WhoSettings(SettingsModel):
    replies: Annotated[
        Literal["off", "when-addressed", "conversational", "freely"], BeforeValidator(_choice)
    ] = setting("conversational", "DASC")
    dms_from: DmAccess = setting("owners", "D", risky=True)
    allow: Annotated[Literal["everyone"] | list[PersonId], BeforeValidator(_people)] = setting(
        "everyone", "EDS", editor="people"
    )
    ignore: PeopleList = setting([], "EDS", editor="people")
    bots: Annotated[Literal["ignore", "when-addressed"], BeforeValidator(_choice)] = setting(
        "when-addressed", "EDS"
    )
    bot_exchange_limit: Annotated[StrictInt | None, BeforeValidator(_number)] = setting(
        None, "ES", ge=1, le=50
    )


class StyleSettings(SettingsModel):
    length: Annotated[Literal["brief", "normal", "detailed"], BeforeValidator(_choice)] = setting(
        "normal", "EDS"
    )
    notes: ConfigText = setting("", "EDSC", max_length=NOTES_MAX)


class AbilitiesSettings(SettingsModel):
    owner_commands: ConfigBool = setting(True, "ES")
    external_apps: Annotated[
        Literal["off", "read-only", "confirm", "write", "unconditional"], BeforeValidator(_choice)
    ] = setting("off", "E", risky=True)
    reminders: Annotated[
        Literal["owners", "everyone"] | list[PersonId], BeforeValidator(_people)
    ] = setting("everyone", "EDS", editor="people")


class PrivacySettings(SettingsModel):
    memory: Annotated[Literal["everything", "public-only", "nothing"], BeforeValidator(_choice)] = (
        setting("public-only", "EDS", risky=True)
    )
    never_share: Phrases = setting([], "E", discord_editable=False)
    suspicious: Annotated[
        Literal["hold-for-review", "ignore", "answer"], BeforeValidator(_choice)
    ] = setting("hold-for-review", "EDS", hidden_values=("answer",))


class LimitsSettings(SettingsModel):
    per_day: Annotated[StrictInt | None, BeforeValidator(_number)] = setting(
        None, "EDS", ge=0, le=100000
    )
    per_person_per_day: Annotated[StrictInt | None, BeforeValidator(_number)] = setting(
        None, "EDS", ge=0, le=10000
    )


class MediaSettings(SettingsModel):
    enabled: ConfigBool = setting(True, "EDASC")
    who: Annotated[Literal["owners", "everyone"] | list[PersonId], BeforeValidator(_people)] = (
        setting("owners", "ES", editor="people", risky=True, legacy="allowed_user_ids")
    )
    per_day: Annotated[StrictInt | None, BeforeValidator(_number)] = setting(
        None, "ES", ge=0, le=1000, legacy="daily_cap"
    )


class ProactiveSettings(SettingsModel):
    per_day: ConfigNumber = setting(30, "E", ge=0, le=100000, discord_editable=False)
    enabled: ConfigBool = setting(False, "EDASC")
    frequency: Annotated[Literal["rarely", "sometimes", "often"], BeforeValidator(_choice)] = (
        setting("rarely", "EDS")
    )
    per_person_per_day: ConfigNumber = setting(1, "ED", ge=0, le=50)
    per_person_per_week: ConfigNumber = setting(3, "ED", ge=0, le=100)
    per_channel_per_day: ConfigNumber = setting(1, "ES", ge=0, le=50)
    quiet_hours: QuietHours = setting("22:00-09:00", "E")
    timezone: Timezone = setting("UTC", "E")


class PresenceSettings(SettingsModel):
    status: ConfigText = setting("", "E", max_length=128)
    kind: Annotated[
        Literal["watching", "playing", "listening", "streaming", "competing", "custom"],
        BeforeValidator(_choice),
    ] = setting("watching", "E")
    enabled: ConfigBool = setting(True, "E", hidden=True, discord_editable=False)


class UpdatesSettings(SettingsModel):
    notify: ConfigBool = setting(True, "E")


class TelemetrySettings(SettingsModel):
    usage_stats: ConfigBool = setting(True, "E")


class MemorySettings(SettingsModel):
    save_dms: ConfigBool = setting(True, "E")


class SettingsSchema(SettingsModel):
    who: WhoSettings = Field(default_factory=WhoSettings)
    style: StyleSettings = Field(default_factory=StyleSettings)
    abilities: AbilitiesSettings = Field(default_factory=AbilitiesSettings)
    privacy: PrivacySettings = Field(default_factory=PrivacySettings)
    limits: LimitsSettings = Field(default_factory=LimitsSettings)
    media: MediaSettings = Field(default_factory=MediaSettings)
    proactive: ProactiveSettings = Field(default_factory=ProactiveSettings)
    presence: PresenceSettings = Field(default_factory=PresenceSettings)
    updates: UpdatesSettings = Field(default_factory=UpdatesSettings)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)


@dataclass(frozen=True)
class SettingField:
    """Editor metadata derived from a Pydantic field, not another schema.

    All values below are extracted by from_field(); validation is performed
    by the same annotation used by the Pydantic model.
    """

    validator: TypeAdapter
    type: str
    default: object
    scopes: tuple
    values: tuple
    hidden_values: tuple = ()
    min: int | None = None
    max: int | None = None
    maxlen: int | None = None
    none: bool = False
    ids: bool = False
    risky: bool = False
    discord_editable: bool = True
    hidden: bool = False
    legacy: str | None = None

    @classmethod
    def from_field(cls, info: FieldInfo):
        validator = TypeAdapter(info.rebuild_annotation())
        schema = validator.json_schema()
        variants = schema.get("anyOf", [schema])
        policy = info.json_schema_extra or {}
        choices = tuple(
            v for b in variants for v in b.get("enum", [b["const"]] if "const" in b else [])
        )
        kind = policy.get("editor") or (
            "choice"
            if choices
            else {"boolean": "on-off", "integer": "number", "string": "text", "array": "phrases"}[
                next(b["type"] for b in variants if b["type"] != "null")
            ]
        )
        hidden = tuple(policy.get("hidden_values", ()))

        def constraint(name):
            return next((b[name] for b in variants if name in b), None)

        return cls(
            validator=validator,
            type=kind,
            default=validator.validate_python(info.get_default(call_default_factory=True)),
            scopes=tuple(policy["scopes"]),
            values=tuple(v for v in choices if v not in hidden),
            hidden_values=hidden,
            min=constraint("minimum"),
            max=constraint("maximum"),
            maxlen=constraint("maxLength"),
            none=any(b.get("type") == "null" for b in variants),
            ids=kind == "choice" and any(b.get("type") == "array" for b in variants),
            **{
                k: policy[k]
                for k in ("risky", "discord_editable", "hidden", "legacy")
                if k in policy
            },
        )

    def valid_text(self):
        if self.type == "choice":
            return ", ".join(self.values) + (", or a list of user ids" if self.ids else "")
        if self.type == "on-off":
            return "on, off"
        if self.type == "number":
            return ("none, " if self.none else "") + f"{self.min}-{self.max}"
        if self.type == "people":
            return (
                "".join(f"{v}, or " for v in self.values)
                + 'a list of "<user id>" / "role:<role id>"'
            )
        if self.type == "text":
            return f"text up to {self.maxlen} characters" if self.maxlen else "text"
        return "a list of words or phrases"

    def normalize(self, value):
        try:
            return True, self.validator.validate_python(value)
        except ValidationError as exc:
            messages = []
            for error in exc.errors(include_url=False):
                bad = error.get("input")
                prefix = f"{bad!r}: " if not isinstance(bad, (dict, list, tuple)) else ""
                messages.append(prefix + error["msg"])
            return False, "; ".join(dict.fromkeys(messages))


def field_registry(*groups):
    """Expose only field definitions; do not instantiate defaults per layer."""
    result = {}
    for group in groups:
        model = SettingsSchema.model_fields[group].annotation
        for name, info in model.model_fields.items():
            result[f"{group}.{name}"] = SettingField.from_field(info)
    return result


CORE_GROUPS = ("who", "style", "abilities", "privacy", "limits", "media", "proactive")
CORE = field_registry(*CORE_GROUPS)
TOP = field_registry("presence", "updates", "telemetry", "memory")

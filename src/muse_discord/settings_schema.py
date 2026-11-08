"""Typed setting definitions shared by files, the CLI and Discord settings.

Pydantic owns types, defaults and constraints. Policy metadata describes where
an item may be edited; authorization and inheritance remain in muse_settings.
Each layer validates only supplied values, so defaults never erase inheritance.
"""

from dataclasses import dataclass
from functools import cache
from typing import Annotated, ClassVar, Literal, Required, TypedDict, cast
from zoneinfo import ZoneInfo

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    field_validator,
)
from pydantic.fields import FieldInfo

NOTES_MAX = 500
PRESENCE_KINDS = ("watching", "playing", "listening", "streaming", "competing", "custom")
_LETTER = {"E": "everywhere", "D": "dms", "A": "all_servers", "S": "server", "C": "channel"}


def _quiet_hours(value):
    if value != "off" and value[:5] == value[-5:]:
        raise ValueError("quiet hours need different start and end times")
    return value


def _timezone(value):
    try:
        ZoneInfo(value)
    except (KeyError, ValueError):
        raise ValueError("expected an IANA time zone such as Europe/Paris") from None
    return value


DiscordId = Annotated[StrictStr, Field(pattern=r"^[0-9]{1,25}$")]
PersonId = Annotated[StrictStr, Field(pattern=r"^(?:role:)?[0-9]{1,25}$")]
PeopleList = list[PersonId]
DmAccess = Literal["owners", "shared-servers", "anyone"] | list[DiscordId]
Phrases = list[Annotated[StrictStr, Field(pattern=r"\S")]]
QuietHours = Annotated[
    StrictStr,
    Field(pattern=r"^(off|([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d)$"),
    AfterValidator(_quiet_hours),
]
Timezone = Annotated[StrictStr, Field(max_length=64), AfterValidator(_timezone)]


def setting(default, scopes, *, editor=None, **options):
    """Field() with edit-policy metadata; value constraints are Pydantic's."""
    policy_keys = ("risky", "discord_editable", "hidden")
    policy = {key: options.pop(key) for key in policy_keys if key in options}
    policy["scopes"] = [_LETTER[c] for c in scopes]
    if editor:
        policy["editor"] = editor
    return Field(default=default, json_schema_extra=policy, **options)


class SettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True, frozen=True)


@cache
def _engine_bounds(model, field_name):
    return TypeAdapter(model.model_fields[field_name].rebuild_annotation()).json_schema()


class EngineModel(SettingsModel):
    """Normalize operational knobs using the defaults and bounds in their schema."""

    path: ClassVar[str] = "engine"
    model_config = ConfigDict(
        extra="forbid", validate_default=True, frozen=True, allow_inf_nan=False
    )

    @field_validator("*", mode="before")
    @classmethod
    def normalize(cls, value, info: ValidationInfo):
        assert info.field_name is not None
        field = cls.model_fields[info.field_name]
        if value is None:
            return field.get_default(call_default_factory=True)
        if field.annotation not in (int, float):
            return value
        cast_number = field.annotation
        try:
            number = cast_number(value)
        except (TypeError, ValueError, OverflowError):
            cls.warn(info, f"{value!r} is not a number; using the default {field.default}.")
            return field.default
        bounds = _engine_bounds(cls, info.field_name)
        for name, comparison in (("minimum", number.__lt__), ("maximum", number.__gt__)):
            limit = bounds.get(name)
            if limit is not None and comparison(limit):
                direction = "below" if name == "minimum" else "above"
                cls.warn(info, f"{value!r} is {direction} the {name} {limit}; using {limit}.")
                return cast_number(limit)
        return number

    @classmethod
    def warn(cls, info, message):
        if info.context is not None:
            info.context.setdefault("warnings", []).append(
                f"WARNING: config {cls.path}.{info.field_name}={message}"
            )


class SweepSettings(EngineModel):
    path: ClassVar[str] = "engine.sweep"
    every_cycles: int = Field(default=6, ge=1)
    grace_seconds: float = Field(default=240.0, ge=0)
    max_age_hours: float = Field(default=6.0, ge=0)
    max_recoveries: int = Field(default=2, ge=0)

    @property
    def max_age_seconds(self):
        return self.max_age_hours * 3600


class BackoffSettings(EngineModel):
    path: ClassVar[str] = "engine.backoff"
    initial_seconds: float = Field(default=60.0, ge=1)
    max_seconds: float = Field(default=900.0, ge=1)


class EngineSettings(EngineModel):
    rest_poll_seconds: int = Field(default=30, ge=10)
    discovery_refresh_minutes: float = Field(default=30.0, ge=1)
    coalesce_seconds: float = Field(default=5.0, ge=0)
    max_reply_chars: int = Field(default=1800, ge=1, le=2000)
    guild_ids: list[Annotated[DiscordId, BeforeValidator(str)]] = Field(default_factory=list)
    sweep: SweepSettings = Field(default_factory=SweepSettings)
    backoff: BackoffSettings = Field(default_factory=BackoffSettings)

    @property
    def bot_coalesce_seconds(self):
        return max(self.coalesce_seconds, 15.0)

    @property
    def discovery_refresh_cycles(self):
        return max(1, round(self.discovery_refresh_minutes * 60 / self.rest_poll_seconds))


class WhoSettings(SettingsModel):
    replies: Literal["off", "when-addressed", "conversational", "freely"] = setting(
        "conversational", "DASC"
    )
    dms_from: DmAccess = setting("owners", "D", risky=True)
    allow: Literal["everyone"] | list[PersonId] = setting("everyone", "EDS", editor="people")
    ignore: PeopleList = setting([], "EDS", editor="people")
    bots: Literal["ignore", "when-addressed"] = setting("when-addressed", "EDS")
    bot_exchange_limit: StrictInt | None = setting(None, "ES", ge=1, le=50)


class StyleSettings(SettingsModel):
    length: Literal["brief", "normal", "detailed"] = setting("normal", "EDS")
    notes: StrictStr = setting("", "EDSC", max_length=NOTES_MAX)


class AbilitiesSettings(SettingsModel):
    owner_commands: bool = setting(True, "ES")
    external_apps: Literal["off", "read-only", "confirm", "write", "unconditional"] = setting(
        "off", "E", risky=True
    )
    reminders: Literal["owners", "everyone"] | list[PersonId] = setting(
        "everyone", "EDS", editor="people"
    )


class PrivacySettings(SettingsModel):
    memory: Literal["everything", "public-only", "nothing"] = setting(
        "public-only", "EDS", risky=True
    )
    never_share: Phrases = setting([], "E", discord_editable=False)
    suspicious: Literal["hold-for-review", "ignore"] = setting("hold-for-review", "EDS")


class LimitsSettings(SettingsModel):
    per_day: StrictInt | None = setting(None, "EDS", ge=0, le=100000)
    per_person_per_day: StrictInt | None = setting(None, "EDS", ge=0, le=10000)


class MediaSettings(SettingsModel):
    enabled: bool = setting(True, "EDASC")
    who: Literal["owners", "everyone"] | list[PersonId] = setting(
        "owners", "ES", editor="people", risky=True
    )
    per_day: StrictInt | None = setting(None, "ES", ge=0, le=1000)


class ProactiveSettings(SettingsModel):
    per_day: StrictInt = setting(30, "E", ge=0, le=100000, discord_editable=False)
    enabled: bool = setting(False, "EDASC")
    frequency: Literal["rarely", "sometimes", "often"] = setting("rarely", "EDS")
    per_person_per_day: StrictInt = setting(1, "ED", ge=0, le=50)
    per_person_per_week: StrictInt = setting(3, "ED", ge=0, le=100)
    per_channel_per_day: StrictInt = setting(1, "ES", ge=0, le=50)
    quiet_hours: QuietHours = setting("22:00-09:00", "E")
    timezone: Timezone = setting("UTC", "E")


class PresenceSettings(SettingsModel):
    status: StrictStr = setting("", "E", max_length=128)
    kind: Literal["watching", "playing", "listening", "streaming", "competing", "custom"] = setting(
        "watching", "E"
    )
    enabled: bool = setting(True, "E", hidden=True, discord_editable=False)


class UpdatesSettings(SettingsModel):
    notify: bool = setting(True, "E")


class TelemetrySettings(SettingsModel):
    usage_stats: bool = setting(True, "E")


class MemorySettings(SettingsModel):
    save_dms: bool = setting(True, "E")


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


class SettingPolicy(TypedDict, total=False):
    scopes: Required[list[str]]
    editor: str
    risky: bool
    discord_editable: bool
    hidden: bool


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
    min: int | None = None
    max: int | None = None
    maxlen: int | None = None
    none: bool = False
    ids: bool = False
    risky: bool = False
    discord_editable: bool = True
    hidden: bool = False

    @classmethod
    def from_field(cls, info: FieldInfo):
        validator = TypeAdapter(info.rebuild_annotation())
        schema = validator.json_schema()
        variants = schema.get("anyOf", [schema])
        policy = cast(SettingPolicy, info.json_schema_extra or {})
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

        def constraint(name):
            return next((b[name] for b in variants if name in b), None)

        return cls(
            validator=validator,
            type=kind,
            default=validator.validate_python(info.get_default(call_default_factory=True)),
            scopes=tuple(policy["scopes"]),
            values=choices,
            min=constraint("minimum"),
            max=constraint("maximum"),
            maxlen=constraint("maxLength"),
            none=any(b.get("type") == "null" for b in variants),
            ids=kind == "choice" and any(b.get("type") == "array" for b in variants),
            risky=policy.get("risky", False),
            discord_editable=policy.get("discord_editable", True),
            hidden=policy.get("hidden", False),
        )

    def valid_text(self):
        if self.type == "choice":
            return ", ".join(self.values) + (", or a list of user ids" if self.ids else "")
        if self.type == "on-off":
            return "true, false"
        if self.type == "number":
            return ("null, " if self.none else "") + f"{self.min}-{self.max}"
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
            return False, "; ".join(e["msg"] for e in exc.errors(include_url=False))


def field_registry(*groups):
    """Expose only field definitions; do not instantiate defaults per layer."""
    result = {}
    for group in groups:
        model = SettingsSchema.model_fields[group].annotation
        if not isinstance(model, type) or not issubclass(model, BaseModel):
            raise TypeError(f"{group} must be a settings model")
        for name, info in model.model_fields.items():
            result[f"{group}.{name}"] = SettingField.from_field(info)
    return result


CORE_GROUPS = ("who", "style", "abilities", "privacy", "limits", "media", "proactive")
CORE = field_registry(*CORE_GROUPS)
TOP = field_registry("presence", "updates", "telemetry", "memory")

"""Validate schedule actions and web-check reports with Pydantic."""

from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    ValidationError,
    field_validator,
    model_validator,
)

from muse_discord.watches.limits import (
    AFTER_MAX,
    CHANGES_MAX,
    FIRST_PRINTABLE_CHARACTER,
    NOT_ON_SCHEDULE_ITEMS,
    NOTE_MAX,
    PLACE_RE,
    SUMMARY_MAX,
    TZ_RE,
    VERSION,
    WEB_EVERY_MAX,
    WEB_EVERY_MIN,
    WHAT_MAX,
    WORD_MAX,
    WORD_MIN,
    WORDS_MAX,
)
from muse_discord.watches.timing import parse_utc


class _Shape(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _utc(value):
    if parse_utc(value) is None:
        raise ValueError("expected an ISO-8601 UTC time before 2100")
    return value


def _word(value):
    if "\n" in value or not WORD_MIN <= len(value.strip()) <= WORD_MAX:
        raise ValueError(f"expected one line of {WORD_MIN} to {WORD_MAX} characters")
    return value


def _true(value):
    if value is not True:
        raise ValueError("must be true")
    return value


UTC = Annotated[str, AfterValidator(_utc)]


ItemId = Annotated[str, Field(pattern=r"^[rw][a-z0-9]{5}$")]


Note = Annotated[str, Field(max_length=NOTE_MAX)]


class _Add(_Shape):
    op: Literal["add"]
    note: Note | None = None


class _Reminder(_Add):
    kind: Literal["remind"]
    at: UTC | None = None
    after: Annotated[int, Field(gt=0, le=AFTER_MAX)] | None = None

    @model_validator(mode="after")
    def one_time(self):
        if ("at" in self.model_fields_set) == ("after" in self.model_fields_set):
            raise ValueError("a reminder takes exactly one of at or after")
        if self.at is None and self.after is None:
            raise ValueError("at or after cannot be null")
        return self


class _Match(_Shape):
    words: Annotated[
        list[Annotated[str, AfterValidator(_word)]], Field(min_length=1, max_length=WORDS_MAX)
    ]
    in_: Annotated[str, Field(alias="in", pattern="^(?:" + PLACE_RE.pattern + ")$")]
    every: bool = False


class _Until(_Add):
    until: UTC | None = None

    @field_validator("until", mode="before")
    @classmethod
    def no_null(cls, value):
        if value is None:
            raise ValueError("until cannot be null")
        return value


class _DiscordWatch(_Until):
    kind: Literal["watch"]
    match: _Match


class _WebWatch(_Until):
    kind: Literal["watch"]
    check: Literal["web"]
    what: Annotated[str, Field(min_length=1, max_length=WHAT_MAX)]
    every: Annotated[int, Field(ge=WEB_EVERY_MIN, le=WEB_EVERY_MAX)]
    repeat: bool = False

    @field_validator("what")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("what must not be blank")
        return value


class _CancelId(_Shape):
    op: Literal["cancel"]
    id: ItemId


class _CancelAll(_Shape):
    op: Literal["cancel"]
    all: Annotated[bool, AfterValidator(_true)]


class _Timezone(_Shape):
    op: Literal["tz"]
    tz: Annotated[str, Field(pattern="^(?:" + TZ_RE.pattern + ")$")] | None


def _action_tag(value):
    if not isinstance(value, dict):
        return None
    op = value.get("op")
    if not isinstance(op, str):
        return None
    if op == "add":
        kind = value.get("kind")
        return "web" if value.get("check") is not None else kind if isinstance(kind, str) else None
    if op == "cancel":
        return "cancel-id" if "id" in value else "cancel-all"
    return op


_Action = Annotated[
    Annotated[_Reminder, Tag("remind")]
    | Annotated[_DiscordWatch, Tag("watch")]
    | Annotated[_WebWatch, Tag("web")]
    | Annotated[_CancelId, Tag("cancel-id")]
    | Annotated[_CancelAll, Tag("cancel-all")]
    | Annotated[_Timezone, Tag("tz")],
    Discriminator(_action_tag),
]


class _Schedule(_Shape):
    v: Annotated[int, Field(ge=1, le=VERSION)]
    changes: Annotated[list[_Action], Field(min_length=1, max_length=CHANGES_MAX)]


class _ScheduledReply(_Shape):
    model_config = ConfigDict(extra="allow", strict=True)
    item: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,19}$")]
    schedule: _Schedule

    @model_validator(mode="after")
    def no_other_actions(self):
        bad = set(self.model_extra or {}) & set(NOT_ON_SCHEDULE_ITEMS)
        if bad:
            raise ValueError("a schedule item carries no " + ", ".join(sorted(bad)))
        return self


def _validation_problems(model, value):
    try:
        model.model_validate(value)
    except ValidationError as exc:
        out = []
        for error in exc.errors(include_url=False, include_input=False):
            loc, message = error["loc"], error["msg"]
            if error["type"].startswith("union_tag"):
                message = "expected an add (remind/watch/web), cancel, or tz action"
            if error["type"] == "extra_forbidden":
                loc = loc[:-1]  # unknown field names can contain secrets too
            out.append(f"{'.'.join(map(str, loc)) or 'schedule'}: {message}")
        return out
    return []


def shape_problems(item):
    """Validate only schedule items; ordinary reply validation is the monitor's."""
    return (
        _validation_problems(_ScheduledReply, item)
        if isinstance(item, dict) and "schedule" in item
        else []
    )


class _Report(_Shape):
    id: ItemId
    nonce: Annotated[str, Field(pattern=r"^[0-9a-f]{32,64}$")]
    result: Literal["met", "not-met", "unknown", "refused"]
    summary: Annotated[str, Field(max_length=SUMMARY_MAX)]
    at: Annotated[float, Field(ge=0)] | None = None  # local report-tool timestamp

    @field_validator("at", mode="before")
    @classmethod
    def no_null_at(cls, value):
        if value is None:
            raise ValueError("at cannot be null")
        return value

    @field_validator("summary")
    @classmethod
    def plain_text(cls, value):
        if any(ord(ch) < FIRST_PRINTABLE_CHARACTER and ch != "\n" for ch in value):
            raise ValueError("summary must be plain text")
        return value


def report_problems(rep):
    return _validation_problems(_Report, rep)

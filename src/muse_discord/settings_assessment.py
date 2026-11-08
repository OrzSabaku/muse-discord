"""Assess the effective audience, disclosure and cost of a settings change.

The caller supplies settings, proposed changes and a directory snapshot. This
module simulates the result once and compares policies without reading files,
writing the overlay or requesting confirmation itself. UI and CLI decide how to
present the result and obtain approval.
"""

from dataclasses import dataclass, field

from muse_discord.muse_settings import (
    BIG_FOR_MEMORY,
    PAIR_LENGTH,
    Place,
    Scope,
    _id_str,
    simulate,
)
from muse_discord.settings_display import (
    ServerInfo,
    chain_names,
    label,
    name_lookup,
    names_from,
    people_text,
    place_name,
)

BIG_FOR_CONVERSATIONAL = 500
BIG_FOR_CAPS = 500
MEDIA_PER_DAY_RISKY = 20
BOT_EXCHANGE_RISKY = 10


@dataclass
class Assessment:
    """assess()'s answer: warnings (plain sentences about risky outcomes;
    any one makes the change risky: confirm first) and notes (plain
    sentences worth showing, not risky)."""

    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def risky(self):
        return bool(self.warnings)

    def to_dict(self):
        return {"risky": self.risky, "warnings": list(self.warnings), "notes": list(self.notes)}

    def __repr__(self):
        return f"Assessment(risky={self.risky}, warnings={self.warnings})"


def _dm_audience(s):
    """Who besides the owners gets answers in DMs: ("none", ()),
    ("set", people), ("shared", ()) or ("any", ())."""
    r = s.resolve(Scope.dm())
    if r["who.replies"] == "off":
        return "none", frozenset()
    pol, allow = r["who.dms_from"], r["who.allow"]
    ignore = {p for p in r["who.ignore"] or [] if not p.startswith("role:")}
    people = None
    if isinstance(pol, list):
        people = set(pol)
    elif pol not in ("shared-servers", "anyone"):
        return "none", frozenset()
    if isinstance(allow, list):
        users = {a for a in allow if not str(a).startswith("role:")}
        people = users if people is None else people & users
    if people is not None:
        people = people - ignore - {str(o) for o in s.owners}
        return ("set", frozenset(people)) if people else ("none", frozenset())
    return ("shared" if pol == "shared-servers" else "any"), frozenset()


def _audience_grew(before, after):
    """The new people (a set), True (an open audience is new), or None."""
    kb, pb = before
    ka, pa = after
    if ka == "none":
        return None
    if ka == "set":
        if kb == "set":
            return set(pa - pb) or None
        # From shared-servers, a named person may share no server.
        return set(pa) if kb in ("none", "shared") else None
    if ka == "shared":
        return True if kb in ("none", "set") else None
    return True if kb != "any" else None


def _effective_cap(s, key, sc):
    caps = [v for _cid, v in s.cap_layers(key, sc)]
    return min(caps) if caps else None


def _join(items):
    items = list(items)
    if len(items) <= PAIR_LENGTH:
        return " and ".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


class ChangeAssessment:
    """Compare effective settings by policy, sharing one simulated result.

    Checks keep their existing order and wording. Helpers describe the
    same server/channel snapshot for every check, including unknown sizes.
    """

    def __init__(self, st, changes, info, names, named):
        self.st = st
        self.named = named
        self.inf = ServerInfo(info)
        self.look_names = chain_names(names, names_from(self.inf, self.st))
        self.look = name_lookup(self.look_names)
        self.after = simulate(self.st, changes)
        self.aud_b, self.aud_a = _dm_audience(self.st), _dm_audience(self.after)
        self.out = Assessment()
        self.gids = set(self.inf.guilds()) | set(self.st.servers_listed())
        for ch in changes:
            if ch.place.guild_id:
                self.gids.add(ch.place.guild_id)
        self.gids = sorted(g for g in self.gids if _id_str(g))

        self.servers = {g: Scope.server(g) for g in self.gids}
        self.channels = {}
        for g in self.gids:
            for c in self.st.channels_listed(g):
                self.channels[(g, c)] = Scope.server(
                    g, channel_id=c, category_id=self.inf.category_of(g, c)
                )
        for ch in changes:
            if ch.place is not None and ch.place.kind == "channel":
                self.channels[(ch.place.guild_id, ch.place.channel_id)] = ch.place.scope(ch.key)
        self.later = Scope("all_servers")  # servers it joins later / not listed
        self.total = sum(self.inf.members(g) or 0 for g in self.inf.guilds())

    def run(self):
        self.external_apps()
        self.dm_audience()
        self.bot_dm_access()
        self.memory_access()
        self.generation_access()
        self.restricted_audience()
        self.global_pause()
        self.reply_caps()
        self.server_conversations()
        self.bot_conversations()
        self.notes()
        return self.out

    def external_apps(self):
        levels = ("off", "read-only", "confirm", "write", "unconditional")
        scope = Scope.everywhere()
        before_apps = self.st.resolve(scope)["abilities.external_apps"]
        after_apps = self.after.resolve(scope)["abilities.external_apps"]
        if levels.index(after_apps) > levels.index(before_apps):
            self.out.warnings.append(
                f"External app access increases from {before_apps} to {after_apps}. "
                "App-specific limits still apply. Unconditional approval skips "
                "approval for registered writes, deletes and sends."
            )

    def gname(self, g):
        return self.look(g) or f"server {g}"

    def gdesc(self, g):
        n = self.inf.members(g)
        return (
            f"{self.gname(g)} ({n:,} members)"
            if n is not None
            else f"{self.gname(g)} (size unknown)"
        )

    def large(self, g, limit):
        """Known to have more than limit members, or of unknown size
        (fail closed: no member counts is no reason to stay quiet)."""
        n = self.inf.members(g)
        return n is None or n > limit

    def bot_dms(self, s):
        r = s.resolve(Scope.dm())
        return (
            r["who.replies"] != "off"
            and r["who.bots"] != "ignore"
            and r["who.dms_from"] in ("anyone", "shared-servers")
            and r["who.allow"] == "everyone"
        )

    def mem(self, s, sc):
        return s.get("privacy.memory", sc) == "everything"

    def media(self, s, sc):
        if not s.get("media.enabled", sc):
            return None, False
        who = s.get("media.who", sc)
        cap = _effective_cap(s, "media.per_day", sc)
        return who, who != "owners" and (cap is None or cap > MEDIA_PER_DAY_RISKY)

    def where(self, k):
        if k == ("DMs",):
            return "DMs"
        if k == ("later",):
            return "servers it joins later"
        if len(k) == 1:
            return self.gname(k[0])
        return place_name(Place.channel(*k), self.look_names)

    def paused_all(self, s):
        return (
            s.get("who.replies", Scope.dm()) == "off" and s.get("who.replies", self.later) == "off"
        )

    def botx(self, s, sc):
        lim = s.get("who.bot_exchange_limit", sc)
        return s.get("who.bots", sc) != "ignore" and (lim is None or lim > BOT_EXCHANGE_RISKY)

    def allow_ineffective(self, s):
        r = s.resolve(Scope.dm())
        return (
            r["who.replies"] != "off"
            and r["who.dms_from"] == "owners"
            and isinstance(r["who.allow"], list)
            and bool(r["who.allow"])
        )

    def dm_audience(self):
        # DMs open to more people.
        grew = _audience_grew(self.aud_b, self.aud_a)
        if isinstance(grew, set) and self.named:
            grew = grew - {str(n) for n in self.named} or None
        if grew is True and self.aud_a[0] == "any":
            self.out.warnings.append(
                "Anyone on Discord, strangers included, can DM "
                "the bot and get answers. Each answer costs a "
                "Muse turn."
            )
        elif grew is True:
            self.out.warnings.append(
                "Anyone who shares a server with the bot can DM it and get answers, not only you."
            )
        elif grew:
            self.out.warnings.append(
                f"{people_text(sorted(grew), self.look)} can DM the bot and get answers."
            )

    def bot_dm_access(self):
        # Other bots in DMs.

        if self.bot_dms(self.after) and not self.bot_dms(self.st):
            self.out.warnings.append(
                "Other bots can DM the bot and get answers; two "
                "bots can keep answering each other, each "
                "answer a Muse turn."
            )

    def memory_access(self):
        # Memory: anything it remembers.

        newly = [
            g
            for g, sc in self.servers.items()
            if self.mem(self.after, sc) and not self.mem(self.st, sc)
        ]
        if newly or (self.mem(self.after, self.later) and not self.mem(self.st, self.later)):
            places = [self.gdesc(g) for g in newly] or ["servers it joins later"]
            big = [g for g in newly if self.large(g, BIG_FOR_MEMORY)]
            text = (
                f"In {_join(places)}, replies may draw on anything it "
                f"remembers about you (your job, location, finances and "
                f"other saved memory), and anyone there can ask the bot "
                f"about you."
            )
            if big:
                text = (
                    f"More than {BIG_FOR_MEMORY} members (or size unknown): "
                    f"{_join(self.gdesc(g) for g in big)}. " + text
                )
            self.out.warnings.append(text)
        if (
            self.aud_a[0] in ("shared", "any")
            and self.mem(self.after, Scope.dm())
            and not (self.aud_b[0] in ("shared", "any") and self.mem(self.st, Scope.dm()))
        ):
            self.out.warnings.append(
                "People who DM the bot, strangers included, may "
                "get replies drawing on anything it remembers "
                "about you."
            )

    def generation_access(self):
        # Image and video generation.
        scopes: dict[tuple[str, ...], Scope] = {(g,): sc for g, sc in self.servers.items()}
        scopes[("DMs",)] = Scope.dm()
        scopes[("later",)] = self.later
        for (g, c), sc in self.channels.items():
            scopes[(g, c)] = sc

        everyone, uncapped = [], []
        for k, sc in scopes.items():
            wb, ub = self.media(self.st, sc)
            wa, ua = self.media(self.after, sc)
            if wa == "everyone" and wb != "everyone":
                everyone.append(self.where(k))
            if ua and not ub:
                uncapped.append(self.where(k))
        if everyone:
            self.out.warnings.append(
                f"Anyone in {_join(everyone)} can ask for "
                f"generated images and video. Generation "
                f"can cost real money."
            )
        if uncapped:
            self.out.warnings.append(
                f"Generations per day: more than "
                f"{MEDIA_PER_DAY_RISKY} (or no limit) for people other "
                f"than you in {_join(uncapped)}. Generation can cost real "
                f"money."
            )

    def restricted_audience(self):
        # Only answer these people: newly limiting places.
        allow_scopes = [(self.gname(g), sc) for g, sc in self.servers.items()] + [
            ("servers it joins later", self.later)
        ]
        if self.aud_b[0] != "none":  # DMs answered someone besides the owners
            allow_scopes.append(("DMs", Scope.dm()))
        silenced, who = [], None
        for name, sc in allow_scopes:
            a = self.after.get("who.allow", sc)
            if isinstance(a, list) and self.st.get("who.allow", sc) == "everyone":
                silenced.append(name)
                who = who or a
        if silenced:
            n = len(silenced)
            self.out.warnings.append(
                f"Only {people_text(who, self.look) if who else 'nobody'} and you "
                f"will get answers in {n} place{'s' if n != 1 else ''} "
                f"({_join(silenced)}); everyone else there is ignored."
            )

    def global_pause(self):
        # Pause everywhere.

        if self.paused_all(self.after) and not self.paused_all(self.st):
            own = [
                self.gname(g)
                for g, sc in self.servers.items()
                if self.after.get("who.replies", sc) != "off"
            ]
            text = (
                "Pause everywhere: the bot stops answering everyone, in DMs "
                "and in every server without its own setting, until you "
                "resume."
            )
            if own:
                text += (
                    f" {_join(own)} set{'s' if len(own) == 1 else ''} "
                    f"its own and keep{'s' if len(own) == 1 else ''} "
                    f"answering."
                )
            self.out.warnings.append(text)

    def reply_caps(self):
        # A reply cap removed where the bot is large.
        for key in ("limits.per_day", "limits.per_person_per_day"):
            lost = []
            for g, sc in self.servers.items():
                if (
                    self.large(g, BIG_FOR_CAPS)
                    and self.st.cap_layers(key, sc)
                    and not self.after.cap_layers(key, sc)
                ):
                    lost.append(self.gdesc(g))
            if (
                self.aud_a[0] in ("shared", "any")
                and self.st.cap_layers(key, Scope.dm())
                and not self.after.cap_layers(key, Scope.dm())
            ):
                lost.append("DMs (open to strangers)")
            ev = Scope.everywhere()
            unknown = not self.inf.guilds() or any(
                self.inf.members(g) is None for g in self.inf.guilds()
            )
            if (
                (self.total > BIG_FOR_CAPS or unknown)
                and not lost
                and self.st.cap_layers(key, ev)
                and not self.after.cap_layers(key, ev)
            ):
                lost.append(
                    "the bot as a whole (size unknown)"
                    if not self.total
                    else f"the bot as a whole ({self.total:,} members"
                    + (", some of unknown size)" if unknown else ")")
                )
            if lost:
                self.out.warnings.append(f"{label(key)}: no limit any more in {_join(lost)}.")

    def server_conversations(self):
        # Chatty in a big server.
        big = []
        for (g, c), sc in [((g, None), sc) for g, sc in self.servers.items()] + list(
            self.channels.items()
        ):
            if not self.large(g, BIG_FOR_CONVERSATIONAL):
                continue
            if (
                self.after.get("who.replies", sc) == "conversational"
                and self.st.get("who.replies", sc) != "conversational"
            ):
                if c:
                    n = self.look(c)
                    cname = (n if str(n).startswith("#") else f"#{n}") if n else f"channel {c}"
                    big.append(f"{cname} in {self.gdesc(g)}")
                else:
                    big.append(self.gdesc(g))
        if big:
            self.out.warnings.append(
                f"Chatty in {_join(big)}: anyone the bot answers there can keep "
                f"talking to it for 2 minutes without @mentioning it."
            )

    def bot_conversations(self):
        # Replies in a row to other bots.

        loose = [
            n
            for n, sc in [("Defaults", Scope.everywhere())]
            + [(self.gname(g), sc) for g, sc in self.servers.items()]
            if self.botx(self.after, sc) and not self.botx(self.st, sc)
        ]
        if loose:
            self.out.warnings.append(
                f"Two bots can keep answering each other in "
                f"{_join(loose)} with no limit (or more than "
                f"{BOT_EXCHANGE_RISKY} in a row), each answer "
                f"a Muse turn."
            )

    def notes(self):
        # Notes.

        if self.allow_ineffective(self.after) and not self.allow_ineffective(self.st):
            self.out.notes.append(
                "Only answer these people does not let anyone DM "
                "the bot while Who can DM it is Only you; choose "
                "You and people you pick for that."
            )


def assess(st, changes, info=None, names=None, named=()):
    """Assess newly introduced risks in effective settings, including undo.

    Caller-supplied server sizes and names describe the same snapshot for
    every policy. Unknown sizes are treated conservatively.
    """
    return ChangeAssessment(st, changes, info, names, named).run()

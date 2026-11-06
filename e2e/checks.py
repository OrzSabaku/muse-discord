#!/usr/bin/env python3
"""
Deterministic transcript checks for the e2e harness.

evaluate(scenario, transcript) runs every check listed under the
scenario's expect.checks against the transcript run.py wrote (schema in
the e2e spec: messages oldest first, labels -> message ids, the SUT's
quarantine.json and mention_ledger.json under sut_files) and returns one
{"name", "ok", "detail"} per check, in order. Pure functions: no I/O, no
network, no clock.

Beyond the channel itself, checks read what run.py captured from the
SUT: sut_files.holds (state.json's never_share holds),
sut_files.overlay (config.discord.yaml, parsed),
sut_files.worker_items (the queue items the fake worker handled, with
follow_up and the settings card), and monitor_log (monitor.log written
during the scenario; monitor_log_tail when that is missing).

Threads. run.py merges the history of every thread a scenario started
into messages (one list, sorted by id); a message from a thread has
`thread` set to the thread's label. Every check sees them like channel
messages: a label of a message sent in a thread works anywhere a label
does, and the SUT's replies in a thread count toward replies_to,
sut_reply_count and the rest.

A "SUT message" is any transcript message with author "sut". A SUT
message "replies to" a label when its Discord reply reference
(reply_to) is that label's message id. Every detail string names what
was expected and what was found (ids and a snippet of each message), so
a failure in the PR comment is readable without opening the transcript.
"""
import re
from datetime import datetime, timezone

DISCORD_EPOCH_MS = 1420070400000
SNIPPET = 60


# ---------------------------------------------------------------------------
# Transcript helpers
# ---------------------------------------------------------------------------

class CheckError(Exception):
    """The transcript lacks something the check needs (a label that was
    never sent, a missing SUT file). Reported as a failed check."""


def _sut(tr):
    return [m for m in tr.get("messages") or [] if m.get("author") == "sut"]


def _label_id(tr, label):
    mid = (tr.get("labels") or {}).get(label)
    if not mid:
        raise CheckError(f"label {label!r} has no message id in the "
                         f"transcript (was the step sent?)")
    return str(mid)


def _snowflake_secs(mid):
    try:
        return ((int(mid) >> 22) + DISCORD_EPOCH_MS) / 1000.0
    except (TypeError, ValueError):
        return None


def _iso_secs(ts):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _time_of(tr, mid):
    """Seconds since the epoch a message was posted: its transcript ts,
    else the time encoded in its snowflake id."""
    for m in tr.get("messages") or []:
        if str(m.get("id")) == mid:
            t = _iso_secs(m.get("ts"))
            if t is not None:
                return t
    t = _snowflake_secs(mid)
    if t is None:
        raise CheckError(f"cannot tell when message {mid} was posted")
    return t


def _msg_time(tr, m):
    t = _iso_secs(m.get("ts"))
    return t if t is not None else _time_of(tr, str(m.get("id")))


def _show(msgs):
    """'id "snippet" (reply to X)' for each message, for details."""
    if not msgs:
        return "none"
    out = []
    for m in msgs:
        text = (m.get("content") or "").replace("\n", " ")
        if len(text) > SNIPPET:
            text = text[:SNIPPET] + "..."
        rt = m.get("reply_to")
        out.append(f'{m.get("id")} "{text}" '
                   f'({"reply to " + str(rt) if rt else "not a reply"})')
    return "; ".join(out)


def _count_ok(n, spec):
    if "eq" in spec:
        return n == spec["eq"], f"expected {spec['eq']}"
    want = []
    ok = True
    if "min" in spec:
        ok &= n >= spec["min"]
        want.append(f"at least {spec['min']}")
    if "max" in spec:
        ok &= n <= spec["max"]
        want.append(f"at most {spec['max']}")
    return ok, "expected " + " and ".join(want)


def _replies_to(m, mid):
    return str(m.get("reply_to") or "") == mid


# ---------------------------------------------------------------------------
# Checks: each takes (arg, transcript) and returns (ok, detail).
# ---------------------------------------------------------------------------

def check_sut_reply_count(spec, tr):
    sut = _sut(tr)
    ok, want = _count_ok(len(sut), spec)
    return ok, f"{len(sut)} SUT message(s), {want}: {_show(sut)}"


def check_replies_to(label, tr):
    mid = _label_id(tr, label)
    sut = _sut(tr)
    if not sut:
        return False, f"no SUT messages; expected replies to {label} ({mid})"
    bad = [m for m in sut if not _replies_to(m, mid)]
    if bad:
        return False, (f"{len(bad)} of {len(sut)} SUT message(s) do not "
                       f"reply to {label} ({mid}): {_show(bad)}")
    return True, f"all {len(sut)} SUT message(s) reply to {label} ({mid})"


def check_replies_to_any(labels, tr):
    ids = {_label_id(tr, lb): lb for lb in labels}
    sut = _sut(tr)
    names = ", ".join(f"{lb} ({mid})" for mid, lb in ids.items())
    if not sut:
        return False, f"no SUT messages; expected replies to one of {names}"
    bad = [m for m in sut if str(m.get("reply_to") or "") not in ids]
    if bad:
        return False, (f"{len(bad)} of {len(sut)} SUT message(s) reply to "
                       f"none of {names}: {_show(bad)}")
    return True, f"all {len(sut)} SUT message(s) reply to one of {names}"


def check_no_sut_reply_to(label, tr):
    mid = _label_id(tr, label)
    bad = [m for m in _sut(tr) if _replies_to(m, mid)]
    if bad:
        return False, (f"{len(bad)} SUT message(s) reply to {label} "
                       f"({mid}), expected none: {_show(bad)}")
    return True, f"no SUT message replies to {label} ({mid})"


def check_no_sut_before(label, tr):
    mid = _label_id(tr, label)
    t0 = _time_of(tr, mid)

    def before(m):
        # Snowflakes order messages exactly; ts is the fallback.
        try:
            return int(m["id"]) < int(mid)
        except (KeyError, TypeError, ValueError):
            return _msg_time(tr, m) < t0

    bad = [m for m in _sut(tr) if before(m)]
    if bad:
        return False, (f"{len(bad)} SUT message(s) posted before {label} "
                       f"({mid}), expected none: {_show(bad)}")
    return True, f"no SUT message before {label} ({mid})"


def check_latency(spec, tr):
    label, limit = spec["from"], spec["max"]
    mid = _label_id(tr, label)
    replies = [m for m in _sut(tr) if _replies_to(m, mid)]
    if not replies:
        return False, (f"no SUT message replies to {label} ({mid}) at all; "
                       f"SUT messages: {_show(_sut(tr))}")
    t0 = _time_of(tr, mid)
    first = min(replies, key=lambda m: _msg_time(tr, m))
    took = _msg_time(tr, first) - t0
    ok = took <= limit
    return ok, (f"first reply to {label} ({mid}) came after {took:.1f}s, "
                f"limit {limit}s: {_show([first])}")


def check_max_chars(limit, tr):
    sut = _sut(tr)
    bad = [m for m in sut if len(m.get("content") or "") > limit]
    lens = [len(m.get("content") or "") for m in sut]
    if bad:
        return False, (f"{len(bad)} SUT message(s) longer than {limit} "
                       f"chars (lengths {lens}): {_show(bad)}")
    return True, f"SUT message lengths {lens}, all <= {limit}"


def check_total_chars(spec, tr):
    sut = _sut(tr)
    lens = [len(m.get("content") or "") for m in sut]
    ok, want = _count_ok(sum(lens), spec)
    return ok, (f"SUT messages total {sum(lens)} chars (lengths {lens}), "
                f"{want}")


def check_content_matches(arg, tr):
    """Every SUT message matches the regex; with {label, pattern}, every
    SUT message replying to that label (and there is one)."""
    sut, which = _sut(tr), "SUT message(s)"
    pattern = arg
    if isinstance(arg, dict):
        pattern, mid = arg["pattern"], _label_id(tr, arg["label"])
        sut = [m for m in sut if _replies_to(m, mid)]
        which = f"SUT repl(ies) to {arg['label']} ({mid})"
    if not sut:
        return False, f"no {which}; expected them to match /{pattern}/"
    bad = [m for m in sut if not re.search(pattern, m.get("content") or "")]
    if bad:
        return False, (f"{len(bad)} of {len(sut)} {which} do not "
                       f"match /{pattern}/: {_show(bad)}")
    return True, f"all {len(sut)} {which} match /{pattern}/"


# A raw @everyone / @here in sent text means the monitor's zero-width
# neutralization (sanitize_reply) did not run, even if allowed_mentions
# kept it from pinging.
_RAW_MASS = re.compile(r"@(everyone|here)\b")


def check_no_mass_mentions(_arg, tr):
    problems = []
    for m in _sut(tr):
        why = []
        if m.get("mention_everyone"):
            why.append("mention_everyone is true")
        if m.get("mention_roles"):
            why.append(f"mention_roles {m['mention_roles']}")
        raw = _RAW_MASS.search(m.get("content") or "")
        if raw:
            why.append(f"raw {raw.group(0)} in the text (not neutralized)")
        if why:
            problems.append(f"{m.get('id')}: {', '.join(why)}")
    if problems:
        return False, "mass mention(s) went out: " + "; ".join(problems)
    return True, f"no mass mentions in {len(_sut(tr))} SUT message(s)"


def check_no_duplicate_replies(_arg, tr):
    seen, dups = {}, []
    for m in _sut(tr):
        key = (str(m.get("reply_to") or ""), m.get("content") or "")
        if key in seen:
            dups.append((seen[key], m))
        else:
            seen[key] = m
    if dups:
        pairs = "; ".join(f"{a.get('id')} and {b.get('id')} (reply to "
                          f"{a.get('reply_to') or 'nothing'}, \""
                          f"{(a.get('content') or '')[:SNIPPET]}\")"
                          for a, b in dups)
        return False, f"{len(dups)} duplicate SUT message(s): {pairs}"
    return True, f"no duplicates among {len(_sut(tr))} SUT message(s)"


def check_mentions(spec, tr):
    """Some SUT message replying to the label both writes <@id> for the
    driver and actually mentions them (the id is in its mentions). The
    text test matters: Discord also lists the replied-to author in
    mentions when a reply pings, so mentions alone would pass for the
    author of the label's message without any <@id> in the reply."""
    label, who = spec["label"], spec["user"]
    mid = _label_id(tr, label)
    uid = str((tr.get("ids") or {}).get(who.upper()) or "")
    if not uid:
        raise CheckError(f"transcript ids has no {who.upper()}")
    replies = [m for m in _sut(tr) if _replies_to(m, mid)]
    if not replies:
        return False, (f"no SUT message replies to {label} ({mid}); SUT "
                       f"messages: {_show(_sut(tr))}")
    token = re.compile(rf"<@!?{re.escape(uid)}>")
    for m in replies:
        in_text = bool(token.search(m.get("content") or ""))
        pinged = uid in [str(x) for x in m.get("mentions") or []]
        if in_text and pinged:
            return True, (f"SUT reply {m.get('id')} to {label} mentions "
                          f"driver {who} ({uid})")
    return False, (f"no SUT reply to {label} ({mid}) both writes <@{uid}> "
                   f"and mentions driver {who}: "
                   + "; ".join(f"{m.get('id')} mentions "
                               f"{m.get('mentions') or []} "
                               f"\"{(m.get('content') or '')[:SNIPPET]}\""
                               for m in replies))


def check_quarantined(label, tr):
    mid = _label_id(tr, label)
    q = (tr.get("sut_files") or {}).get("quarantine")
    if q is None:
        return False, (f"no quarantine.json in the SUT dir; expected "
                       f"{label} ({mid}) to be quarantined")
    if not isinstance(q, list):
        return False, f"quarantine.json is not a list: {str(q)[:120]}"
    for e in q:
        if not isinstance(e, dict):
            continue
        ids = [str(e.get("message_id"))] + [
            str(f.get("message_id")) for f in e.get("fragments") or []
            if isinstance(f, dict)]
        if mid in ids:
            return True, (f"{label} ({mid}) quarantined (matched "
                          f"{e.get('matched_pattern')!r})")
    held = [str(e.get("message_id")) for e in q if isinstance(e, dict)]
    return False, (f"{label} ({mid}) is not in quarantine.json "
                   f"({len(q)} entries: {held})")


def check_ledger_status(spec, tr):
    label, want = spec["label"], spec["status"]
    mid = _label_id(tr, label)
    ledger = (tr.get("sut_files") or {}).get("ledger")
    if not isinstance(ledger, dict) or not isinstance(
            ledger.get("entries"), dict):
        return False, (f"no usable mention_ledger.json in the SUT dir "
                       f"(got {str(ledger)[:120]})")
    entry = ledger["entries"].get(mid)
    if not isinstance(entry, dict):
        return False, f"{label} ({mid}) has no ledger entry; expected {want}"
    got = entry.get("status")
    reason = f", reason {entry.get('reason')!r}" if entry.get("reason") \
        else ""
    if got != want:
        return False, (f"{label} ({mid}) ledger status {got!r}{reason}, "
                       f"expected {want!r}")
    return True, f"{label} ({mid}) ledger status {got!r}{reason}"


def check_converse_turns(spec, tr):
    n = tr.get("converse_turns")
    if not isinstance(n, int) or isinstance(n, bool):
        return False, (f"transcript has no converse_turns count (got "
                       f"{n!r}); did the converse step run?")
    ok, want = _count_ok(n, spec)
    return ok, f"{n} SUT turn(s) during the converse step, {want}"


def _scenario_log(tr):
    log = tr.get("monitor_log")
    if isinstance(log, str) and log:
        return log, "the scenario's monitor.log"
    return str(tr.get("monitor_log_tail") or ""), "the monitor.log tail"


def check_log_contains(pattern, tr):
    log, what = _scenario_log(tr)
    m = re.search(pattern, log, re.M)
    if m:
        line = log[log.rfind("\n", 0, m.start()) + 1:].split("\n", 1)[0]
        return True, f"/{pattern}/ found in {what}: {line[:200]!r}"
    return False, (f"/{pattern}/ not found in {what} "
                   f"({len(log.splitlines())} lines)")


def check_log_lacks(pattern, tr):
    log, what = _scenario_log(tr)
    hits = [ln for ln in log.splitlines() if re.search(pattern, ln)]
    if hits:
        return False, (f"/{pattern}/ found in {what}, expected none: "
                       f"{len(hits)} line(s), first {hits[0][:200]!r}")
    return True, f"/{pattern}/ absent from {what}"


def check_held(label, tr):
    """A never_share hold in the SUT's state.json answers the label's
    message (the held reply's item or reply_to is its id). The detail
    never quotes the held text."""
    mid = _label_id(tr, label)
    holds = (tr.get("sut_files") or {}).get("holds")
    if not isinstance(holds, dict) or not holds:
        return False, (f"no never_share holds in the SUT's state.json; "
                       f"expected one for {label} ({mid})")
    for hid, h in holds.items():
        item = (h.get("item") if isinstance(h, dict) else None) or {}
        if mid in (str(item.get("item") or ""),
                   str(item.get("reply_to") or "")):
            return True, f"{label} ({mid}) is held (hold {hid})"
    seen = [str((h.get("item") or {}).get("item"))
            for h in holds.values() if isinstance(h, dict)]
    return False, (f"{label} ({mid}) is not held ({len(holds)} hold(s), "
                   f"for items {seen})")


def _worker_item(tr, mid):
    """The newest worker-recorded queue item whose trigger or a burst
    fragment is mid."""
    items = (tr.get("sut_files") or {}).get("worker_items")
    if not isinstance(items, list):
        raise CheckError("no worker items recorded (sut_files."
                         "worker_items); queue_item needs the tier-1 "
                         "fake worker")
    found = None
    for it in items:
        if not isinstance(it, dict):
            continue
        ids = [str(it.get("message_id"))] + [
            str(x) for x in it.get("fragment_ids") or []]
        if mid in ids:
            found = it
    return found, len(items)


def check_queue_item(spec, tr):
    label = spec["label"]
    mid = _label_id(tr, label)
    item, n = _worker_item(tr, mid)
    if item is None:
        return False, (f"{label} ({mid}) was never queued (the worker saw "
                       f"{n} item(s), none with this message)")
    bad, good = [], []
    for k, want in spec.items():
        if k in ("label", "settings"):
            continue
        got = item.get(k)
        if isinstance(want, bool):
            got = bool(got)
        (good if got == want else bad).append(
            f"{k} {got!r}" + ("" if got == want else f" (expected {want!r})"))
    card = item.get("settings")
    for k, want in (spec.get("settings") or {}).items():
        got = card.get(k) if isinstance(card, dict) else None
        (good if got == want else bad).append(
            f"settings.{k} {got!r}"
            + ("" if got == want else f" (expected {want!r})"))
    where = f"queue item {item.get('message_id')} for {label} ({mid})"
    if bad:
        return False, f"{where}: " + ", ".join(bad + good)
    return True, f"{where}: " + ", ".join(good)


def check_monitor_restarts(spec, tr):
    """How many times the harness (re)started the monitor while the
    scenario's steps ran (the restart for its config, before the first
    step, is not counted)."""
    n = tr.get("monitor_restarts")
    if not isinstance(n, int) or isinstance(n, bool):
        return False, (f"transcript has no monitor_restarts count (got "
                       f"{n!r})")
    ok, want = _count_ok(n, spec)
    return ok, f"{n} monitor (re)start(s) during the steps, {want}"


# A channel's overlay keys use short names (muse_settings.CHANNEL_SHORT).
_CHANNEL_SHORT = {"replies": "who.replies", "notes": "style.notes"}
_CHANNEL_KEY_RE = re.compile(r'^(servers\."[^"]*"\.channels\."[^"]*"\.)(.+)$')


def _overlay_key(k):
    """An overlay key with a channel's short name spelled out
    (servers."1".channels."2".replies -> ....who.replies)."""
    m = _CHANNEL_KEY_RE.match(str(k))
    if m and m[2] in _CHANNEL_SHORT:
        return m[1] + _CHANNEL_SHORT[m[2]]
    return str(k)


def check_overlay_lacks(suffix, tr):
    """No entry in the SUT's config.discord.yaml (the Discord-side
    changes) for a key ending in suffix ("who.replies" matches it at
    every place). A missing file has no entries."""
    ov = (tr.get("sut_files") or {}).get("overlay")
    if ov is None:
        return True, (f"no config.discord.yaml in the SUT dir: nothing "
                      f"set, so no {suffix}")
    if not isinstance(ov, dict):
        return False, f"config.discord.yaml is not a mapping: {str(ov)[:120]}"
    values = ov.get("values") or {}
    if not isinstance(values, dict):
        return False, f"config.discord.yaml values: {str(values)[:120]}"
    hits = [k for k in values
            if _overlay_key(k) == suffix
            or _overlay_key(k).endswith("." + suffix)]
    if hits:
        return False, (f"config.discord.yaml sets {suffix}: "
                       + ", ".join(f"{k} = {values[k].get('value')!r}"
                                   if isinstance(values[k], dict) else k
                                   for k in hits))
    return True, (f"config.discord.yaml has no {suffix} entry "
                  f"({len(values)} entr{'y' if len(values) == 1 else 'ies'})")


CHECKS = {
    "sut_reply_count": check_sut_reply_count,
    "replies_to": check_replies_to,
    "replies_to_any": check_replies_to_any,
    "no_sut_reply_to": check_no_sut_reply_to,
    "no_sut_before": check_no_sut_before,
    "latency": check_latency,
    "max_chars": check_max_chars,
    "total_chars": check_total_chars,
    "content_matches": check_content_matches,
    "no_mass_mentions": check_no_mass_mentions,
    "no_duplicate_replies": check_no_duplicate_replies,
    "mentions": check_mentions,
    "quarantined": check_quarantined,
    "ledger_status": check_ledger_status,
    "converse_turns": check_converse_turns,
    "log_contains": check_log_contains,
    "log_lacks": check_log_lacks,
    "held": check_held,
    "queue_item": check_queue_item,
    "monitor_restarts": check_monitor_restarts,
    "overlay_lacks": check_overlay_lacks,
}


def evaluate(scenario, transcript):
    """One {"name", "ok", "detail"} per entry in expect.checks, in order.
    Never raises: an unknown check, a malformed entry, or a transcript
    missing what a check needs is a failed check with the reason."""
    results = []
    checks = ((scenario or {}).get("expect") or {}).get("checks") or []
    for chk in checks:
        if not isinstance(chk, dict) or len(chk) != 1:
            results.append({"name": str(chk), "ok": False,
                            "detail": "malformed check entry"})
            continue
        (name, arg), = chk.items()
        fn = CHECKS.get(name)
        if fn is None:
            results.append({"name": name, "ok": False,
                            "detail": "unknown check"})
            continue
        try:
            ok, detail = fn(arg, transcript or {})
        except CheckError as e:
            ok, detail = False, str(e)
        except Exception as e:  # a check must never take the run down
            ok, detail = False, f"check crashed: {type(e).__name__}: {e}"
        results.append({"name": name, "ok": bool(ok), "detail": detail})
    return results

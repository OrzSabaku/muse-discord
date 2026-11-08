"""Issue budgeted web checks and consume nonce-bound reports exactly once."""

import hashlib
import hmac
import secrets
import time
from datetime import UTC as UTC_ZONE
from datetime import datetime

from muse_discord.watches.limits import (
    WEB_BACKOFF_MAX,
    WEB_CHECK_SECS,
    WEB_MAX_FAILS,
    WEB_REPEAT_SECS,
    WEB_TOTAL_PER_DAY,
)
from muse_discord.watches.schema import report_problems
from muse_discord.watches.timing import iso


def _utc_day(now):
    return datetime.fromtimestamp(now, UTC_ZONE).strftime("%Y-%m-%d")


def _backoff(rec, now):
    rec["next_check"] = now + min(
        WEB_BACKOFF_MAX, rec.get("every", 3600) * 2 ** max(0, rec.get("fails", 0))
    )


def _not_owners(r, owners):
    """A web watch set before they were the owners' only, or by someone
    no longer among owners (the owners now, when given)."""
    return not r.get("owner") or (owners is not None and str(r.get("requester")) not in owners)


def web_issue(data, now=None, owners=None):
    """Expire checks nobody reported on (a failure each: back off, and
    give up after WEB_MAX_FAILS), then issue the due ones, each with a
    fresh nonce, within today's budgets (over budget: tomorrow). A
    watch whose requester is not among owners (the owners now, when
    given) ends. Returns [(id, record)] of the watches that ended
    (removed)."""
    now = time.time() if now is None else now
    day = _utc_day(now)
    used = data.setdefault("web", {})
    if used.get("day") != day:
        used.clear()
        used.update({"day": day, "total": 0})
    gave_up = []
    for i, r in list(data["items"].items()):
        if not isinstance(r, dict) or r.get("kind") != "watch-web" or r.get("until", 0) <= now:
            continue
        if _not_owners(r, owners):
            # It ends, its check withdrawn (the requester is told, as for
            # one that gave up).
            gave_up.append((i, data["items"].pop(i)))
            continue
        chk = r.get("check")
        if chk and chk.get("expires", 0) <= now:
            r["check"] = None
            r["fails"] = r.get("fails", 0) + 1
            if r["fails"] >= WEB_MAX_FAILS:
                gave_up.append((i, data["items"].pop(i)))
                continue
            _backoff(r, now)
            continue
        if chk or r.get("next_check", 0) > now:
            continue
        if used["total"] >= WEB_TOTAL_PER_DAY:
            tomorrow = (
                datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC_ZONE).timestamp() + 86400
            )
            r["next_check"] = tomorrow + secrets.randbelow(600)
            continue
        used["total"] += 1
        r["check"] = {
            "nonce": secrets.token_hex(16),
            "issued": now,
            "expires": now + WEB_CHECK_SECS,
        }
        r["checks"] = r.get("checks", 0) + 1
    return gave_up


def web_due(data, now=None):
    """What web_watches_due.json lists: every live check, as data for the
    checker (the condition, the id, the nonce, when it expires), the
    hook's lease key, and `group`, an opaque tag per requester: the hook
    never hands two requesters' checks (and nonces) to one checker run,
    so one person's page cannot report on another's watch."""
    now = time.time() if now is None else now
    out = []
    for i, r in sorted(data["items"].items()):
        chk = r.get("check") if isinstance(r, dict) else None
        if r.get("kind") == "watch-web" and chk and chk.get("expires", 0) > now:
            out.append(
                {
                    "id": i,
                    "key": f"{i}:{int(chk['issued'])}",
                    "group": hashlib.sha256(str(r.get("requester")).encode()).hexdigest()[:16],
                    "nonce": chk["nonce"],
                    "what": r.get("what"),
                    "expires": iso(chk["expires"]),
                }
            )
    return out


def _web_report_problem(r, chk, now, owners, nonce):
    if not r or r.get("kind") != "watch-web" or r.get("until", 0) <= now:
        return "no such active web watch"
    if _not_owners(r, owners):
        return "its requester is not an owner"
    if not chk or chk.get("expires", 0) <= now:
        return "no live check (expired or already reported)"
    if not hmac.compare_digest(str(chk.get("nonce")).encode(), nonce.encode()):
        return "wrong nonce"
    return None


def _apply_web_result(data, i, r, result, now):
    if result == "met":
        r["fails"] = 0
        r["last_met"] = now
        if not r.get("repeat"):
            del data["items"][i]
        else:
            r["next_check"] = now + max(r.get("every", 0), WEB_REPEAT_SECS)
        return "met", i, dict(r)
    if result == "refused":
        return "refused", i, data["items"].pop(i)
    if result == "not-met":
        r["fails"] = 0
        r["next_check"] = now + r.get("every", 3600)
        return "not-met", i, r
    r["fails"] = r.get("fails", 0) + 1
    if r["fails"] >= WEB_MAX_FAILS:
        return "gave-up", i, data["items"].pop(i)
    _backoff(r, now)
    return "unknown", i, r


def web_apply(data, rep, now=None, owners=None):
    """Apply one check report: (result, id, record) for a report naming
    a live check of an active web watch with that check's nonce (spent
    now), else (None, id, why). met: the watch ends unless it repeats, then
    stays quiet WEB_REPEAT_SECS; not-met: the next check in `every`;
    unknown: a failure (back off; ends after WEB_MAX_FAILS, result
    "gave-up"); refused (the checker judged the condition to be more
    than a condition: instructions, a request to act): the watch ends."""
    now = time.time() if now is None else now
    if report_problems(rep):
        return None, None, "malformed"
    i = rep["id"]
    r = data["items"].get(i)
    chk = (r or {}).get("check") if isinstance(r, dict) else None
    reason = _web_report_problem(r, chk, now, owners, rep["nonce"])
    if reason is not None:
        return None, i, reason
    r["check"] = None
    return _apply_web_result(data, i, r, rep["result"], now)


def web_work(data, now=None, owners=None):
    """True when web_issue has something to do (a check to issue or to
    expire, or a watch to end), so the monitor takes the lock only
    then."""
    now = time.time() if now is None else now
    for r in data["items"].values():
        if isinstance(r, dict) and r.get("kind") == "watch-web" and r.get("until", 0) > now:
            if _not_owners(r, owners):
                return True
            chk = r.get("check")
            if (chk and chk.get("expires", 0) <= now) or (
                not chk and r.get("next_check", 0) <= now
            ):
                return True
    return False

"""Read authenticated web-watch reports and publish private due-work files."""

import contextlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field

from muse_discord import muse_watches as W
from muse_discord.watches.fires import FirePolicy, WatchFireBuilder


@dataclass
class WebWatchWork:
    results_path_source: Callable[[], str]
    due_path_source: Callable[[], str]
    publish: Callable[..., bool]
    owners_source: Callable[[], set[str]]
    suspicious: Callable[[str], str | bool]
    cache: dict = field(default_factory=lambda: {"body": None, "path": None})

    @property
    def results_path(self) -> str:
        return self.results_path_source()

    def has_work(self, data, now):
        return os.path.exists(self.results_path) or W.web_work(data, now, self.owners_source())

    def take_results(
        self,
    ):
        """The checker's reports (bin/watch-report.py), emptied; the caller
        holds the store's lock, which the tool appends under."""
        path = self.results_path
        try:
            with open(path) as f:
                reps = json.load(f)
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            print(f"web_watch_results.json unreadable ({e}); dropped", flush=True)
            reps = []
        with contextlib.suppress(OSError):
            os.remove(path)
        return reps if isinstance(reps, list) else []

    def tick(self, state, data, now, verdicts, fires: WatchFireBuilder):
        """Web watches, under the store's lock: apply the checker's reports
        (only with a live check's nonce, for an active watch; anything else
        changes nothing), expire and issue checks, and make the fires:
        "watch-met" for a met result, a refused one, or a watch that gave
        up. A summary that looks like prompt injection (is_suspicious) is
        withheld from the worker."""
        out = []
        owners = self.owners_source()
        for rep in self.take_results():
            res, i, rec = W.web_apply(data, rep, now, owners)
            if res is None:
                print(f"Refused a web watch report for {i or '?'}: {rec}.", flush=True)
                continue
            print(f"Web watch {i}: {res}.", flush=True)
            if res in ("met", "gave-up", "refused"):
                assert isinstance(rec, dict)
                # The requester hears once: it happened, it could not be
                # checked, or the checker would not check it.
                m = {"result": res}
                summary = str(rep.get("summary") or "")
                withheld = bool(summary) and bool(self.suspicious(summary))
                if res in ("met", "refused") and not withheld:
                    m["summary"] = summary
                e = W.fire_entry(i, rec, now, m)
                if withheld:
                    print(
                        f"Web watch {i}: its summary looked like prompt injection; withheld.",
                        flush=True,
                    )
                    e["summary_withheld"] = True
                out.append((rec["requester"], rec["channel_id"], (rec, e)))
        for i, rec in W.web_issue(data, now, owners=owners):
            why = (
                f"no answer {W.WEB_MAX_FAILS} times"
                if rec.get("fails", 0) >= W.WEB_MAX_FAILS
                else "its requester is not an owner"
            )
            print(f"Web watch {i}: {why}; ended.", flush=True)
            out.append(
                (
                    rec["requester"],
                    rec["channel_id"],
                    (rec, W.fire_entry(i, rec, now, {"result": "gave-up"})),
                )
            )
        return fires.make(
            state,
            data,
            out,
            policy=FirePolicy(now=now, verdicts=verdicts, counts={}, kind="watch-met"),
        )

    def write_due(self, data, now=None):
        """web_watches_due.json: the live checks (muse_watches.web_due), for
        hooks/watch-due.sh; written (mode 600) only when it changes."""
        self.publish(self.due_path_source(), W.web_due(data, now), self.cache, create=False)

#!/usr/bin/env python3
"""watch-report.py -- the web watch checker's only write.

    python3 bin/watch-report.py <id> <nonce> met|not-met|unknown|refused --stdin <<'END_<token>'
    <summary>
    END_<token>

(or the summary as the last argument instead of --stdin). The checker
prompt gives the heredoc's end marker, random for every run, so text
from a web page cannot end the heredoc and run as a command.

Reports one web watch check (an entry of the checker's data block): its
id and nonce exactly as given, the result, and a short summary in plain
words (at most 300 characters). Appends it to web_watch_results.json in
the package dir under the watches' lock; the monitor drains that file
within seconds and acts on a report only when the nonce is that check's
live one (each nonce works once, and only for about 15 minutes) and the
watch is still active, so a wrong, reused or late report changes
nothing.

Refuses (and writes nothing) when the arguments are malformed, or when
the check is not live in web_watches_due.json (expired, already
reported, or never issued), saying so. Never needs the bot token and
never touches the network.
"""
import fcntl
import hmac
import json
import os
import sys
import tempfile
import time

PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PACKAGE_DIR)
import muse_watches as W  # noqa: E402

DUE = os.path.join(PACKAGE_DIR, "web_watches_due.json")
RESULTS = os.path.join(PACKAGE_DIR, "web_watch_results.json")
LOCK = os.path.join(PACKAGE_DIR, "watches.lock")


def live(rep):
    """The check is listed as due with this nonce (constant time)."""
    try:
        with open(DUE) as f:
            due = json.load(f)
    except (OSError, ValueError):
        return False
    return any(isinstance(e, dict) and e.get("id") == rep["id"]
               and hmac.compare_digest(str(e.get("nonce")).encode(),
                                       rep["nonce"].encode())
               for e in due if isinstance(due, list))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 4:
        print("usage: watch-report.py <id> <nonce> "
              "met|not-met|unknown|refused --stdin|'<summary>'",
              file=sys.stderr)
        return 2
    summary = sys.stdin.read(4096) if argv[3] == "--stdin" else argv[3]
    rep = {"id": argv[0], "nonce": argv[1], "result": argv[2],
           "summary": " ".join(summary.split()), "at": time.time()}
    problems = W.report_problems(rep)
    if problems:
        print("error: nothing reported; fix these and run it again:",
              file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1
    if not live(rep):
        print(f"error: check {rep['id']} is not live (it expired, was "
              f"reported already, or the id or nonce is wrong); nothing "
              f"reported. Move on to the next entry.", file=sys.stderr)
        return 1
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with open(RESULTS) as f:
                reps = json.load(f)
            if not isinstance(reps, list):
                reps = []
        except (OSError, ValueError):
            reps = []
        reps = (reps + [rep])[-200:]
        tfd, tmp = tempfile.mkstemp(dir=PACKAGE_DIR,
                                    prefix=".web_watch_results.json.",
                                    suffix=".tmp")
        try:
            with os.fdopen(tfd, "w") as f:
                os.fchmod(f.fileno(), 0o600)
                json.dump(reps, f)
            os.replace(tmp, RESULTS)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    finally:
        os.close(fd)
    print(f"reported {rep['id']}: {rep['result']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

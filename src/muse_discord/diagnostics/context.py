"""Collect one installation's diagnostic results and read its local runtime evidence.

The caller selects text or JSON reporting. Process and log inspection stay bound
to the supplied installation, and helpers never connect to Discord.
"""

import json
import os
import subprocess

AGE_MINUTE_THRESHOLD = 2 * 60


AGE_HOUR_THRESHOLD = 2 * 60 * 60


class Doctor:
    def __init__(self, pkg, as_json):
        self.pkg = pkg
        self.as_json = as_json
        self.results = []

    def add(self, status, what, fix=None):
        self.results.append({"status": status, "what": what, "fix": fix})
        if not self.as_json:
            tag = {"ok": "OK", "note": "NOTE", "problem": "PROBLEM"}[status]
            print(f"{tag}: {what}" + (f" Fix: {fix}" if fix else ""), flush=True)

    @property
    def failed(self):
        return any(r["status"] == "problem" for r in self.results)

    def path(self, name):
        return os.path.join(self.pkg, name)

    def log_tail(self, name, size=200_000):
        try:
            with open(self.path(name), "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - size))
                return f.read().decode("utf-8", "replace")
        except OSError:
            return ""

    # -- process -------------------------------------------------------
    def monitor_pid(self):
        """The live monitor.py running from this package dir, or None.
        Recorded PIDs first, then the watchdog's root/bin entrypoint whose
        working directory is this package (never another install's)."""
        cands = []
        for name in ("src/muse_discord/monitor.pid", "monitor.pid"):
            try:
                with open(self.path(name)) as f:
                    cands.append(f.read().split()[0])
            except OSError, IndexError:
                pass
        try:
            out = subprocess.run(
                ["pgrep", "-f", r"^python3 (bin/)?[m]onitor\.py( |$)"],
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout
            cands += out.split()
        except OSError, subprocess.SubprocessError:
            pass
        real = os.path.realpath(self.pkg)
        for pid in cands:
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    argv = f.read().split(b"\0")
                cwd = os.path.realpath(os.readlink(f"/proc/{pid}/cwd"))
            except OSError:
                continue
            if len(argv) > 1 and argv[1].endswith(b"monitor.py") and cwd == real:
                return int(pid)
        return None


def ago(secs):
    secs = max(0, int(secs))
    if secs < AGE_MINUTE_THRESHOLD:
        return f"{secs} seconds ago"
    if secs < AGE_HOUR_THRESHOLD:
        return f"{secs // 60} minutes ago"
    return f"{secs // 3600} hours ago"


def read_ledger(d):
    try:
        with open(d.path("mention_ledger.json")) as f:
            data = json.load(f)
        ents = data.get("entries") if isinstance(data, dict) else None
        return ents if isinstance(ents, dict) else {}
    except OSError, ValueError:
        return {}

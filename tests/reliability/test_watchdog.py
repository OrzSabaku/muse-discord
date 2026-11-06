"""watchdog.sh scenarios against fake monitor/presence processes. Each runs
in its own user+PID namespace (unshare), so the watchdog's pgrep/pkill
can only ever see the fakes, and every fake dies with the namespace;
they all run at once. The watchdog's output lines are what the owners
get as alerts, so they are checked as written."""
import concurrent.futures
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "_lib"))
from common import FAKE_PROC, FAKEWS, copy_watchdog, scratch  # noqa: E402


def scenario(steps, env=None, token_file=True, token_env=None,
             presence=False, old_log=None):
    d = scratch("muse-wd-")
    copy_watchdog(d)
    for n in ("monitor.py", "presence.py"):
        shutil.copy(FAKE_PROC, os.path.join(d, n))
    tok = os.path.join(d, "bot_token")
    if token_file:
        with open(tok, "w") as f:
            f.write("dummy\n")
    with open(os.path.join(d, "config.yaml"), "w") as f:
        f.write(f"bot:\n  token_file: {tok}\n"
                f"  token_env: {token_env or 'null'}\n"
                f"presence:\n  enabled: {'true' if presence else 'false'}\n"
                f"updates:\n  check: false\n")
    if old_log:
        with open(os.path.join(d, "monitor.log"), "w") as f:
            f.write(old_log)
    e = os.environ.copy()
    # A fake websockets package, so the watchdog never pip-installs.
    e["PYTHONPATH"] = FAKEWS
    e.update(env or {})
    p = subprocess.run(
        ["unshare", "--user", "--map-root-user", "--pid", "--fork",
         "--mount-proc", "bash", "-c", f"cd '{d}' && {steps}"],
        capture_output=True, text=True, timeout=400, env=e)
    return p.stdout + p.stderr, d


WD = "bash watchdog.sh"
# name -> scenario() arguments; they all run at once (setUpModule).
SCENARIOS = {
    "token_env_only": (WD, dict(env={"MY_BOT_TOKEN": "x"},
                                token_file=False, token_env="MY_BOT_TOKEN")),
    "missing_token": (WD, dict(token_file=False, token_env="MY_BOT_TOKEN")),
    "log_rotated": (WD, dict(old_log="Traceback: the crash we need\n")),
    "identity_pending": (WD, dict(env={"FAKE_MODE": "pending"})),
    "token_rejected": (f"time -p {WD}", dict(env={"FAKE_MODE": "token"})),
    "stale_heartbeat": (f"{WD}; touch -d '20 minutes ago' monitor.heartbeat;"
                        f" {WD}; echo; {WD}", dict(env={"FAKE_MODE": "hung"})),
    "gateway_disabled": (f"{WD}; {WD}; {WD}", dict(env={"FAKE_MODE":
                                                         "gwoff"})),
    "presence_token": (WD, dict(env={"FAKE_PMODE": "token"}, presence=True)),
    "healthy": (f"{WD}; {WD}", dict(presence=True)),
}
_results = {}


def setUpModule():
    with concurrent.futures.ThreadPoolExecutor(len(SCENARIOS)) as pool:
        futs = {name: pool.submit(scenario, steps, **kw)
                for name, (steps, kw) in SCENARIOS.items()}
        for name, fut in futs.items():
            _results[name] = fut.result()


class Watchdog(unittest.TestCase):
    def test_token_env_only_install_starts(self):
        out, _ = _results["token_env_only"]
        self.assertIn("OK monitor restarted", out)
        self.assertNotIn("ALERT", out)

    def test_missing_token_alerts(self):
        out, _ = _results["missing_token"]
        self.assertIn("ALERT: monitor DOWN", out)
        self.assertNotIn("OK monitor restarted", out)

    def test_log_rotated_not_truncated(self):
        out, d = _results["log_rotated"]
        with open(os.path.join(d, "monitor.log.1")) as f:
            self.assertIn("the crash we need", f.read())

    def test_identity_pending_counts_as_started(self):
        out, _ = _results["identity_pending"]
        self.assertIn("OK monitor restarted (identity pending", out)
        self.assertNotIn("ALERT", out)

    def test_token_rejected_alerts_fast(self):
        out, _ = _results["token_rejected"]
        self.assertIn("ALERT: monitor DOWN: Discord rejected the token", out)
        real = float(out.split("real ")[1].split()[0])
        self.assertLess(real, 30)

    def test_stale_heartbeat_restarts(self):
        out, _ = _results["stale_heartbeat"]
        self.assertIn("OK monitor restarted (it was stuck: heartbeat", out)
        self.assertEqual(out.count("OK monitor alive"), 1, out)

    def test_gateway_disabled_alerts_once(self):
        out, _ = _results["gateway_disabled"]
        self.assertEqual(out.count("ALERT: monitor degraded: GATEWAY "
                                   "DISABLED (code 4014)"), 1, out)
        self.assertIn("OK already alerted", out)

    def test_presence_token_rejected(self):
        out, _ = _results["presence_token"]
        self.assertIn("ALERT: presence DOWN: Discord rejected the token",
                      out)

    def test_healthy_run_is_quiet(self):
        out, _ = _results["healthy"]
        self.assertIn("OK monitor alive", out)
        self.assertIn("OK presence alive", out)
        self.assertNotIn("ALERT", out)


if __name__ == "__main__":
    unittest.main()

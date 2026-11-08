"""Release eligibility and bounded platform/build metadata."""

import platform
import subprocess
import sys

from muse_discord.usage_stats.limits import _SEMVER_RE, OS_FAMILIES


def is_release(version):
    """A vX.Y.Z tag: the only versions that send."""
    return bool(version and _SEMVER_RE.match(version))


def os_family():
    s = (platform.system() or "").lower()
    return s if s in OS_FAMILIES else "other"


def python_minor():
    return f"{sys.version_info[0]}.{sys.version_info[1]}"


def installed_version(pkg_dir, timeout=5):
    """The newest vX.Y.Z release tag HEAD contains (as watchdog.sh
    decides it), "unreleased" when there is none, "unknown" when git
    cannot tell."""
    try:
        out = subprocess.run(
            ["git", "-C", pkg_dir, "tag", "--merged", "HEAD", "--list", "v*"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except OSError, subprocess.SubprocessError:
        return "unknown"
    if out.returncode != 0:
        return "unknown"
    tags = [t for t in out.stdout.split() if _SEMVER_RE.match(t)]
    if not tags:
        return "unreleased"
    return max(tags, key=lambda t: tuple(int(x) for x in t.lstrip("v").split(".")))


def build_info(pkg_dir, version, timeout=5):
    """(ahead, modified) for a release version: commits HEAD is past that
    tag, and whether tracked code (*.py, *.sh) has uncommitted edits.
    persona.md and the config are the owner's to edit, so they don't
    count. (0, False) when git cannot tell or there is no release."""
    if not (version and _SEMVER_RE.match(version)):
        return 0, False
    git = ["git", "-C", pkg_dir]
    try:
        n = subprocess.run(
            [*git, "rev-list", "--count", f"{version}..HEAD"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        st = subprocess.run(
            [*git, "status", "--porcelain", "--untracked-files=no", "--", "*.py", "*.sh"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except OSError, subprocess.SubprocessError:
        return 0, False
    ahead = int(n.stdout.strip()) if n.returncode == 0 and n.stdout.strip().isdigit() else 0
    return min(ahead, 100_000), st.returncode == 0 and bool(st.stdout.strip())

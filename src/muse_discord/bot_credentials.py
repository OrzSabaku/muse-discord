"""Read bot credentials from ordered environment/file sources without logging them.

Empty or unavailable sources fall through to the next source. Relative paths
belong to the target project, independent of the caller's working directory.
Callers choose their own missing-token policy; importing this module reads none.
"""

import os
from collections.abc import Iterable

DEFAULT_TOKEN_FILE = "~/.config/muse-discord/bot_token"


def token_path(where: str, base_dir: str | os.PathLike[str]) -> str:
    return os.path.join(base_dir, os.path.expanduser(where))


def read_token(sources: Iterable[tuple[str, str]], base_dir: str | os.PathLike[str]) -> str | None:
    for kind, where in sources:
        if kind == "env":
            value = os.environ.get(where, "").strip()
        else:
            try:
                with open(token_path(where, base_dir)) as file:
                    value = file.read().strip()
            except OSError:
                continue
        if value:
            return value
    return None


def token_file(sources: Iterable[tuple[str, str]], base_dir: str | os.PathLike[str]) -> str | None:
    return next((token_path(where, base_dir) for kind, where in sources if kind == "file"), None)

"""One tool invocation's transport, token, identity and local file destinations.

Construction and imports read no token. The CLI loads it explicitly before
execution. REST access, generated-file checks and output privacy stay on this
instance, so tests and other callers need no process-wide mutable bindings.
"""

import os
from typing import Any

from muse_discord.bot_credentials import DEFAULT_TOKEN_FILE, read_token
from muse_discord.discord_tools.privacy import scrub


class ToolSession:
    def __init__(self, package_dir: str):
        self.package_dir = package_dir
        self.generated_dir = os.path.join(package_dir, "generated")
        self.owner_dm_nonces_file = os.path.join(package_dir, "owner_dm_nonces.json")
        self.owner_items_file = os.path.join(package_dir, "owner_items.json")
        self.config = os.environ.get("MUSE_DISCORD_CONFIG") or os.path.join(
            package_dir, "config.yaml"
        )
        self.token = ""
        self.api_base = "https://discord.com/api/v10"
        self.timeout = 30
        self._bot_id = None

    def scrub(self, text):
        return scrub(text, self.token, self.owner_dm_nonces_file, self.owner_items_file)

    def generated_file(self, path):
        """Resolve an upload path, refusing anything that is not a regular
        file inside the package's generated/ dir after resolving symlinks
        (the same rule as generated_media.validate_paths). Keeps the tool
        from ever posting the token file, SSH keys, or anything else."""
        root = os.path.realpath(self.generated_dir)
        rp = os.path.realpath(os.path.expanduser(path))
        if not rp.startswith(root + os.sep) or not os.path.isfile(rp):
            raise SystemExit(
                f"Refusing to upload {path!r}: "
                f"only files inside {self.generated_dir}/ can be attached."
            )
        return rp

    def settings(self):
        """The package's settings (muse_settings), or None when the config
        cannot be read."""
        try:
            from muse_discord import muse_settings

            return muse_settings.load(self.config)
        except Exception:
            return None

    def owners(self):
        """The owners' user ids from the config, or None when it cannot be
        read (then every DM counts as an owner's)."""
        st = self.settings()
        return None if st is None else {str(o) for o in st.owners}

    def load_token(self):
        """token_from, in order (an invalid config: the default file)."""
        st = self.settings()
        sources = st.token_sources() if st is not None else [("file", DEFAULT_TOKEN_FILE)]
        token = read_token(sources, self.package_dir)
        if not token:
            raise SystemExit("No bot token found in the configured environment variables or files.")
        return token

    def bot_id(self):
        if self._bot_id is None:
            self._bot_id = self.api("GET", "/users/@me")["id"]
        return self._bot_id

    def request(self, method: str, path: str, data: Any = None, files=()) -> Any:
        from muse_discord.discord_rest import DiscordError, transport

        try:
            result = transport(self.token, self.api_base).request(
                method, path, data, files=files, timeout=self.timeout
            )
            return result if result not in (None, "") else {"ok": True, "status": 204}
        except (DiscordError, OSError, TimeoutError) as exc:
            raise SystemExit(str(exc)) from None

    def api(self, method: str, path: str, data: Any = None) -> Any:
        return self.request(method, path, data)

    def api_multipart(self, path, payload, file_path):
        """POST with a single file attachment (Discord multipart upload).

        The file is referenced as attachments[0] inside payload["message"];
        used by forum-thread creation so the starter post carries the image
        itself instead of an external link.
        """
        filename = os.path.basename(file_path)
        with open(file_path, "rb") as f:
            blob = f.read()
        self.scrub(filename)
        self.scrub(
            str(payload.get("name", ""))
            + str(payload.get("message", {}).get("content", ""))
            + blob.decode("latin-1")
        )
        return self.request("POST", path, payload, files=[(filename, blob)])

"""Bounded CDN downloads. The REST client's credentials never reach this code."""

import hashlib
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

MAX_BYTES = 25 * 1024 * 1024
INLINE_BYTES = 5 * 1024 * 1024
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
TWEMOJI = "https://cdn.jsdelivr.net/gh/jdecked/twemoji@15.1.0/assets/72x72"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("CDN redirects are not allowed.")


def validate_url(url):
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
        or parts.fragment
    ):
        raise ValueError("Invalid CDN URL.")
    if parts.hostname in ("cdn.discordapp.com", "media.discordapp.net"):
        if not parts.path.startswith(
            ("/attachments/", "/ephemeral-attachments/", "/avatars/", "/embed/avatars/", "/emojis/")
        ):
            raise ValueError("Unsupported Discord CDN path.")
    elif parts.hostname == "cdn.jsdelivr.net":
        if not url.startswith(TWEMOJI + "/"):
            raise ValueError("Unsupported emoji CDN path.")
    else:
        raise ValueError("Unsupported CDN host.")


def download(url, filename, package_dir, opener=None):
    validate_url(url)
    package = Path(package_dir).resolve()
    directory = package / "media" / "on-demand"
    directory.resolve().relative_to(package)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename).lstrip(".")[:100] or "file"
    digest = hashlib.sha256(url.encode()).hexdigest()[:24]
    dest = directory / (digest + "-" + safe)
    request = Request(url, headers={"User-Agent": "muse-discord-media/1.0"})
    opener = opener or build_opener(NoRedirect())
    temp = None
    try:
        with opener.open(request, timeout=30) as response:
            if int(response.headers.get("Content-Length", 0)) > MAX_BYTES:
                raise ValueError("Attachment exceeds the download limit.")
            mime = (
                response.headers.get("Content-Type", "application/octet-stream")
                .split(";")[0]
                .strip()
            )
            fd, temp = tempfile.mkstemp(prefix=".download-", dir=directory)
            total = 0
            with os.fdopen(fd, "wb") as target:
                while True:
                    chunk = response.read(min(65536, MAX_BYTES - total + 1))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise ValueError("Attachment exceeds the download limit.")
                    target.write(chunk)
            os.replace(temp, dest)
        return {"path": str(dest), "bytes": total, "content_type": mime, "untrusted": True}
    finally:
        if temp and Path(temp).exists():
            Path(temp).unlink()


class MediaTools:
    def __init__(self, api, guard, package_dir, downloader=download):
        self.api, self.guard = api, guard
        self.package_dir, self.download = package_dir, downloader

    def attachment(self, channel, message, attachment):
        self.guard(channel)
        data = self.api("GET", f"/channels/{channel}/messages/{message}")
        for item in data.get("attachments") or []:
            if item.get("id") == attachment:
                if int(item.get("size", 0)) > MAX_BYTES:
                    raise ValueError("Attachment exceeds the download limit.")
                return self.download(
                    item["url"], item.get("filename") or "attachment", self.package_dir
                )
        raise ValueError("Attachment not found on this message.")

    def avatar(self, channel, user):
        self.guard(channel)
        data = self.api("GET", f"/users/{user}")
        avatar = data.get("avatar")
        if avatar:
            if not re.fullmatch(r"(?:a_)?[0-9a-f]{32}", avatar):
                raise ValueError("Invalid avatar hash.")
            ext = "gif" if avatar.startswith("a_") else "png"
            url = f"https://cdn.discordapp.com/avatars/{user}/{avatar}.{ext}?size=256"
        else:
            # Legacy discriminator accounts and newer migrated accounts use
            # different default-avatar indices.
            discriminator = int(data.get("discriminator") or 0)
            index = discriminator % 5 if discriminator else (int(user) >> 22) % 6
            ext = "png"
            url = f"https://cdn.discordapp.com/embed/avatars/{index}.png"
        return self.download(url, f"avatar-{user}.{ext}", self.package_dir)

    def emoji(self, channel, value):
        self.guard(channel)
        match = re.fullmatch(r"<(a?):[^:>\s]+:([1-9][0-9]{16,19})>", value)
        if match:
            emoji_id, animated = match[2], bool(match[1])
        else:
            custom = re.fullmatch(r"(?:[^:\s]+:)?([1-9][0-9]{16,19})", value)
            emoji_id, animated = (custom[1], False) if custom else (None, False)
        if emoji_id:
            ext = "gif" if animated else "png"
            url = f"https://cdn.discordapp.com/emojis/{emoji_id}.{ext}"
            filename = f"emoji-{emoji_id}.{ext}"
        else:
            if (
                not value
                or len(value) > 16
                or value.isascii()
                or any(ord(c) < 0x2000 for c in value)
            ):
                raise ValueError("Invalid emoji.")
            codes = [ord(c) for c in value]
            key = "-".join(f"{code:x}" for code in codes if 0x200D in codes or code != 0xFE0F)
            url, filename = f"{TWEMOJI}/{key}.png", f"emoji-{key}.png"
        return self.download(url, filename, self.package_dir)

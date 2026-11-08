"""Download complete media from allowed public HTTPS hosts without bot credentials.

Validate every redirect and pin curl to the checked DNS addresses. Prefer curl
on networks where urllib responses can truncate; the fallback verifies lengths
and resumes short bodies. Callers select a media kind with fixed byte limits.
"""

import contextlib
import ipaddress
import socket
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from typing import override

MAX_MEDIA_BYTES = 8 * 1024 * 1024

MAX_VIDEO_BYTES = 32 * 1024 * 1024

MAX_DOC_BYTES = 4 * 1024 * 1024

EMBED_MEDIA_HOST_SUFFIXES = (
    "discordapp.com",
    "discordapp.net",
    "tenor.com",
    "giphy" + ".com",
    "klipy.com",
)

FETCH_MAX_REDIRECTS = 3

DOWNLOADS = {
    "image": (MAX_MEDIA_BYTES, 20, "image/"),
    "video": (MAX_VIDEO_BYTES, 60, "video/"),
    "document": (MAX_DOC_BYTES, 20, ""),
}


def embed_host_allowed(url):
    """True when url's host is an allowlisted media host or a subdomain
    of one. A bare suffix match would also accept lookalikes such as
    evil-tenor.com or attackerdiscordapp.net."""
    try:
        host = (urllib.parse.urlparse(url).hostname or "").rstrip(".")
    except ValueError:
        return False
    return any(host == s or host.endswith("." + s) for s in EMBED_MEDIA_HOST_SUFFIXES)


def _fetch_addrs(url):
    """The addresses a media fetch of url may connect to, or None when it
    must not be fetched at all: not https, not an allowlisted media host,
    unresolvable, or resolving to ANY non-public address (private,
    loopback, link-local such as 169.254.169.254, reserved, multicast)."""
    try:
        p = urllib.parse.urlparse(url)
        port = p.port or 443
    except ValueError:
        return None
    if p.scheme != "https" or not embed_host_allowed(url):
        return None
    try:
        infos = socket.getaddrinfo(p.hostname, port, proto=socket.IPPROTO_TCP)
    except OSError, UnicodeError:
        return None
    addrs = []
    for info in infos:
        try:
            ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        except ValueError:
            return None
        if not ip.is_global or ip.is_multicast:
            return None
        if str(ip) not in addrs:
            addrs.append(str(ip))
    return addrs or None


class _FetchRedirectHandler(urllib.request.HTTPRedirectHandler):
    """urllib's side of _fetch_complete's redirect policy: at most
    FETCH_MAX_REDIRECTS hops, each to a URL that passes _fetch_addrs."""

    max_redirections = FETCH_MAX_REDIRECTS

    @override
    def redirect_request(self, req, fp, code, msg, hdrs, newurl):
        if not _fetch_addrs(newurl):
            raise urllib.error.HTTPError(newurl, code, "redirect to a disallowed URL", hdrs, fp)
        return super().redirect_request(req, fp, code, msg, hdrs, newurl)


def _fetch_with_curl(url, max_bytes, timeout, kind, ua):
    # Path 1: curl (proven reliable on this network). No -L: redirects
    # are followed here one hop at a time, so each hop gets the same
    # checks as the first URL, and --resolve pins curl to the addresses
    # just checked (no DNS swap between check and connect). -f makes an
    # HTTP error a failure instead of content: an error page must not be
    # extracted as the "document".
    hop = url
    try:
        for _ in range(FETCH_MAX_REDIRECTS + 1):
            addrs = _fetch_addrs(hop)
            if not addrs:
                print(
                    f"Refusing {kind} download from {hop[:120]}: not https, not an "
                    "allowlisted media host, or not a public address",
                    flush=True,
                )
                return None, None
            p = urllib.parse.urlparse(hop)
            addresses = ",".join(f"[{a}]" if ":" in a else a for a in addrs)
            pin = f"{p.hostname}:{p.port or 443}:{addresses}"
            r = subprocess.run(
                [
                    "curl",
                    "-sS",
                    "-f",
                    "--proto",
                    "=https",
                    "--resolve",
                    pin,
                    "--max-time",
                    str(timeout),
                    "--max-filesize",
                    str(max_bytes),
                    "-A",
                    ua,
                    "-w",
                    "\n%{http_code}\n%{content_type}\n%{redirect_url}",
                    hop,
                ],
                capture_output=True,
                timeout=timeout + 10,
            )
            if r.returncode != 0 or not r.stdout:
                print(
                    "curl {} download failed: {}".format(
                        kind, r.stderr.decode(errors="replace")[:150]
                    ),
                    flush=True,
                )
                break
            body, code, ctype, nxt = r.stdout.rsplit(b"\n", 3)
            code = code.decode(errors="replace").strip()
            nxt = nxt.decode(errors="replace").strip()
            if code.startswith("3") and nxt:
                hop = nxt
                continue
            ctype = ctype.decode(errors="replace").split(";")[0].strip().lower()
            if code.startswith("2") and body and len(body) <= max_bytes:
                return bytes(body), ctype
            print(f"curl {kind} download failed: HTTP {code}", flush=True)
            break
        else:
            print(
                f"curl {kind!s} download failed: more than {int(FETCH_MAX_REDIRECTS)} redirects",
                flush=True,
            )
            return None, None
    except FileNotFoundError:
        pass  # no curl installed; fall through to urllib
    except Exception as e:
        print(f"curl {kind} download failed: {e}", flush=True)
    return None


def _expected_download_size(resp, expected):
    if resp.status == HTTPStatus.PARTIAL_CONTENT:
        with contextlib.suppress(ValueError, IndexError):
            expected = int(resp.headers.get("Content-Range", "").rsplit("/", 1)[1])
    elif expected is None:
        with contextlib.suppress(ValueError):
            expected = int(resp.headers.get("Content-Length") or 0) or None
    return expected


def _fetch_with_urllib(url, max_bytes, timeout, kind, ua):
    # Path 2: urllib with length verification + Range resume. HTTP errors
    # raise; redirects go through _FetchRedirectHandler's checks.
    if not _fetch_addrs(url):
        return None, None
    opener = urllib.request.build_opener(_FetchRedirectHandler)
    data = bytearray()
    expected = None
    ctype = ""
    for _ in range(4):
        try:
            h = {"User-Agent": ua}
            if data:
                h["Range"] = f"bytes={len(data)}-"
            req = urllib.request.Request(url, headers=h)
            with opener.open(req, timeout=timeout) as resp:
                if resp.status == HTTPStatus.OK and data:
                    data = bytearray()  # server ignored Range; start over
                    expected = None
                ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
                expected = _expected_download_size(resp, expected)
                if expected and expected > max_bytes:
                    return None, None
                data += resp.read(max_bytes + 1 - len(data))
                if len(data) > max_bytes:
                    return None, None
                if not expected or len(data) >= expected:
                    break  # unknown length: take what we got
        except Exception as e:
            print(f"urllib {kind} download attempt failed: {e}", flush=True)
    if not data or (expected and len(data) < expected):
        return None, None  # empty or truncated even after resume attempts
    return bytes(data), ctype


def _fetch_complete(url, max_bytes, timeout, kind):
    """Fetch a URL, returning (body_bytes, content_type), or (None, None).

    This network's egress proxy intermittently truncates HTTP/1.1 response
    bodies fetched with Python's urllib: the connection just ends early with
    no error, so a naive read loop mistakes a partial file for a complete
    one. Mitigations: prefer curl when available (reliable here), and
    otherwise verify the byte count against Content-Length, resuming with
    Range requests when short.
    Never sends the bot token: attachment URLs are pre-signed, and the CDN
    drops connections carrying an Authorization header.
    Every URL, and every redirect hop, must pass _fetch_addrs (https, an
    allowlisted media host, public addresses only): message content
    picks these URLs, and a fetch must not become a probe of the LAN or
    the cloud metadata service.
    """
    ua = "DiscordBot (muse-discord, 1.0)"
    fetched = _fetch_with_curl(url, max_bytes, timeout, kind, ua)
    return fetched if fetched is not None else _fetch_with_urllib(url, max_bytes, timeout, kind, ua)


def download(kind, url, dest_path):
    """Download a complete file of this kind to dest_path; its content
    type (a document's may be anything), or None (nothing written)."""
    cap, timeout, want = DOWNLOADS[kind]
    try:
        data, ctype = _fetch_complete(url, cap, timeout, kind)
    except Exception as e:
        print(f"{kind.capitalize()} download failed: {e}", flush=True)
        return None
    if not data or ctype is None or not ctype.startswith(want):
        return None
    with open(dest_path, "wb") as f:
        f.write(data)
    return ctype or "application/octet-stream"

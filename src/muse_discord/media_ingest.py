"""Receive message attachments without depending on monitor configuration or state.

Fetch policy, byte limits and local extraction stay together. Callers provide
only the message and destination directory; this module never reads bot tokens.
"""

import contextlib
import importlib
import ipaddress
import os
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from typing import override

# Media handling: the monitor downloads images/GIFs from mentions so the
# reply worker can actually see them (the worker has no network access).
MAX_MEDIA_BYTES = 8 * 1024 * 1024
MAX_MEDIA_PER_MESSAGE = 6
MEDIA_TTL_SECS = 3600  # downloaded media older than this is swept each poll
# Embeds are only downloaded from known media hosts (attachments always
# come from the chat CDN). Keeps a malicious link unfurl from becoming a
# download from an arbitrary server. A host matches a suffix exactly or
# as a subdomain ("media.tenor.com"), never as a lookalike
# ("evil-tenor.com"); see embed_host_allowed and _fetch_addrs, which
# _fetch_complete applies to every URL and every redirect hop.
EMBED_MEDIA_HOST_SUFFIXES = (
    "discordapp.com",
    "discordapp.net",
    "tenor.com",
    "giphy" + ".com",
    "klipy.com",
)
FETCH_MAX_REDIRECTS = 3
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
CTYPE_EXT = {"image/gif": ".gif", "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}

# Video: download the clip, extract a few frames for the worker to view.
# The worker sees still frames only: no motion between them, no audio.
MAX_VIDEO_BYTES = 32 * 1024 * 1024
VIDEO_FRAME_COUNT = 5
VIDEO_EXTS = (".mp4", ".mov", ".webm", ".avi", ".mkv")
FFMPEG_TIMEOUT = 60
# ffprobe/ffmpeg pick a demuxer from the downloaded bytes, not the
# extension, so a crafted HLS or concat playlist saved as a "video" would
# make them read other local files or fetch URLs. Only the expected
# container demuxers may run, and only on a plain local file.
FFMPEG_INPUT_GUARD = [
    "-format_whitelist",
    "mov,mp4,matroska,webm,avi",
    "-protocol_whitelist",
    "file",
]

# Documents: download text-ish attachments and extract their text so the
# worker can read them. The extracted text is inlined on the queue item
# (truncated); the worker never opens these files. Anything executable or
# archive-like stays unviewable.
MAX_DOC_BYTES = 4 * 1024 * 1024
MAX_DOCS_PER_MESSAGE = 3
DOC_TEXT_CHARS = 6000  # max extracted chars per document
TEXT_EXTS = (
    ".txt",
    ".md",
    ".markdown",
    ".csv",
    ".json",
    ".jsonl",
    ".log",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".rs",
    ".go",
    ".java",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".cs",
    ".rb",
    ".sh",
    ".yaml",
    ".yml",
    ".toml",
    ".xml",
    ".html",
    ".css",
    ".sql",
)
PDF_EXTS = (".pdf",)
DOCX_EXTS = (".docx",)
PDFTOTEXT_TIMEOUT = 30


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


# kind -> (size cap, timeout, the content type it must have: "" = any).
DOWNLOADS = {
    "image": (MAX_MEDIA_BYTES, 20, "image/"),
    "video": (MAX_VIDEO_BYTES, 60, "video/"),
    "document": (MAX_DOC_BYTES, 20, ""),
}


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


def _read_pdf_text(path, filename):
    try:
        proc = subprocess.run(
            ["pdftotext", "-layout", path, "-"],
            capture_output=True,
            text=True,
            timeout=PDFTOTEXT_TIMEOUT,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout
        print(f"pdftotext found no text in {filename}", flush=True)
        return None
    except Exception as e:
        print(f"pdftotext failed ({filename}): {e}", flush=True)
        return None


def _read_docx_text(path, filename):
    try:
        docx = importlib.import_module("docx")

        doc = docx.Document(path)
        text = "\n".join(p.text for p in doc.paragraphs)
        return text
    except Exception as e:
        print(f"docx read failed ({filename}): {e}", flush=True)
        return None


def extract_document_text(path, content_type, filename):
    """Extract readable text from a downloaded document. Returns the text
    (truncated) or None when nothing usable could be extracted. Runs only
    local subprocess/library extraction: never executes the document."""
    ctype = (content_type or "").lower()
    ext = os.path.splitext(filename.lower())[1]

    def _truncate(t):
        t = (t or "").strip()
        if len(t) > DOC_TEXT_CHARS:
            t = t[:DOC_TEXT_CHARS] + "\n[...truncated...]"
        return t or None

    # Plain text and friends: read directly.
    if (
        ctype.startswith("text/")
        or ext in TEXT_EXTS
        or ctype.endswith("+json")
        or ctype in ("application/json", "application/x-sh")
    ):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return _truncate(f.read())
        except Exception as e:
            print(f"Text read failed ({filename}): {e}", flush=True)
            return None

    if ctype == "application/pdf" or ext in PDF_EXTS:
        return _truncate(_read_pdf_text(path, filename))

    # DOCX: python-docx paragraph text.
    if (
        ctype == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        or ext in DOCX_EXTS
    ):
        return _truncate(_read_docx_text(path, filename))

    return None


def extract_frames(video_path, out_prefix, count=VIDEO_FRAME_COUNT):
    """Extract `count` evenly spaced frames across the clip as PNGs.
    Returns the list of frame paths (may be empty on failure)."""
    try:
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                *FFMPEG_INPUT_GUARD,
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                video_path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        duration = float((probe.stdout or "").strip() or 0)
    except Exception:
        duration = 0
    fracs = [(i + 0.5) / count for i in range(count)]
    timestamps = [0.5] if duration <= 0 else [duration * f for f in fracs]
    frames = []
    for i, t in enumerate(timestamps):
        out = f"{out_prefix!s}_frame{int(i)}.png"
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-ss",
                    str(t),
                    *FFMPEG_INPUT_GUARD,
                    "-i",
                    video_path,
                    "-frames:v",
                    "1",
                    "-y",
                    out,
                ],
                capture_output=True,
                timeout=FFMPEG_TIMEOUT,
            )
            if os.path.exists(out) and os.path.getsize(out) > 0:
                frames.append(out)
        except Exception as e:
            print(f"Frame extraction failed @{t}s: {e}", flush=True)
    return frames


def tenor_gif_variant(url):
    """media.tenor.com serves GIF-picker GIFs as .mp4; the .gif variant is
    the same animation as an image the worker can view."""
    base, _, qs = url.partition("?")
    if "media.tenor.com" in base and base.lower().endswith(".mp4"):
        return base[:-4] + ".gif" + ("?" + qs if qs else "")
    return None


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


class MediaCollector:
    """Collect one message's bounded media, preserving video/document/image order."""

    def __init__(self, message, directory):
        self.message = message
        self.directory = directory
        self.media = []
        self.documents = []
        self.unviewable = []
        self.image_candidates = []
        self.video_candidates = []
        self.document_candidates = []

    def collect(self):
        os.makedirs(self.directory, exist_ok=True)
        self.attachments()
        self.embeds()
        self.videos()
        self.document_text()
        self.images()
        return self.media, self.documents, self.unviewable

    def attachments(self):
        for att in self.message.get("attachments", []):
            url = att.get("url")
            if not url:
                continue
            ctype = (att.get("content_type") or "").lower()
            name = att.get("filename") or "attachment"
            base = url.lower().split("?")[0]
            if ctype.startswith("image/") or base.endswith(IMAGE_EXTS):
                self.image_candidates.append((url, "attachment:" + name))
            elif ctype.startswith("video/") or base.endswith(VIDEO_EXTS):
                self.video_candidates.append((url, name))
            elif (
                ctype.startswith("text/")
                or base.endswith(TEXT_EXTS)
                or ctype == "application/pdf"
                or base.endswith(PDF_EXTS)
                or "wordprocessingml" in ctype
                or base.endswith(DOCX_EXTS)
                or ctype in ("application/json", "application/x-sh")
                or ctype.endswith("+json")
            ):
                self.document_candidates.append((url, name, ctype))
            else:
                self.unviewable.append(name + " (" + (ctype or "unknown type") + ")")

    def embeds(self):
        for emb in self.message.get("embeds", []):
            etype = str(emb.get("type", "?"))
            for key in ("image", "video", "thumbnail"):
                u = (emb.get(key) or {}).get("url")
                if not (u and embed_host_allowed(u)):
                    continue
                # GIF-picker embeds (type "gifv") carry the animation as an mp4
                # under the "video" key. Route those through the video path
                # (frame extraction): the .gif variant is usually far over the
                # 8MB image cap, while the mp4 is small.
                if key == "video" and (etype == "gifv" or u.lower().split("?")[0].endswith(".mp4")):
                    self.video_candidates.append((u, "embed:" + etype))
                else:
                    self.image_candidates.append((u, "embed:" + etype))

    def videos(self):
        for url, name in self.video_candidates:
            if len(self.media) >= MAX_MEDIA_PER_MESSAGE:
                break
            vdest = os.path.join(self.directory, f"{self.message['id']!s}_{len(self.media)}.vid")
            try:
                ctype = download("video", url, vdest)
            except Exception as e:
                print(f"Video download failed ({name}): {e}", flush=True)
                continue
            if not ctype:
                self.unviewable.append(name + " (video: download failed or too large)")
                continue
            frames = extract_frames(
                vdest, os.path.join(self.directory, f"{self.message['id']!s}_{len(self.media)}")
            )
            with contextlib.suppress(OSError):
                os.remove(vdest)  # keep only the frames
            if frames:
                for fp in frames:
                    if len(self.media) >= MAX_MEDIA_PER_MESSAGE:
                        break
                    self.media.append(
                        {"path": fp, "content_type": "image/png", "source": "video-frame:" + name}
                    )
            else:
                self.unviewable.append(name + " (video: could not extract frames)")

    def document_text(self):
        for url, name, _ctype in self.document_candidates:
            if len(self.documents) >= MAX_DOCS_PER_MESSAGE:
                break
            dest = os.path.join(self.directory, f"{self.message['id']!s}_{len(self.documents)}.doc")
            try:
                got_ctype = download("document", url, dest)
            except Exception as e:
                print(f"Document download failed ({name}): {e}", flush=True)
                continue
            if not got_ctype:
                self.unviewable.append(name + " (document: download failed or too large)")
                with contextlib.suppress(OSError):
                    os.remove(dest)
                continue
            # The extracted text is inlined on the queue item; the downloaded
            # file stays until the hourly sweep. The worker never opens it.
            text = extract_document_text(dest, got_ctype, name)
            if text:
                self.documents.append({"name": name, "content_type": got_ctype, "text": text})
            else:
                self.unviewable.append(name + " (document: could not extract text)")

    def images(self):
        seen = set()
        for url, label in self.image_candidates:
            if len(self.media) >= MAX_MEDIA_PER_MESSAGE:
                break
            urls_to_try = [url]
            gif_variant = tenor_gif_variant(url)
            if gif_variant:
                urls_to_try.insert(0, gif_variant)
            for u in urls_to_try:
                if u in seen:
                    continue
                seen.add(u)
                dest = os.path.join(self.directory, f"{self.message['id']!s}_{len(self.media)}.img")
                try:
                    ctype = download("image", u, dest)
                except Exception as e:
                    print(f"Media download failed ({label}): {e}", flush=True)
                    continue
                if ctype:
                    final = dest + CTYPE_EXT.get(ctype, ".img")
                    os.rename(dest, final)
                    self.media.append({"path": final, "content_type": ctype, "source": label})
                    break


def collect_media(msg, directory):
    """Return viewable frames/images, extracted document text and skipped files."""
    return MediaCollector(msg, directory).collect()


def sweep_media(directory):
    """Delete downloaded media older than MEDIA_TTL_SECS."""
    if not os.path.isdir(directory):
        return
    now = time.time()
    for fname in os.listdir(directory):
        fpath = os.path.join(directory, fname)
        try:
            if now - os.path.getmtime(fpath) > MEDIA_TTL_SECS:
                os.remove(fpath)
        except OSError:
            pass

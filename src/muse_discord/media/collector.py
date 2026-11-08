"""Select one message's attachments and collect bounded worker-visible inputs.

Videos produce frames, documents produce inline text and images remain files.
Collection preserves that ordering and limits; expired downloads are swept here.
"""

import contextlib
import os
import time

from muse_discord.media.documents import DOCX_EXTS, PDF_EXTS, TEXT_EXTS, extract_document_text
from muse_discord.media.fetch import download, embed_host_allowed
from muse_discord.media.video import VIDEO_EXTS, extract_frames

MAX_MEDIA_PER_MESSAGE = 6

MAX_DOCS_PER_MESSAGE = 3

MEDIA_TTL_SECS = 3600  # downloaded media older than this is swept each poll

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

CTYPE_EXT = {"image/gif": ".gif", "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


def tenor_gif_variant(url):
    """media.tenor.com serves GIF-picker GIFs as .mp4; the .gif variant is
    the same animation as an image the worker can view."""
    base, _, qs = url.partition("?")
    if "media.tenor.com" in base and base.lower().endswith(".mp4"):
        return base[:-4] + ".gif" + ("?" + qs if qs else "")
    return None


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

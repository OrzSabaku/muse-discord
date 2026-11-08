"""Generated-file containment, upload preparation and expiry.

Only files inside the requested message's directory can be selected. Generation
approval and channel authorization belong to the caller, before using these files.
This module has no dependency on monitor state or Discord credentials.
"""

import contextlib
import os
import re
import subprocess
import time

TTL_SECS = 3600
DISCORD_MAX_BYTES = 8 * 1024 * 1024  # unboosted server upload cap


def fit_for_discord(path):
    """Return (send_path, filename) with the file under Discord's upload cap,
    compressing with ffmpeg if needed. Returns (None, None) when it can't
    be made to fit."""
    if os.path.getsize(path) <= DISCORD_MAX_BYTES:
        return path, os.path.basename(path)
    ext = os.path.splitext(path)[1].lower()
    if ext in (".png", ".jpg", ".jpeg", ".webp"):
        out = path + ".fit.jpg"
        cmd = [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            path,
            "-vf",
            "scale='min(1600,iw)':-2",
            "-q:v",
            "5",
            "-y",
            out,
        ]
    elif ext in (".mp4", ".mov", ".webm"):
        out = path + ".fit.mp4"
        cmd = [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            path,
            "-vf",
            "scale='min(1280,iw)':-2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "28",
            "-preset",
            "veryfast",
            "-c:a",
            "aac",
            "-y",
            out,
        ]
    else:
        return None, None
    try:
        subprocess.run(cmd, capture_output=True, timeout=120)
    except Exception as e:
        print(f"Compression failed for {path}: {e}", flush=True)
        return None, None
    if os.path.exists(out) and os.path.getsize(out) <= DISCORD_MAX_BYTES:
        return out, os.path.basename(out)
    with contextlib.suppress(OSError):
        os.remove(out)
    return None, None


def sweep_generated(directory):
    """Delete generated files older than TTL_SECS (already sent),
    including those in the per-item generated/<message_id>/ dirs, then
    item dirs left empty for that long."""
    if os.path.isdir(directory):
        now = time.time()
        for fname in os.listdir(directory):
            fpath = os.path.join(directory, fname)
            try:
                if os.path.isdir(fpath) and not os.path.islink(fpath):
                    removed = False
                    for sub in os.listdir(fpath):
                        spath = os.path.join(fpath, sub)
                        try:
                            if now - os.lstat(spath).st_mtime > TTL_SECS:
                                os.remove(spath)
                                removed = True
                        except OSError:
                            pass
                    # An empty dir only goes once it held expired files
                    # or is old itself: the worker may have just created
                    # it and not written its file yet.
                    if not os.listdir(fpath) and (
                        removed or now - os.path.getmtime(fpath) > TTL_SECS
                    ):
                        os.rmdir(fpath)
                elif now - os.path.getmtime(fpath) > TTL_SECS:
                    os.remove(fpath)
            except OSError:
                pass


def _gen_item_dir(directory, item_id):
    """generated/<item_id>/, the only place a queue item's generated files
    may come from, or None for anything but a plain message id (no path
    tricks). Only the generated/ root is realpath'd: an item dir that is
    a symlink to another item's dir must fail the containment check."""
    if not isinstance(item_id, str) or not re.fullmatch(r"[0-9]+", item_id):
        return None
    return os.path.join(os.path.realpath(directory), item_id)


def validate_paths(directory, paths, item_id):
    """Keep only paths that exist and resolve inside generated/<item_id>/."""
    item_root = _gen_item_dir(directory, item_id)
    candidates = []
    for p in paths or []:
        if not isinstance(p, str):
            continue
        # Workers sometimes hand over paths relative to $HOME.
        ap = p if os.path.isabs(p) else os.path.join(os.path.expanduser("~"), p)
        rp = os.path.realpath(ap)
        if item_root is None or not rp.startswith(item_root + os.sep):
            print(f"Dropping attachment outside generated/{item_id}/: {p}", flush=True)
            continue
        if not os.path.isfile(rp):
            print(f"Dropping missing attachment: {p}", flush=True)
            continue
        candidates.append(rp)
    return candidates


def discover_generated(directory, sent_paths, item_id):
    """Find the newest unsent generated file in generated/<item_id>/. The
    worker often isn't told the local filename (the media tool returns a
    remote URL/handle), so the monitor matches the file itself, but only
    in the item's own directory: a file made for another item (a DM, say)
    is never a candidate. Returns a local path or None."""
    item_root = _gen_item_dir(directory, item_id)
    if item_root is None or not os.path.isdir(item_root):
        return None
    sent = set(sent_paths)
    best = None
    for fname in os.listdir(item_root):
        if fname.endswith(".json") or ".fit." in fname:
            continue  # sidecars and compressed copies, not originals
        fpath = os.path.realpath(os.path.join(item_root, fname))
        if not fpath.startswith(item_root + os.sep):
            continue  # a symlink out of the item dir
        if fpath in sent or not os.path.isfile(fpath):
            continue
        try:
            mt = os.path.getmtime(fpath)
        except OSError:
            continue
        if best is None or mt > best[1]:
            best = (fpath, mt)
    return best[0] if best else None

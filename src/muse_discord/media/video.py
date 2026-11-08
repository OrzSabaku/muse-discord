"""Extract still frames from local video files with bounded subprocesses.

Container and protocol allowlists stop crafted playlists from making ffmpeg read
other files or fetch URLs. Workers receive still frames without motion or audio.
"""

import os
import subprocess

VIDEO_FRAME_COUNT = 5

VIDEO_EXTS = (".mp4", ".mov", ".webm", ".avi", ".mkv")

FFMPEG_TIMEOUT = 60

FFMPEG_INPUT_GUARD = [
    "-format_whitelist",
    "mov,mp4,matroska,webm,avi",
    "-protocol_whitelist",
    "file",
]


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

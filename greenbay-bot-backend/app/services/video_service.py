"""
Video handling service: keyframe extraction for vision analysis.

Uses ffmpeg (installed in the container via Dockerfile) invoked through
`ffmpeg-python`. When ffmpeg is unavailable we degrade gracefully: the video
is still stored, and we return an empty frame list with a note.

Public API
----------
- `extract_keyframes(video_path, count=5)` -> list[bytes]
- `video_duration_seconds(video_path)` -> Optional[float]
- `frame_to_base64(jpeg_bytes)` -> str
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
from typing import Optional

from loguru import logger


# ---------------------------------------------------------------------------
# Capability probe
# ---------------------------------------------------------------------------
_ffmpeg_available: Optional[bool] = None


def _ffmpeg_ok() -> bool:
    """Return True once per process if the ffmpeg binary is callable."""
    global _ffmpeg_available
    if _ffmpeg_available is None:
        _ffmpeg_available = bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))
        if not _ffmpeg_available:
            logger.warning(
                "video_service: ffmpeg/ffprobe not found on PATH - "
                "keyframe extraction disabled"
            )
    return _ffmpeg_available


def video_duration_seconds(video_path: str) -> Optional[float]:
    """Return duration in seconds via ffprobe, or None on any failure."""
    if not _ffmpeg_ok():
        return None
    try:
        out = subprocess.check_output(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path,
            ],
            stderr=subprocess.STDOUT,
            timeout=30,
        )
        return float(out.decode().strip())
    except Exception as e:
        logger.warning(f"video_service: ffprobe failed for {video_path}: {e}")
        return None


def extract_keyframes(
    video_path: str,
    count: int = 5,
    max_dimension: int = 1280,
) -> list[bytes]:
    """Extract *count* evenly-spaced JPEG frames from the video.

    Frames are sampled at (t = duration * (i+1)/(count+1)) for i in [0, count).
    Each frame is scaled so the longest side is *max_dimension* px (preserves
    aspect ratio), then encoded as JPEG (quality ~85).

    Returns a list of JPEG bytes. On any failure returns an empty list.
    Never raises.
    """
    if not _ffmpeg_ok() or count <= 0:
        return []

    duration = video_duration_seconds(video_path)
    if not duration or duration <= 0.1:
        logger.warning(f"video_service: invalid duration ({duration}) for {video_path}")
        return []

    frames: list[bytes] = []
    tmpdir = tempfile.mkdtemp(prefix="gb_kf_")
    try:
        for i in range(count):
            # Sample at (i+1)/(count+1) * duration to avoid the very first/last frame
            ts = duration * (i + 1) / (count + 1)
            out_path = os.path.join(tmpdir, f"kf_{i:02d}.jpg")
            cmd = [
                "ffmpeg",
                "-hide_banner", "-loglevel", "error",
                "-ss", f"{ts:.3f}",
                "-i", video_path,
                "-frames:v", "1",
                "-vf", f"scale='min({max_dimension},iw)':-2",
                "-q:v", "4",  # JPEG quality (lower = better, 2..5 is typical)
                "-y",
                out_path,
            ]
            try:
                subprocess.run(
                    cmd,
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=30,
                )
            except subprocess.CalledProcessError as e:
                logger.warning(
                    f"video_service: ffmpeg frame @ {ts:.2f}s failed: "
                    f"{(e.stderr or b'').decode()[:200]}"
                )
                continue
            except subprocess.TimeoutExpired:
                logger.warning(f"video_service: ffmpeg frame @ {ts:.2f}s timed out")
                continue

            try:
                with open(out_path, "rb") as f:
                    frames.append(f.read())
            except Exception as e:
                logger.warning(f"video_service: could not read {out_path}: {e}")
    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass

    logger.info(
        f"video_service: extracted {len(frames)}/{count} keyframes from {video_path} "
        f"(duration ~ {duration:.1f}s)"
    )
    return frames


def frame_to_base64(jpeg_bytes: bytes) -> str:
    """Encode JPEG bytes to a plain base64 string (no data URI prefix)."""
    return base64.b64encode(jpeg_bytes).decode("ascii")

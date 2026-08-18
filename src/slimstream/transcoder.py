"""ffmpeg/ImageMagick wrappers (spec 1.9).

The one invariant that matters here: a transcode result must be verified
before it's trusted (non-zero size, decodable stream). A 0-byte or corrupt
output must never be allowed to reach `verified` in the manifest — that's
the one condition that could let a good original be deleted for a bad copy
(IMPLEMENTATION_GUIDE.md Phase 3).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

HEIC_EXTENSIONS = {".heic", ".heif"}
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".3gp"}

# Ubuntu 24.04's `imagemagick` apt package is ImageMagick 6, which has no
# unified `magick` binary — that's IM7-only. IM6 uses separate `convert`/
# `identify` commands. Confirmed empirically on the target VM image
# (dpkg -l shows imagemagick 8:6.9.12.98) rather than assumed from docs.
CONVERT_BIN = "convert"
IDENTIFY_BIN = "identify"


class TranscodeError(RuntimeError):
    """A transcode step failed or its output failed verification."""


@dataclass(frozen=True)
class TranscodeResult:
    output_path: Path
    output_size: int


def _run(args: list[str], *, timeout: int = 1800) -> None:
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as exc:
        raise TranscodeError(f"command not found: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise TranscodeError(f"timed out: {' '.join(args)}") from exc

    if result.returncode != 0:
        raise TranscodeError(
            f"{' '.join(args)} failed (exit {result.returncode}): {result.stderr.strip()}"
        )


def _require_free_space(scratch_dir: Path, needed_bytes: int) -> None:
    """Guard disk before transcode (spec 1.12: VM disk fills mid-run -> failed)."""
    usage = shutil.disk_usage(scratch_dir)
    # require headroom beyond the input size itself, for the output + margin
    if usage.free < needed_bytes * 3:
        raise TranscodeError(
            f"insufficient scratch disk space: {usage.free} bytes free, "
            f"need ~{needed_bytes * 3} for a safe margin"
        )


def _verify_video_output(path: Path) -> None:
    """ffprobe confirms the output is a decodable video stream, not just
    a non-empty file. A corrupt-but-nonzero output must not pass.
    """
    if not path.exists() or path.stat().st_size == 0:
        raise TranscodeError(f"transcode output missing or empty: {path}")
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0 or "video" not in result.stdout:
        raise TranscodeError(
            f"ffprobe could not confirm a decodable video stream in {path}: "
            f"{result.stderr.strip()}"
        )


def _verify_image_output(path: Path) -> None:
    if not path.exists() or path.stat().st_size == 0:
        raise TranscodeError(f"transcode output missing or empty: {path}")
    result = subprocess.run(
        [IDENTIFY_BIN, str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise TranscodeError(
            f"ImageMagick could not identify transcode output {path}: "
            f"{result.stderr.strip()}"
        )


def transcode_video(
    input_path: Path,
    output_path: Path,
    *,
    height: int = 480,
    crf: int = 30,
    preset: str = "slow",
    fps: int = 24,
    audio_bitrate: str = "64k",
) -> TranscodeResult:
    """spec 1.9 VIDEO command. Verifies output before returning success."""
    _require_free_space(output_path.parent, input_path.stat().st_size)

    _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-vf",
            f"scale=-2:{height}",
            "-c:v",
            "libx264",
            "-crf",
            str(crf),
            "-preset",
            preset,
            "-r",
            str(fps),
            "-c:a",
            "aac",
            "-b:a",
            audio_bitrate,
            str(output_path),
        ]
    )
    _verify_video_output(output_path)
    return TranscodeResult(output_path=output_path, output_size=output_path.stat().st_size)


def transcode_image(
    input_path: Path,
    output_path: Path,
    *,
    long_edge: int = 1600,
    quality: int = 60,
) -> TranscodeResult:
    """spec 1.9 STILL command. HEIC inputs need libheif-enabled ImageMagick
    (A4) — if that's unavailable on the target system, pre-convert with
    heif-convert before calling this (branch point, not handled here).
    """
    _require_free_space(output_path.parent, input_path.stat().st_size)

    _run(
        [
            CONVERT_BIN,
            str(input_path),
            "-resize",
            f"{long_edge}x{long_edge}>",
            "-quality",
            str(quality),
            str(output_path),
        ]
    )
    _verify_image_output(output_path)
    return TranscodeResult(output_path=output_path, output_size=output_path.stat().st_size)


def cleanup(*paths: Path) -> None:
    """Remove local scratch files. Always called, success or failure
    (spec 1.7 step e) — missing files are fine, not an error.
    """
    for path in paths:
        path.unlink(missing_ok=True)

"""One-off parameter tuning tool. Not part of the pipeline.

Downloads the test files in MEGA_CAMERA_PATH once, then runs transcoder.py's
real transcode_video/transcode_image across a few parameter combos each,
naming outputs by their parameters so they can be compared side by side.

Usage (on the VM, inside the venv):
    python3 scripts/tune_transcode.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from slimstream.mega_client import MegaClient
from slimstream.transcoder import PHOTO_EXTENSIONS, VIDEO_EXTENSIONS, transcode_image, transcode_video

CAMERA_PATH = "/slimstream-test"
OUT_DIR = Path.home() / "slimstream" / "tuning-output"
DOWNLOAD_DIR = OUT_DIR / "_originals"

VIDEO_PARAMS = [
    {"height": 480, "crf": 23},
    {"height": 480, "crf": 30},
    {"height": 480, "crf": 35},
    {"height": 320, "crf": 30},
]

IMAGE_PARAMS = [
    {"long_edge": 1600, "quality": 50},
    {"long_edge": 1600, "quality": 60},
    {"long_edge": 1600, "quality": 70},
    {"long_edge": 1200, "quality": 60},
]


def safe_stem(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    mega = MegaClient()
    entries = mega.list(CAMERA_PATH)
    print(f"found {len(entries)} files in {CAMERA_PATH}")

    for entry in entries:
        if entry.is_dir:
            continue
        suffix = Path(entry.path).suffix.lower()
        name = Path(entry.path).name
        stem = safe_stem(Path(name).stem)

        print(f"\n=== {name} ({entry.size} bytes) ===")
        local_input = mega.download(entry.path, DOWNLOAD_DIR)

        if suffix in VIDEO_EXTENSIONS:
            for params in VIDEO_PARAMS:
                out_name = f"{stem}_h{params['height']}_crf{params['crf']}.mp4"
                out_path = OUT_DIR / out_name
                result = transcode_video(local_input, out_path, **params)
                ratio = result.output_size / entry.size * 100
                print(f"  {out_name}: {result.output_size:,} bytes ({ratio:.1f}% of original)")
        elif suffix in PHOTO_EXTENSIONS:
            for params in IMAGE_PARAMS:
                out_name = f"{stem}_edge{params['long_edge']}_q{params['quality']}.jpg"
                out_path = OUT_DIR / out_name
                result = transcode_image(local_input, out_path, **params)
                ratio = result.output_size / entry.size * 100
                print(f"  {out_name}: {result.output_size:,} bytes ({ratio:.1f}% of original)")
        else:
            print(f"  skipping unrecognized type: {suffix}")

    print(f"\nAll outputs in: {OUT_DIR}")
    print("Originals (untouched, for reference) in:", DOWNLOAD_DIR)


if __name__ == "__main__":
    main()

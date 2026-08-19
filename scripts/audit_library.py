"""Audit a real Mega library against the production parser.

Read-only. Produces the numbers recorded in docs/library-audit.md:
composition by type, total size, projected savings, catch-up time, and —
importantly — how many rows the parser actually handles.

Run it before a large first batch, and any time MEGAcmd is upgraded: it
is the cheapest way to catch a parser regression against real data
before a scheduled run silently discovers zero files.

Usage (on the VM, inside the venv):
    python3 scripts/audit_library.py                 # uses MEGA_CAMERA_PATH
    python3 scripts/audit_library.py "/Camera Uploads"
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from slimstream.mega_client import (  # noqa: E402
    MEGACMD_TIME_FORMAT,
    MegaParseError,
    _LS_HEADER_PREFIX,
    _parse_ls_line,
)
from slimstream.transcoder import PHOTO_EXTENSIONS, VIDEO_EXTENSIONS  # noqa: E402

# Ratios measured in the real tuning sweep (scripts/tune_transcode.py,
# 2026-08-18) at the chosen defaults: 480p/CRF30 video, 1200px/q60 stills.
COMPRESSION_RATIO = 0.12


def fetch(remote_path: str) -> str:
    return subprocess.run(
        ["mega-ls", "-l", "--show-handles", f"--time-format={MEGACMD_TIME_FORMAT}", remote_path],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def main() -> int:
    remote = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MEGA_CAMERA_PATH")
    if not remote:
        print("pass a remote path or set MEGA_CAMERA_PATH", file=sys.stderr)
        return 2

    print(f"listing {remote!r} ...")
    raw = fetch(remote)

    by_ext: dict[str, list[int]] = {}
    flags_seen: dict[str, int] = {}
    failures: list[tuple[str, str]] = []
    dirs = files = total_bytes = 0

    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith(_LS_HEADER_PREFIX):
            continue
        if line.endswith(":") and "H:" not in line:
            continue

        first = line.split(maxsplit=1)[0]
        flags_seen[first] = flags_seen.get(first, 0) + 1

        try:
            entry = _parse_ls_line(line, parent_path=remote)
        except MegaParseError as exc:
            failures.append((str(exc)[:100], line))
            continue

        if entry.is_dir:
            dirs += 1
            continue
        files += 1
        total_bytes += entry.size
        ext = Path(entry.path).suffix.lower()
        slot = by_ext.setdefault(ext, [0, 0])
        slot[0] += 1
        slot[1] += entry.size

    print(f"\nPARSER: {files + dirs} rows parsed, {len(failures)} failed")
    for reason, line in failures[:10]:
        print(f"  FAIL {reason}\n       {line!r}")
    if len(failures) > 10:
        print(f"  ... and {len(failures) - 10} more")

    print("\nFLAGS SEEN")
    for flag, count in sorted(flags_seen.items(), key=lambda kv: -kv[1]):
        print(f"  {flag!r:8} x{count}")

    print(f"\n{'ext':8} {'count':>7} {'GB':>8}  handled")
    handled_bytes = unhandled_bytes = 0
    unhandled_n = 0
    for ext, (count, size) in sorted(by_ext.items(), key=lambda kv: -kv[1][1]):
        known = ext in PHOTO_EXTENSIONS or ext in VIDEO_EXTENSIONS
        if known:
            handled_bytes += size
        else:
            unhandled_bytes += size
            unhandled_n += count
        print(f"  {ext:8} {count:7} {size / 1e9:8.2f}  {'yes' if known else 'NO -> skipped'}")

    print(f"\nfiles {files}  dirs {dirs}  total {total_bytes / 1e9:.1f} GB")
    if unhandled_n:
        print(f"unhandled: {unhandled_n} files, {unhandled_bytes / 1e9:.3f} GB")

    projected = handled_bytes * COMPRESSION_RATIO
    print(
        f"projected compressed ~{projected / 1e9:.1f} GB "
        f"(reclaim ~{(handled_bytes - projected) / 1e9:.0f} GB)"
    )

    print(f"\ncatch-up time for {files} files, one run per day:")
    for batch in (20, 100, 250, 500, 1000):
        days = files / batch
        print(f"  MAX_BATCH_SIZE={batch:5} -> {days:7.0f} days ({days / 365:.1f} years)")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

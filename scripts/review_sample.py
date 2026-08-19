"""Pick what a human should actually look at.

Reviewing 22k compressed files one by one is not a plan. This narrows it
to two much smaller sets:

1. **Outliers** — files whose compression ratio is far from normal.
   Both tails matter. A ratio near zero suggests the transcode produced
   something degenerate (a black frame, a truncated clip) even though it
   passed the decodable check; a ratio near 100 % suggests the transcode
   barely did anything and the file is not really compressed.
2. **A stratified random sample** — spread across media type and capture
   year, so the eyeball check covers old and new, photo and video, rather
   than whatever happens to sort first.

Everything printed is a Mega path: open them in the Mega app on a phone,
which is the only honest way to judge "does this still look like the
memory I wanted to keep".

Usage (on the VM, inside the venv):
    python3 scripts/review_sample.py                # 20 sampled + outliers
    python3 scripts/review_sample.py --sample 40
"""

from __future__ import annotations

import argparse
import os
import random
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# Ratio bands used to flag "look at this one". Deliberately wide: the
# point is to surface the handful worth a human's attention, not to
# second-guess every file.
SUSPICIOUS_LOW = 0.01  # < 1 % of original — likely degenerate output
SUSPICIOUS_HIGH = 0.60  # > 60 % of original — barely compressed


def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.environ.get("MANIFEST_DB_PATH"))
    parser.add_argument("--sample", type=int, default=20)
    parser.add_argument("--seed", type=int, default=None, help="fix for a reproducible sample")
    args = parser.parse_args()

    if not args.db:
        print("pass --db or set MANIFEST_DB_PATH", file=sys.stderr)
        return 2

    conn = open_db(Path(args.db))
    rows = conn.execute(
        """
        SELECT original_path, compressed_path, media_type, captured_at,
               original_size, compressed_size
        FROM files
        WHERE state IN ('verified', 'original_deleted')
          AND compressed_size IS NOT NULL AND original_size > 0
        """
    ).fetchall()

    if not rows:
        print("nothing processed yet")
        return 0

    scored = [(r, r["compressed_size"] / r["original_size"]) for r in rows]
    print(f"reviewing {len(scored)} processed files\n")

    # --- distribution -----------------------------------------------------
    print("compression ratio distribution")
    bands = [(0, .01), (.01, .05), (.05, .10), (.10, .20), (.20, .40), (.40, .60), (.60, 1.01)]
    for lo, hi in bands:
        n = sum(1 for _, ratio in scored if lo <= ratio < hi)
        if n:
            bar = "#" * max(1, round(40 * n / len(scored)))
            print(f"  {lo * 100:5.0f}-{hi * 100:3.0f}%  {n:6}  {bar}")

    # --- outliers ---------------------------------------------------------
    broken = [(r, ratio) for r, ratio in scored if r["compressed_size"] >= r["original_size"]]
    too_small = [(r, ratio) for r, ratio in scored if ratio < SUSPICIOUS_LOW]
    too_big = [
        (r, ratio)
        for r, ratio in scored
        if ratio > SUSPICIOUS_HIGH and r["compressed_size"] < r["original_size"]
    ]

    def show(title: str, items, limit: int = 10) -> None:
        if not items:
            return
        print(f"\n{title}  ({len(items)})")
        for r, ratio in sorted(items, key=lambda t: t[1])[:limit]:
            print(
                f"  {ratio * 100:6.2f}%  {r['original_size'] / 1e6:7.1f}MB -> "
                f"{r['compressed_size'] / 1e6:6.2f}MB  {r['compressed_path']}"
            )
        if len(items) > limit:
            print(f"  ... and {len(items) - limit} more")

    show("!! LARGER THAN THE ORIGINAL — this would be a bug, inspect first", broken)
    show(f"suspiciously small (< {SUSPICIOUS_LOW * 100:.0f}%) — check for degenerate output", too_small)
    show(f"barely compressed (> {SUSPICIOUS_HIGH * 100:.0f}%) — check the transcode ran", too_big)

    if not (broken or too_small or too_big):
        print("\nno outliers outside the expected bands")

    # --- stratified sample ------------------------------------------------
    rng = random.Random(args.seed)
    strata: dict[tuple[str, str], list] = defaultdict(list)
    for r, ratio in scored:
        year = (r["captured_at"] or "unknown")[:4]
        strata[(r["media_type"], year)].append((r, ratio))

    per_stratum = max(1, args.sample // max(1, len(strata)))
    print(f"\nrandom sample across {len(strata)} strata (media type x year) — open these in Mega:")
    for key in sorted(strata):
        picked = rng.sample(strata[key], min(per_stratum, len(strata[key])))
        for r, ratio in picked:
            print(f"  [{key[0]:5} {key[1]}] {ratio * 100:5.1f}%  {r['compressed_path']}")

    print(
        "\nReminder: nothing is deleted yet, and Job B moves originals to a trash\n"
        "folder you empty by hand — so this review has two safety nets behind it,\n"
        "not one. Review before emptying trash, not before finishing the backlog."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

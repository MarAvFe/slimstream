# Library audit — 2026-08-19

Measured against the real `/Camera Uploads` account, not estimated. Every
number here came from parsing the full 22,146-row `mega-ls` listing with
the production parser (`scripts/`-free, run directly against
`slimstream.mega_client._parse_ls_line`).

Re-run the numbers any time with `scripts/audit_library.py`.

## Scale

| | |
|---|---|
| Files | **22,144** (+2 directories: `keepers`, `recyclingbin`) |
| Total size | **117.7 GB** |
| Photos | 49.7 GB |
| Videos | 68.0 GB |

## Composition

| ext | count | size | handled by pipeline |
|---|---|---|---|
| `.mp4` | 2,424 | 68.00 GB | yes |
| `.jpg` | 19,687 | 49.65 GB | yes |
| `.png` | 26 | 0.02 GB | yes |
| `.gif` | 5 | 0.01 GB | **no — skipped** |
| `.jpeg` | 2 | ~0 GB | yes |

Videos are 2% more than half the bytes despite being 11% of the files —
they dominate both the storage win and the CPU cost per run.

The 5 GIFs are logged as `skipping unrecognized file type` and never
touched. Deliberate: compressing them through the still-image path would
flatten the animation to a single JPEG frame. Revisit only if animated
output matters.

## Measured runs (2026-08-19)

Wall-clock from the first real `job-a` runs against the live library,
`DRY_RUN_UPLOAD=false`, on the `s-2vcpu-2gb` droplet.

| run | batch | wall clock | per file |
|---|---|---|---|
| 1 | 20 | 17:57:01 → 17:58:03 = **62 s** | 3.10 s |
| 2 | 200 | 19:02:27 → 19:13:52 = **11 m 25 s** | 3.42 s |
| 3 | 200 | 19:58:42 → 20:07:35 = **8 m 53 s** | 2.66 s |

Per-file timing from the manifest (gaps between consecutive
`verified_at`, 218 samples): **avg 3.37 s, min 1.68 s, max 7.86 s**.

Runs 2 and 3 were the same batch size but differed by 2.5 minutes
(3.42 vs 2.66 s/file, a 29 % spread) purely on file mix — same machine,
same settings, consecutive slices of the same library. Treat any single
run as an estimate with roughly ±30 % of spread, and size batches off the
slower figure rather than the faster one.

Timings include discovery, which re-lists and re-checks all ~22k rows on
every run. That is a fixed overhead of roughly 1–2 s per run, so it is
noise at batch 200 and would be pure waste at batch 5.

### Measured compression, first 220 files

| media | files | original | compressed | ratio |
|---|---|---|---|---|
| photo | 220 | 610.0 MB | 21.9 MB | **3.6 %** |
| video | 0 | — | — | not yet measured |

**Both caveats matter before extrapolating:**

1. **No videos have been processed yet.** Files are handled oldest-first,
   and the oldest ~2,000 are 2014-era photos. Videos are 58 % of the
   bytes and are far slower per file, so the 3.37 s/file figure is a
   photo-only measurement and will rise sharply once videos appear.
2. **3.6 % is not representative of the whole library.** These are old,
   large-dimension photos being downscaled hard to 1200 px. The
   2026-08-18 tuning sweep on *recent* Pixel photos measured ~12.7 % at
   the same settings. Expect the real library-wide photo ratio to land
   between the two, nearer 12 % for anything modern.

### Revised projection

Photo-only extrapolation, for the ~19,720 remaining photos at 3.37 s:
**≈ 18 hours** of compute. Videos are unmeasured; at a plausible
20–60 s each, 2,424 videos add **13–40 hours**. Total order of magnitude
is therefore **a day and a half to two and a half days of CPU**, not
weeks — the calendar time below is dominated by how often you choose to
run, not by the work itself.

Re-measure once the first video batch completes; that is the number that
actually decides the schedule.

## Projected savings

Using the compression ratios actually measured in the 2026-08-18 tuning
sweep (`scripts/tune_transcode.py`) at the chosen defaults — 480p/CRF 30
for video, 1200px/q60 for stills:

| | |
|---|---|
| Current | 117.7 GB |
| Projected compressed | **~14 GB** |
| Reclaimed | **~104 GB** |

That is the project's premise, confirmed against the real library rather
than assumed.

## Catch-up time

`MAX_BATCH_SIZE` caps how many files Job A *processes* per run (discovery
always lists everything). For 22,144 backlogged files at one run per day:

| `MAX_BATCH_SIZE` | days to clear backlog |
|---|---|
| 20 | 1,107 (≈3.0 years) |
| 100 | 221 |
| 250 | 89 |
| 500 | **44** |
| 1000 | 22 |

**20 is not viable for the initial catch-up.** With measured throughput
of ~3.4 s/file (photos), the wall clock per run is:

| `MAX_BATCH_SIZE` | run duration (photos) |
|---|---|
| 200 | ~11 min *(measured)* |
| 500 | ~28 min |
| 1000 | ~57 min |
| 2000 | ~1 h 55 m |

Since the bottleneck is CPU time rather than any quota (spec 1.13 —
inbound transfer is free and Mega's allowance is not a constraint), the
backlog can be cleared with a handful of large runs rather than a year of
small daily ones. Videos will slow this down considerably; re-check after
the first batch that contains them.

**Long runs over plain SSH get killed by disconnects** — this already
happened once and stranded a record in `compressing` (see D9). Run big
batches detached:

```bash
nohup .venv/bin/slimstream job-a > /dev/null 2>&1 &
tail -f ~/slimstream/logs/slimstream.log
```

Once the backlog is cleared, steady-state volume is only whatever the
phone uploads each day, so the cap stops being the limiting factor.

## Flags worth knowing about

**133 files carry `-ep-` flags — they have live public/exported links.**
When Job B eventually moves an original into `recyclingbin`, any public
link pointing at it will likely break. Not a code issue, but worth
checking whether any of those shared links still matter before enabling
`DRY_RUN_DELETE=false`.

## Parser findings from the same audit

Recorded here because they were measured, not reasoned about (spec 1.5).
See `IMPLEMENTATION_GUIDE.md` D7 for the resulting decisions.

| flags value | rows | meaning |
|---|---|---|
| `----` | 22,011 | plain file |
| `-ep-` | 133 | exported / public link |
| `d---` | 2 | directory |

Parser results before and after the 2026-08-19 rewrite:

| | old parser | new parser |
|---|---|---|
| `--time-format=ISO6081` | 22,013 ok / 133 fail | **22,146 / 0** |
| default time format | **0 ok / 22,146 fail** | **22,146 / 0** |

Both formats independently agree on 117.7 GB across 22,144 files, which
is the real correctness check on the rewrite.

Probed MEGAcmd exit codes (undocumented):

| code | meaning |
|---|---|
| 53 | `Couldn't find "<path>"` — missing file or folder |
| 54 | `Folder already exists: <name>` |

## A3 revisited — `captured_at` is reliable after all (2026-08-19)

Phase 0's A3 concluded that Mega's reported timestamp is *upload* time
rather than capture date, and `RETENTION_KEY` was defaulted to
`discovered_at` because of it. Measured against the real library, that
conclusion is wrong.

A3 tested by running `mega-get` then `mega-put` — a **manual re-upload**,
which naturally stamps a fresh mtime. That is not how this library was
populated. Files delivered by Mega's camera-upload feature preserve the
phone's file mtime, which is the capture time.

Comparing Pixel's own filename (`YYYY-MM-DD HH.MM.SS.ext`, local time)
against `captured_at` (Mega mtime, UTC) across all 22,139 records:

| | count | share |
|---|---|---|
| filename date == `captured_at` | 16,818 | 76.0 % |
| `captured_at` is next day, filename time ≥ 18:00 | 5,172 | 23.4 % |
| **consistent with UTC−6 capture time** | **21,990** | **99.3 %** |
| unexplained (likely re-uploads / edited copies) | 149 | 0.7 % |

Every mismatch is exactly +1 day and clusters in the evening — the
signature of a UTC−6 local time crossing midnight in UTC, not of a
corrupted or upload-derived timestamp.

### What this changes

`captured_at` is usable, so the retention key is now a real choice rather
than a forced fallback:

| | effect on the current backlog |
|---|---|
| `discovered_at` (current) | everything was discovered 2026-08-19, so **nothing is deletable until 2026-09-18** |
| `captured_at` | all 920 verified files are **immediately deletable** (they are 2014–2025 photos) |

Verified against the live manifest: Job B selects **0** files today under
`discovered_at` and **920** under `captured_at`.

Worth noting the accident is a useful one. `discovered_at` gives the
first bulk run a free 30-day quarantine in which the compressed copy and
the original both exist, which is exactly the review window Phase 4/5
asks for — and in steady state, once caught up, `discovered_at` is within
a day of capture date anyway. `captured_at` is the right switch only if
the goal is to reclaim the backlog's space sooner than a month from now.

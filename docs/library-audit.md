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
| 4 | 500 | 20:49:18 → 21:19:11 = **29 m 53 s** | 3.59 s |

Whole-run averages hide the only variable that matters, which is media
type. Measuring gaps between consecutive `verified_at` **within** a run
(inter-run idle gaps excluded — an early version of this measurement let
a 45-minute gap between runs through and reported it as a 2,695 s
"photo"):

| media | samples | avg | min | max | avg size |
|---|---|---|---|---|---|
| photo | 899 | **2.89 s** | 1.48 s | 9.38 s | 2.0 MB |
| video | 17 | **26.41 s** | 3.82 s | 194.92 s | 19.8 MB |

Videos are ~9× slower per file. Their cost tracks size almost linearly at
**≈1.3 s per MB** — the 151 MB clip took 194.9 s (1.29 s/MB), and the
larger files cluster tightly around 1.1–1.3 s/MB.

**Batch size in *files* is therefore a poor proxy for run length.** 500
photos is half an hour; 500 videos would be closer to 3.7 hours. The
largest video in the library is 656 MB, which alone projects to ~14
minutes.

### Measured compression by type

| media | files | original | compressed | ratio |
|---|---|---|---|---|
| photo | 903 | 1,772.5 MB | 92.0 MB | **5.2 %** |
| video | 17 | 336.1 MB | 31.2 MB | **9.3 %** |

The photo figure still skews optimistic: these are the oldest files in
the library and the sample averages 2.0 MB against a library-wide photo
average of 2.6 MB. The 2026-08-18 tuning sweep measured ~12.7 % on recent
Pixel photos, so expect the true blended photo ratio to land between the
two.

### Projected total work

Using the measured rates against what is left (18,811 photos, 2,407
videos / 63.0 GB):

| | remaining | rate | time |
|---|---|---|---|
| photos | 18,811 files | 2.89 s/file | **≈ 15 h** |
| videos | 63.0 GB | ≈1.25 s/MB | **≈ 22 h** |
| **total** | | | **≈ 37 h** |

So roughly **a day and a half of CPU**, now measured rather than guessed.
Videos are 60 % of the remaining time despite being 11 % of the files.

## Projected savings

Using the ratios measured on real processed files (5.2 % photo, 9.3 %
video — see above), rather than the earlier tuning-sweep estimate:

| | photos | videos | total |
|---|---|---|---|
| Current | 49.7 GB | 68.0 GB | **117.7 GB** |
| Projected compressed | ~2.6–4 GB | ~6.3 GB | **~9–10 GB** |
| Reclaimed | | | **~108 GB** |

Slightly better than the original ~104 GB estimate, and now grounded in
920 actually-processed files instead of a 6-file sample. The photo range
reflects the old-files skew noted above.

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

**20 is not viable for the initial catch-up.** Wall clock per run,
measured for photos — but note this stops holding once a batch reaches
the video-heavy stretches (files are processed oldest-first, and videos
begin at position 855):

| `MAX_BATCH_SIZE` | all photos | all videos |
|---|---|---|
| 200 | ~10 min *(measured)* | ~1 h 28 m |
| 500 | ~30 min *(measured)* | ~3 h 40 m |
| 1000 | ~48 min | ~7 h 20 m |
| 2000 | ~1 h 36 m | ~14 h 40 m |

Real batches are mixed, so actual durations fall between the columns —
but a batch sized against the photo column can run 7× longer than
expected when it lands in video territory.

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

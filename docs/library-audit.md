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

**20 is not viable for the initial catch-up.** Suggested approach: run
the first real batch at 100 to measure actual wall-clock throughput and
review output quality across a real spread of files, then raise to ~500.
Videos are the slow part, so the mix in a given batch matters more than
the count — measure before committing to a number.

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

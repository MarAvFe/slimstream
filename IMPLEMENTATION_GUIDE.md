# slimstream — Implementation Guide (the "how")

`PROJECT_SPEC.md` is the *why*. This document is the *how*: the ordered execution plan, the human steps that can't be automated, and the concrete requirements each artifact must satisfy.

**Read this rule first.** Phases are gates, not suggestions. Phase 0 must complete before any code in Phase 2+ is trusted, because four design decisions below are still *unresolved pending empirical results*. Building past an unresolved gate means building on a guess.

**Audience note:** this is written to be handed to an implementer (human or model) who has not read the design discussion. Every artifact section states its contract — inputs, outputs, invariants, and what "done" means — rather than prescribing code.

---

## 0. Resolved vs. unresolved design decisions

Several "how" questions that the spec left open are resolved here, plus one new risk found while researching MEGAcmd. Everything else waits on Phase 0 tests.

### D1 — `file_id` identity: content hash is NOT available at discovery (resolved)

The spec says key on "content hash (or Mega node handle)". These are not interchangeable, and the difference is load-bearing:

- A **content hash requires downloading the file.** Discovery lists a remote folder; it has not downloaded anything yet. So a record cannot be keyed on content hash at the moment it is created.
- A **node handle is available at list time** (`--show-handles`, format `H:XXXXXXXX`) but MEGA does not document whether a handle survives a move to Rubbish. If it doesn't, the handle is unusable as a stable PK precisely at the moment we care most (deletion).

**Decision — dual identity:**

| Column | Assigned at | Role |
|---|---|---|
| `file_id` (PK) | discovery | `sha256(original_path + original_size + captured_at)` — a *stable synthetic key*, computable from list output alone |
| `node_handle` | discovery | MEGA's `H:XXXXXXXX`, treated as a **mutable address, never identity** — re-resolved before every remote operation |
| `content_sha256` | after download | true content hash; used for dedup and verify, **not** as PK |

Rationale: the PK must be assignable at insert time and must never change. Content hash can't satisfy the first; node handle can't be guaranteed on the second. The synthetic key satisfies both, and `content_sha256` still gives real dedup once the bytes are local.

*Consequence to implement:* if a file is renamed/moved in Mega, it gets a new `file_id` and is re-processed. Deduplicate against `content_sha256` after download and short-circuit to `verified` if those bytes were already handled. This is the correct trade: re-downloading a file wastes bandwidth (free inbound, per spec 1.13); mis-identifying one risks a bad delete.

### D2 — Manifest backend: sqlite + WAL, with export (resolved)

Spec 1.17 left this open. **Decision: local sqlite** with `journal_mode=WAL`, plus a nightly plaintext export (JSON) uploaded to Mega.

Rationale: Job A and Job B run as separate processes and can overlap; WAL gives concurrent read during write and real transactional guarantees, which a JSON control-file in Mega cannot (a partial write to a cloud JSON file corrupts the whole state store). The spec's own worry — "survives VM loss" — is solved by the export, not by making the live store remote. Verified available: sqlite 3.45.1 with WAL on the target Python 3.12.

Non-negotiable: **the state transition and its side-effect must commit in one transaction.** Never write "deleted" before the delete returns success.

### D3 — Hashing: `hashlib.sha256` from stdlib (resolved)

Non-cryptographic need, but stdlib-only beats adding a dependency (blake3 confirmed absent). Hash in streaming chunks (never load a video into memory). Speed is irrelevant next to transcode cost.

### D4a — Delete mechanism: `mv` to an explicit trash folder, always (resolved)

Decided regardless of what A2 finds: `mega-rm` semantics are undocumented (confirmed while researching this guide — the UserGuide never states whether `rm` goes to Rubbish or deletes permanently), so the safety net should not depend on an unverified vendor behavior at all. Job B moves originals to `MEGA_TRASH_PATH` (a normal folder under our control) instead of calling `rm`.

This makes A2 informational rather than load-bearing — worth still running once, so you know whether MEGA's own Rubbish bin is a second, redundant safety net, but the design no longer branches on its result. `move_to_trash` in `mega_client` has exactly one implementation: `mv` to `MEGA_TRASH_PATH`. Emptying that folder stays a manual, human, out-of-band action.

### D4b — Retention: monthly on a fixed day, not a rolling daily window (resolved)

Spec 1.8 specified a rolling 30-day window evaluated daily ("every day is one day further in the past"). Revised: Job B runs **once a month, on a fixed day (default: the 30th; falls back to the last day of the month for shorter months)**, and deletes every `verified` original older than `RETENTION_DAYS`. Job A still runs daily — only Job B's cadence changes.

Rationale: matches the existing manual habit (spec 1.3 — originals are deleted from phone/Google Photos monthly already), and a monthly batch is easier to review in one sitting than daily trickle-deletes. The safety invariant (`verified`-only, `retention_key` cutoff) is unchanged — only *how often the query runs* changes, not what it selects.

Config: `RETENTION_RUN_DAY=30` (falls back to last-day-of-month). Scheduler unit for Job B fires monthly, not daily (see Phase 6).

### D4c — MEGAcmd output parsing is a genuine fragility (NEW — unresolved, gated on A6)

Research finding: **MEGAcmd documents no stable machine-readable output format** for `ls`/`find`. The pipeline's entire view of remote state comes from parsing this text. A format change silently breaks discovery.

Mitigations required in `mega_client`:
- Pin the MEGAcmd version; record it in the manifest's meta table.
- Force deterministic output: `--time-format=ISO6081`, `-l`, `--show-handles`.
- **Parse strictly and fail loud.** A line that doesn't match the expected shape raises — it is never skipped silently. A silently-skipped file is an invisible data-loss path.
- Contract-test the parser against captured real output fixtures (Phase 0 produces these).

### D5 — Compressed output lands in a mirrored tree, not next to the original (resolved, 2026-08-18)

Originally, Job A uploaded the compressed copy into the **same folder** as the original (`Camera Uploads`). This created a real gap: if the manifest sqlite db were ever lost, a rediscovery run would see the original *and* the compressed copy as two new, indistinguishable `discovered` files — re-downloading/re-transcoding the original (wasted work) and attempting to compress the already-compressed file too (a real quality problem, not just waste), since nothing in the file itself marked it as "already done."

**Decision:** `MEGA_COMPRESSED_ROOT` (must differ from `MEGA_CAMERA_PATH`, enforced at config load). Compressed output mirrors the original's path 1:1 under this separate root — same relative path, same filename, different top-level folder. `config.compressed_path_for(original_path)` computes the mirror.

Two benefits, one basically free once the other is chosen:
1. **Browsable.** The compressed tree has the identical folder structure and filenames as `Camera Uploads`, so finding a compressed copy of a known original is just swapping the root — no date-bucketing scheme needed, and no dependency on a timestamp field (which A3 already showed is unreliable — Mega's reported time is upload time, not EXIF capture time — so bucketing by "year/month" would have inherited that same problem).
2. **Manifest-loss resilient.** `_process_one` now checks, *before downloading anything*, whether a file already exists at the mirrored path via a plain `stat()`. If it does, the record short-circuits straight to `verified` — no download, no re-transcode, no re-upload. This check needs zero manifest state to be trustworthy, because "already compressed" is now a fact about *where a file lives*, not something only the manifest remembers.

This doesn't replace the D2 nightly manifest export (still worth having, for full state — retry counts, failure history, etc.) — it specifically closes the worst failure mode of manifest loss: silent double-processing of already-compressed media.

### D6 — `mega.list()` is not recursive; keepers protection is defense in depth, not the only guard (found 2026-08-19)

`mega.list(path)` only lists the **top level** of a folder — it does not descend into subfolders. In production this first surfaced as a crash: `Camera Uploads` contains a real `keepers` subfolder, and the directory row in `mega-ls` output (`d---    -            - ... keepers`) wasn't handled by the parser, which only expected file rows. Fixed in `mega_client.py` (directory rows print `-` for both VERS and SIZE; `is_dir`/`size=0` now parsed explicitly, still raising if a directory row doesn't match that exact shape — D4c's fail-loud rule holds).

**Separately, and more subtly:** because `list()` doesn't recurse, files placed *inside* `keepers` are never returned by discovery's listing call at all — `_is_under_keepers()`'s path-prefix check never actually fires on a real (flat) `Camera Uploads`, since nothing nested is ever seen in the first place. Today, on a flat library, `keepers` content is safe by construction (never listed, so never processed) — but `_is_under_keepers()` is kept anyway as **defense in depth**: if `list()` is ever made recursive (e.g. to support nested album folders), this check is what would actually stop `keepers` content from being swept into the compress pipeline, instead of relying on "list() happens not to recurse" as the only safety net. Deliberately not building recursion now — no deployer has a nested structure yet, and it would reopen the same untested-real-output risk A6 already exposed once for the flat case.

### D7 — Never infer MEGAcmd semantics from POSIX; probe them (resolved 2026-08-19)

Four production crashes in a row shared one root cause: assuming MEGAcmd behaves like the familiar Unix tool it resembles. Each was found by a real run failing, not by a test, because the test doubles encoded the same assumption as the code.

| assumed | actually |
|---|---|
| `ls` FLAGS is `----` for every file | 133 of 22,146 rows are `-ep-` (exported / public link) |
| `--time-format=ISO6081` guarantees a space-free DATE | default format emits two tokens; **0 of 22,146 rows parsed** under it |
| `ls` on a missing path returns an empty listing | exits **53** — so `stat()` raised instead of returning `None` |
| `mkdir -p` succeeds if the folder exists | exits **54** — so every upload crashed once the root existed |

Resulting rules, all now enforced by tests:

1. **Validate only fields that drive behavior.** FLAGS position 0 (the directory bit) is checked; positions 1–3 are status we never read and must not be able to fail a run. D4c's fail-loud rule means *be strict about what you consume*, not about decoration — over-validation converts harmless vendor variation into an outage.
2. **Parse by anchor, not by field count.** The row parser locates the `H:` handle token rather than counting whitespace runs, so a one- or two-token DATE both work. Verified 22,146/22,146 under both formats, with both formats independently agreeing on 117.7 GB — that agreement is the real check, not the unit tests.
3. **Exit codes are probed and named** (`EXIT_NOT_FOUND = 53`, `EXIT_ALREADY_EXISTS = 54`), and only the specific expected code is absorbed. Any other failure still raises: silently treating an auth or network error as "file absent" would make the pipeline redo work or, worse, believe a verify step passed.
4. **`stat()` lists the exact path, never the parent.** Listing the parent both raised when the parent was missing (the state on every first run) and was O(folder size) per call — with ~22k files eventually in the compressed root, that is a full listing fetched and parsed for every single file processed.
5. **Test doubles must be able to fail the way the real CLI fails.** `FakeMegaClient` politely returned `None` and no-op'd `mkdir`, so it validated the bug. Failure-mode fakes (`_FailingRun`) now cover exit 53/54 and the "unexpected error still propagates" case.

`scripts/audit_library.py` re-runs the whole parser against a real account and reports any row it cannot handle. **Run it after any MEGAcmd upgrade** — it is the cheapest way to catch a format regression before a scheduled run silently discovers zero files. Measured results live in `docs/library-audit.md`.

### D9 — Records stranded in `compressing` are reaped, not orphaned (resolved 2026-08-19)

Spec 1.12 claims a worker crashing mid-transcode is "stuck at `compressing`; next run resumes; idempotent by design." Nothing implemented that. `get_pending()` selects only `discovered` and `failed`, so an interrupted run stranded its in-flight record **permanently and silently**: never compressed, and never deleted either, since Job B only ever selects `verified`. The file simply fell out of the pipeline with no error recorded anywhere.

Found in production when an SSH disconnect (`client_loop: send disconnect: Broken pipe`) killed a long interactive run and left exactly that state behind. This is not an edge case — it is the expected outcome of any long run over a plain SSH session, plus every crash, OOM, or VM reboot.

Job A now reaps stranded records at the start of each run, routing them back through the normal `failed` path rather than resuming them in place. Going via `failed` is deliberate: `retry_count` still climbs, so a file that reliably kills the worker eventually parks for human review instead of being reaped and re-killed forever. The count surfaces in `runs.log` as `reaped=N`.

This assumes Job A never runs concurrently with itself — already required (overlapping runs would double-process) and true for both the systemd `Type=oneshot` unit and manual invocation. Note the practical corollary: **run large batches detached** (`nohup`, `tmux`, or the systemd unit), not from an interactive SSH session that a dropped connection can kill.

### D8 — Run summaries report successes and failures separately (resolved 2026-08-19)

`JobAResult` counted every *attempt* as `processed`, so a batch in which all 20 files errored still wrote `processed=20` to `runs.log`. For an unattended pipeline whose monitoring surface is that one line, a summary that reports total failure as work done is worse than no summary. `succeeded` and `failed` are now tracked and printed separately, and `_process_one` returns whether the file reached a good terminal state.

---

## Phase 0 — The assumption gate (human-run, before any pipeline code)

**Nothing in Phase 2+ may be trusted until this completes.** These are cheap real-world tests, not reasoning exercises. Budget: one evening.

Use a **throwaway Mega folder with junk files**, never real memories.

### Setup (human)
1. Provision the VM (spec 1.13: 1–2 vCPU; inbound free, outbound metered).
2. Install MEGAcmd, ffmpeg, ImageMagick (with libheif), Python 3.12. **Record exact versions** — they go in the manifest meta table.
3. `mega-login` once. Do not log out.
4. Create test tree: `/slimstream-test/` with a few junk files, plus one real-format sample of each: Pixel HEIC still, HEVC video, Motion Photo.

### Tests — run in this order

| # | Test | Command sketch | Records |
|---|---|---|---|
| A2 | Does `rm` go to Rubbish? (informational only — see D4a; Job B uses `mv` regardless) | `mega-rm` a junk file → inspect Rubbish bin | Rubbish contents |
| A2b | Does a node handle survive a move (to trash or Rubbish)? **RUN THIS FIRST** — feeds D1's identity design | note `H:` before, compare after | handle stability |
| A1 | Session survives reboot | login → reboot → `mega-ls` | needs re-auth? |
| A3 | Is capture date preserved? | upload known-EXIF photo → compare Mega timestamp | which date is real |
| A4 | Do Pixel formats transcode? | run 1.9 commands on each sample by hand | per-format notes |
| A5 | Mid-upload file safety | trigger phone upload during a list | truncation seen? |
| **A6** | **Output format stability (NEW)** | capture raw `ls -l --show-handles --time-format=ISO6081` output | **save as parser fixtures** |

### Each result changes the build — this is the point of the gate

| Result | Design consequence |
|---|---|
| A2 — **run, informational** | `mega-rm`'d file did not appear in `/Rubbish`, and `/Rubbish` isn't even a resolvable CLI path on this account. Confirms D4a was the right call: Job B never depended on `rm`/Rubbish semantics, and this result shows why — they're not reliably observable at all, let alone documented. No change (D4a already used `mv`). |
| A2b — **run, confirmed true** | Node handle survived a `mega-mv`. Handles are more stable than assumed; D1's synthetic `file_id` remains the PK regardless (no reason to weaken it), but `node_handle` can be trusted as a real address, not just a best-effort one. |
| A1 — **run, confirmed true** (done during infra setup) | Session survived a full VM reboot without re-login. No change needed. |
| A3 — **run, false** | `--show-creation-time`/`--time-format` report Mega's upload/file timestamp, not EXIF capture date — confirmed by testing against files re-uploaded via `mega-get`+`mega-put`, where the reported date tracked the re-upload, not the original photo. **Retention now defaults to `discovered_at`** (config.py, .env.example) instead of `captured_at`. `captured_at` remains a selectable option for setups that verify it's accurate for their upload path, but it is no longer safe to assume. |
| A4 — **skipped**, not applicable | No HEIC media in this deployer's library. The HEIC/heif-convert branch in `transcoder.py` remains unimplemented; add it only if a deployer's phone actually produces HEIC. |
| A5 — **skipped**, deferred | Mid-upload race not tested. `SETTLING_MINUTES` config exists as the intended mitigation but isn't wired into discovery logic yet — treat as an open risk, not a confirmed-safe gap. |
| A6 — **run, format captured and parser fixed** | Real `mega-ls -l --show-handles` output differs substantially from the guide's original guess: `FLAGS` is 4 dashes (`----`) not a 10-char `-rwx...` string, `DATE` has no time component despite `--time-format=ISO6081`, and `NAME` can contain spaces (Pixel's own `"2026-08-03 10.10.48.jpg"` filenames). `mega_client.py`'s parser was rewritten against the real captured fixture (`tests/fixtures/ls_output_sample.txt`) and verified against the live droplet directly, not just the fixture. Directory-flag shape is still unconfirmed (no directories were in the test sample) — the parser deliberately raises rather than guesses if one is encountered. |

**Deliverable:** `docs/assumptions-results.md` — each row with date, command run, raw output, verdict, and resulting decision. Commit the captured output as test fixtures.

---

## Phase 1 — Repository & configuration skeleton

```
slimstream/
├── src/slimstream/
│   ├── config.py        # load + validate settings
│   ├── manifest.py      # schema, state machine, queries
│   ├── mega_client.py   # MEGAcmd wrapper
│   ├── transcoder.py    # ffmpeg/ImageMagick wrapper
│   ├── worker.py        # Job A + Job B entrypoints
│   └── cli.py
├── tests/fixtures/      # captured MEGAcmd output from Phase 0
├── docs/assumptions-results.md
├── .env.example         # committed; real .env NEVER committed
└── pyproject.toml
```

### Human steps
1. `.gitignore` must contain `.env`, `*.db`, `scratch/` **before the first commit that could touch them.**
2. Create `.env` with `chmod 600`.
3. Config via env vars with validation **at startup, not at use** — a bad `RETENTION_DAYS` must fail before any file is touched, not midway through a delete loop.

**`.env` loading is automatic — no manual `source .env` needed.** `cli.py` loads `.env` from the current working directory via `python-dotenv` before reading config, so `cd ~/slimstream/app && .venv/bin/slimstream job-a` just works. Real environment variables (e.g. set by systemd's `EnvironmentFile=`, or explicit shell `export`) always take priority over `.env` — this keeps manual runs, systemd, and cron all behaving the same way without requiring different invocation styles. Use `--env-file <path>` to point at a `.env` outside the working directory.

### Config surface (all defaults from spec 1.9 / 1.8)
`MEGA_CAMERA_PATH`, `MEGA_KEEPERS_PATH`, `MEGA_TRASH_PATH`, `MEGA_COMPRESSED_ROOT`, `RETENTION_DAYS=30`, `RETENTION_RUN_DAY=30`, `RETENTION_KEY=discovered_at`, `VIDEO_HEIGHT=480`, `VIDEO_CRF=30`, `IMAGE_LONG_EDGE=1200`, `IMAGE_QUALITY=60`, `SCRATCH_DIR`, `MANIFEST_DB_PATH`, `LOG_DIR`, `DRY_RUN_UPLOAD=true`, `DRY_RUN_DELETE=true`, `SETTLING_MINUTES`, `MAX_BATCH_SIZE=100`.

**`MEGA_COMPRESSED_ROOT` (D5) is where compressed copies actually land — never back into `MEGA_CAMERA_PATH`.** Must differ from `MEGA_CAMERA_PATH`, enforced at config load. See D5 above for the full rationale (browsability + manifest-loss resilience).

`VIDEO_CRF` is ffmpeg's libx264 Constant Rate Factor (spec 1.9) — the quality/size dial, roughly logarithmic, lower = larger/better, ~18 "visually lossless," ~23 the libx264 default. Tuned by visual comparison against real footage (2026-08-18, `scripts/tune_transcode.py`): CRF 30 confirmed as an acceptable tradeoff — CRF 23 looked better but saved far less space, working against the project's actual goal. `IMAGE_LONG_EDGE` similarly tuned down from spec 1.9's original 1600 to 1200.

**`DRY_RUN_UPLOAD` and `DRY_RUN_DELETE` are two independent flags, not one — this replaces the original single `DRY_RUN`.** Uploading a compressed copy is fully reversible (it's just an extra file sitting in Mega you can inspect before trusting it), but deleting an original is the one irreversible action in the whole pipeline (spec 1.6's invariant). Both default `true`. The intended rollout: flip `DRY_RUN_UPLOAD=false` first and let Job A upload for real so you can eyeball actual compressed output in Mega on your own devices, keeping `DRY_RUN_DELETE=true` the whole time — only flip that once Job A's output is trusted.

**Logging is split into two files under `LOG_DIR`** (defaults to `<MANIFEST_DB_PATH's dir>/logs` if unset): `slimstream.log` is the full verbose per-file log (`tail -f` while a run is in progress), `runs.log` is one appended line per invocation — timestamp, mode, batch size, and outcome counts — for a fast human-readable history without wading through the verbose log. Any unhandled crash is caught in `cli.py` and still lands in both files (as `CRASHED: <message>` in `runs.log`, full traceback in `slimstream.log`) rather than only reaching `journalctl` — a silent-to-the-logs crash defeats the point of having them.

**`MAX_BATCH_SIZE` caps how many files Job A *processes* (downloads/transcodes/uploads) per run — never how many it *discovers*.** Discovery always lists the full remote folder and inserts every unseen file into the manifest, so the manifest stays a complete picture of reality on every run; only the processing step is throttled. `get_pending()` orders oldest `discovered_at` first, so a capped run works through backlog oldest-first, and repeated daily runs gradually catch up to the present — this is the intended way to onboard a library with thousands of existing files without one run trying to process all of them. Set it low (e.g. `MAX_BATCH_SIZE=20`) for a first real run to keep the blast radius small and the run fast to review; raise it toward the steady-state default (100) once Job A is trusted.

---

## Phase 2 — `manifest` (build first; everything honors it)

Spec 1.6 gives the schema; add D1's columns (`node_handle`, `content_sha256`) and a `meta` table (tool versions, schema version, pause flag).

**Requirements:**
- State transitions go through **one guarded function**, not scattered `UPDATE`s. Illegal transitions raise.
- The `verified → original_deleted` edge is the only path into `original_deleted`. Enforce structurally — Job B's query cannot express selecting a non-`verified` row.
- Every transition records timestamp + `retry_count` on failure.
- Queries needed: `get_pending()`, `get_deletable(cutoff)`, `upsert_discovered()`, `find_by_content_hash()`, pause-flag read/write.

**Definition of done:** unit tests prove (a) every illegal transition raises, (b) `get_deletable()` returns nothing for non-`verified` rows *even when directly asked for them*, (c) re-running discovery on the same listing produces zero duplicate rows.

Test (b) is the safety property of the entire system. Write it before Job B exists.

---

## Phase 3 — External edges

### `mega_client`
Wraps: `list`, `download`, `upload`, `stat`, `move_to_trash`, `resolve_path`.

- Every method takes/returns manifest-friendly types, never raw CLI strings.
- **`move_to_trash` is always `mv` to `MEGA_TRASH_PATH`** (D4a) — a single implementation, not a branch on A2.
- Strict parsing per D4c; unparseable line → raise.
- Retries with exponential backoff on transient network errors only — **never** retry a destructive op that may have partially succeeded.
- Must be mockable: Phase 0 fixtures drive parser tests with no network.

### `transcoder`
Wraps spec 1.9 commands. Per-format branch driven by A4.

- Returns structured result: output path, size, duration, exit status.
- **Verify the output before declaring success**: non-zero size, and ffprobe confirms a decodable stream. A 0-byte or corrupt output must never be allowed to reach `verified` — that is the one condition that could let a good original be deleted for a bad copy.
- Always writes to `SCRATCH_DIR`; cleans up both files on success *and* on failure.
- Guard disk: check free space before transcode (spec 1.12).

---

## Phase 4 — Job A (compress) — still `DRY_RUN_UPLOAD`

Implements spec 1.7. Job A **never deletes an original.**

Order per file: download → hash (D1 dedup check) → transcode → verify output → upload → **re-stat remote** → `verified` → clean scratch.

- Keepers excluded at discovery, before any work.
- Any exception: record `error`, increment `retry_count`, → `failed`. Never delete on a failure path.
- Idempotent: a crash mid-run leaves a resumable state; the next run continues.
- Backoff on `retry_count`; a row exceeding max retries is parked for human review, not retried forever.

**Human gate:** run against a small real folder with `DRY_RUN_UPLOAD=true` first, then flip to `false` (still `DRY_RUN_DELETE=true`) so Job A uploads for real. **Eyeball the transcoded output** — since upload is now live but delete is not, this can be done directly against real compressed copies sitting in Mega, viewable from any device, without any risk to originals. 480p/CRF 30 confirmed by this exact process on 2026-08-18 (`scripts/tune_transcode.py`) as a reasonable default — see Phase 0 appendix.

---

## Phase 5 — Job B (delete) — the irreversible one

Implements spec 1.8, revised per D4b: **monthly, on a fixed day, not a daily rolling window.** Independent process from Job A (spec 1.7 rationale: a compression bug can't cause a bad delete).

Trigger: scheduler fires Job B once a month, on `RETENTION_RUN_DAY` (default 30th; clamp to last day of shorter months — do not skip the month).

Query, run once per invocation: `state == verified AND <retention_key> < now - RETENTION_DAYS`, where **`retention_key` is decided by A3.** Because the job runs monthly rather than daily, one invocation may select a larger batch — the query itself is unchanged, only the calling cadence is.

- Pause flag checked first.
- Destination is always `mv` to `MEGA_TRASH_PATH` (D4a) — not `rm`.
- Emptying trash stays **manual** — it is the human's recovery window (spec 1.8).
- Log every delete with enough detail to reverse it by hand.

**Rollout, in order, no skipping:**
1. `DRY_RUN_DELETE=true` — read the intended-delete log in full.
2. Live on a handful of files whose originals you would not miss.
3. Confirm they are recoverable from trash. *Actually restore one.*
4. Only then schedule it.

**MVP ends here** (spec 1.16). Stage 2 and the Telegram bot are out of scope until Job A + Job B run unattended and correctly on real data for a sustained period.

---

## Phase 6 — Scheduling & operations

- Scheduler: **systemd timers** over cron (spec 1.17 left this open) — better logging via journald, dependency ordering, and no silent failure. Job A and Job B as separate units.
- **Job A: daily.** **Job B: monthly**, `OnCalendar=*-*-30` style (with the last-day-of-month clamp handled in code per D4b, since `OnCalendar` alone won't clamp Feb/short months).
- Stagger them; do not let Job B start while Job A is mid-run on the same files.
- Nightly: export manifest → upload to Mega (D2).
- Alert on: repeated `failed` rows, session expiry, disk pressure.

---

## Appendix — Phase 0 status

Run 2026-08-18 against a live Mega account and the provisioned droplet.

| Item | Status |
|---|---|
| Retention key: `captured_at` vs `discovered_at` | **Resolved by A3** — defaults to `discovered_at` (config.py, .env.example) |
| Node handle stability across a move | **Resolved by A2b** — confirmed stable |
| `mega-rm`/Rubbish semantics | **Resolved by A2** — informational, doesn't matter (D4a) |
| Parser strictness / fixture coverage | **Resolved by A6** — `mega_client.py` parser rewritten against real captured output, verified live |
| Session persistence across reboot | **Resolved by A1** — confirmed during infra setup |
| Settling window duration | **Still open — A5 skipped.** `SETTLING_MINUTES` config exists but isn't wired into discovery logic. Treat mid-upload races as an unmitigated risk until tested. |
| Per-format transcode branches (HEIC) | **Not applicable for this deployer** — no HEIC in their library. `transcoder.py` has no heif-convert branch; add one before deploying for a phone that produces HEIC. |

(Trash mechanism and delete cadence were never gated on Phase 0 — see D4a and D4b, decided independently of these tests.)

Each is a one-line change *if* resolved before Phase 2. Each is a refactor across multiple modules if discovered afterward — which is the entire argument for the gate.

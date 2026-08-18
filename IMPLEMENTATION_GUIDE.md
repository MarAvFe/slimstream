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
| A2 (either result) | Informational only (D4a). Logged for the record; does not change `mega_client`. |
| A2b false (handle changes) | Confirms D1: handle is an address, not identity. Re-resolve by path before every op. |
| A1 false | Add re-auth step + secure credential storage to `mega_client`. |
| A3 false | Retention keys off `discovered_at`, not `captured_at`. **Changes Job B's core query.** |
| A4 false | `transcoder` needs a per-format branch + `heif-convert` pre-step. |
| A5 false | Add settling window: skip files modified < N minutes ago. |
| A6 unstable | Harden parser; pin version; expand fixtures. |

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

### Config surface (all defaults from spec 1.9 / 1.8)
`MEGA_CAMERA_PATH`, `MEGA_KEEPERS_PATH`, `MEGA_TRASH_PATH`, `RETENTION_DAYS=30`, `RETENTION_RUN_DAY=30`, `VIDEO_HEIGHT=480`, `VIDEO_CRF=30`, `IMAGE_LONG_EDGE=1600`, `IMAGE_QUALITY=60`, `SCRATCH_DIR`, `MANIFEST_DB_PATH`, `DRY_RUN=true`, `SETTLING_MINUTES`.

`VIDEO_CRF` is ffmpeg's libx264 Constant Rate Factor (spec 1.9) — the quality/size dial, roughly logarithmic, lower = larger/better, ~18 "visually lossless," ~23 the libx264 default. 30 is an unvalidated starting guess for casual footage; Phase 4 is where it gets eyeballed against real clips and adjusted.

**`DRY_RUN` defaults to `true`.** Turning it off is a deliberate human act (spec 1.10).

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

## Phase 4 — Job A (compress) — still `DRY_RUN`

Implements spec 1.7. Job A **never deletes an original.**

Order per file: download → hash (D1 dedup check) → transcode → verify output → upload → **re-stat remote** → `verified` → clean scratch.

- Keepers excluded at discovery, before any work.
- Any exception: record `error`, increment `retry_count`, → `failed`. Never delete on a failure path.
- Idempotent: a crash mid-run leaves a resumable state; the next run continues.
- Backoff on `retry_count`; a row exceeding max retries is parked for human review, not retried forever.

**Human gate:** run against a small real folder with `DRY_RUN=true`, then live-but-Job-A-only. **Eyeball the transcoded output.** 480p/CRF 30 is a starting guess, not a validated setting — this is where you tune it, while originals are still safe.

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
1. `DRY_RUN=true` — read the intended-delete log in full.
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

## Appendix — Open items still owned by Phase 0

| Item | Resolved by |
|---|---|
| Retention key: `captured_at` vs `discovered_at` | A3 |
| Node handle stability across a move | A2b |
| Settling window duration | A5 |
| Per-format transcode branches | A4 |
| Parser strictness / fixture coverage | A6 |

(Trash mechanism and delete cadence are no longer open — see D4a and D4b.)

Each is a one-line change *if* resolved before Phase 2. Each is a refactor across multiple modules if discovered afterward — which is the entire argument for the gate.

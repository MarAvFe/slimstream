# slimstream: automated media compression pipeline

## 1.1 Problem statement (the actual North Star)

Personal Google One storage: 100GB plan, currently at 89%. Source of the bloat: a Pixel phone camera that only offers "high quality" or a still-too-large "medium," at 30/60fps video — far above what's needed for day-to-day capture (a kid playing, a moment worth remembering, not cinema). Roughly once a year there's a scenario that genuinely wants high quality; that should be an opt-in manual action, not the default for every file.

**North Star, stated as a test every design decision must pass:** *does this reduce recurring storage cost, or serve something that does?* Anything that doesn't pass this test — however interesting — is out of scope for v1.

A secondary, explicit goal: package this so **others can run it on their own setup** (own VM, own Mega account, own thresholds). This shapes the credentials and config design from day one, not as a later hardening pass.

## 1.2 Why not just use what exists

- **Google's own "Recover storage" / Storage Saver** — real, native, one click, converts existing library to Storage Saver quality. But it floors video at **1080p**, and the whole point of this project is a target *below* that (480p, sometimes 320p). Ruled out for the everyday tier.
- **Google Photos Library API** — as of a March 2025 scope change, apps can only list/search/retrieve media *they themselves uploaded* (no full-library read access). It does, however, still support **uploading new items** (`mediaItems.batchCreate`, up to 50/call) — relevant for the optional Stage 2 below, not for bulk-processing the existing library via API.
- **MEGA Manager** (github.com/szmania/mega_manager) — closest existing tool. Bidirectional Mega sync with image/video compression flags. Rejected as a base: state tracking is `.npy` files, not a real manifest; no verify-before-delete invariant; plaintext credentials in config; models the problem as "sync local↔remote" rather than "maintain two deliberate quality tiers with reversible deletion." Worth skimming for ffmpeg preset ideas only.
- **Consumer compress-and-reclaim apps** (Auto Photo Compress, MEGASQUEEZE, etc.) — solve local phone storage, not the headless-pipeline-with-audit-trail problem.

Conclusion: nothing found does the specific thing — a manifest-driven, idempotent, reversible pipeline between two clouds with a rolling retention window. That's the gap this fills.

## 1.3 Why Mega, and why Mega is the source of truth (not Google)

Google Photos' lack of bulk API access makes it a dead end for programmatic control. Mega, by contrast, offers real CLI/API control (MEGAcmd) and is already paid-for storage with better data ownership. The existing (working, 1+ year stable) setup: phone → Google Photos (native camera, unavoidable) → Mega camera-upload feature mirrors everything into Mega automatically. Monthly, originals get manually deleted from phone/Google Photos once safely in Mega.

**Critical design decision, arrived at after initial confusion:** early design discussion wrongly treated "what does Google's uploader do with a re-uploaded file" as a central unknown blocking the whole architecture. It doesn't matter, because **Google is not part of the write path.** Once a photo reaches Mega, nothing downstream in Stage 1 touches Google at all. Google is purely upstream (capture happens to land there because that's how phones work) and, optionally, downstream in Stage 2 (republishing compressed copies back for search/memories). This separation — Google in, Mega as the actual operating substrate — is what makes the architecture tractable. Don't reintroduce Google-behavior questions into Stage 1 design; they're irrelevant there by construction.

## 1.4 System overview

```
Phone (any, Google-Photos-enabled)
    │ capture
    ▼
Google Photos ──(Mega camera-upload reads it)──► MEGA  ◄── source of truth
                                                   │
                                  ┌────────────────┴──────────────────┐
                                  │        slimstream worker          │  (headless, cloud VM)
                                  │  Job A: compress new arrivals     │
                                  │  Job B: rolling 30-day delete     │
                                  │           ▲        │              │
                                  │           │        ▼              │
                                  │        MANIFEST (state store)     │
                                  └───────────┬────────┬──────────────┘
                                              │        │
                          (optional Stage 2)  │        │  read/control
                                              ▼        ▼
                                   Google Photos    Telegram bot
                                   (publish small     (reports,
                                    copies back)       pause, undo)
```

## 1.5 Design principles (in priority order)

1. **Mega is the source of truth.** Everything operates on Mega. Google Photos is upstream capture and, optionally, a downstream publish target — never a mid-pipeline dependency.
2. **The manifest is the product.** Not the transcode (trivial — ffmpeg/ImageMagick one-liners). The manifest is the load-bearing engineering: it's what makes the worker idempotent, resumable, and structurally incapable of deleting an original before its replacement is verified. It's also the fixed interface other deployers build against — everyone's Mega, VM, and thresholds differ, but the manifest schema doesn't have to.
3. **Worker before control surface.** The headless worker must fully function with zero UI. A Telegram bot (optional) is a thin read/query skin over manifest state afterward — it invents nothing.
4. **Reversibility by default.** No destructive action is irreversible within a recovery window. Delete routes to Mega's Rubbish bin, never straight deletion, **pending verification — see Assumption A2 below, this is unconfirmed and must be tested before relying on it.**
5. **Empirical claims get empirical gates, not model reasoning.** Any assumption about how Mega (or Google, in Stage 2) actually behaves gets a cheap real-world test before the design leans on it. A logically coherent design built on a false premise about vendor behavior still fails in production. See Section 1.11.

## 1.6 The manifest — schema

One record per original file. Key on **content hash** (or Mega node handle), not path — paths get relisted and can't be trusted as identity.

| Field | Type | Notes |
|---|---|---|
| `file_id` | string (PK) | content hash or Mega node handle |
| `original_path` | string | path in Mega camera-upload folder |
| `original_size` | int (bytes) | |
| `captured_at` | datetime | EXIF if present, else Mega timestamp (unverified — see A3) |
| `discovered_at` | datetime | when the worker first saw it |
| `media_type` | enum | `photo` \| `video` |
| `state` | enum | see state machine below |
| `compressed_path` | string? | path of small copy in Mega |
| `compressed_size` | int? | |
| `compressed_at` | datetime? | |
| `verified_at` | datetime? | small copy confirmed present & non-zero |
| `original_deleted_at` | datetime? | original moved to Rubbish |
| `published_at` | datetime? | (Stage 2 only) |
| `google_media_item_id` | string? | (Stage 2 only) |
| `error` | string? | last failure message |
| `retry_count` | int | for backoff |

### State machine

```
discovered ──(in keepers folder?)──► keeper           [terminal, excluded from everything]
    │
    ▼
compressing ──► compressed ──► uploaded ──► verified ──► original_deleted  [terminal]
    │                                          │
    │                                          └──(Stage 2, optional)──► published
    ▼
 failed  ──(retry w/ backoff)──► compressing
```

**Non-negotiable invariant:** a file reaches `original_deleted` **only** via `verified`. This one rule is what makes unattended deletion of irreplaceable memories safe. Enforce it in code, not just in intent — e.g. Job B's query should be structurally incapable of selecting a non-`verified` record.

## 1.7 Job A — compress new arrivals (daily)

1. Check pause flag (manifest); exit if set.
2. List Mega camera-upload folder. Any file not already in the manifest → insert as `discovered`. (Trust the manifest as the record of "handled," not folder-diffing alone.)
3. Anything under the **keepers** folder → `keeper`, terminal, untouched thereafter.
4. For each `discovered`/`failed` file:
   a. Download to VM scratch space.
   b. Transcode (see 1.9 for params). `compressing → compressed`.
   c. Upload small copy to its Mega location. `→ uploaded`.
   d. **Verify:** re-stat the uploaded copy — present, non-zero. `→ verified`.
   e. Clean up local scratch (both files).
5. Any error at any step: record `error`, increment `retry_count`, state `→ failed`. **Never delete on a failure path.**

Job A never touches the original's existence. That's exclusively Job B's job — keeping the two separate means a compression bug can never trigger a bad delete, and a delete bug can never block compression.

## 1.8 Job B — rolling retention delete (daily, independent of Job A)

1. Check pause flag; exit if set.
2. Query: `state == verified AND captured_at < now - RETENTION_DAYS` (default 30).
3. For each: `mega-rm` original **to Rubbish bin** (recoverable). `→ original_deleted`.
4. Emptying Rubbish is manual/out of scope for automation — it's the human's recovery window. Bot can optionally surface bin size.

"Every day is one day further in the past" = a moving 30-day window, not batch-monthly deletes.

## 1.9 Transcode parameters (defaults, all should be config)

```bash
# VIDEO → 480p target
ffmpeg -i IN -vf scale=-2:480 -c:v libx264 -crf 30 -preset slow -r 24 \
       -c:a aac -b:a 64k OUT.mp4
#   scale=-2:320 → 320p tier for even smaller
#   libx265 → ~30% smaller, slower, less universally playable — optional

# STILL → cap long edge, recompress
magick IN.jpg -resize 1600x1600\> -quality 60 OUT.jpg
#   \> only shrinks, never upscales
#   OUT.avif -quality 50 → notably smaller at similar perceived quality
```

Pixel-specific gotchas to handle, not yet empirically confirmed (see A4): HEIC stills likely need libheif-enabled ImageMagick or a `heif-convert` pre-step. Pixel "Motion Photos" are a JPEG with an embedded MP4 clip; transcoding will strip the motion component, which is acceptable (smaller, not a bug).

## 1.10 Reversibility & safety

- Verify-before-delete invariant (1.6) is the primary guard.
- Mega Rubbish bin is the recovery net — **pending confirmation, A2**.
- Keepers folder excluded at discovery, never entered into the compress/delete pipeline.
- **`--dry-run` is mandatory on first deployment**: every destructive action logs intent to the manifest without executing.
- Run Job A on a small real sample first, eyeball transcode quality, *then* enable Job B. The reason to go slow here is irreversibility of real memories, not any transfer/bandwidth constraint (see 1.13 — bandwidth was checked and is a non-issue).

## 1.11 Assumptions register — the pre-implementation gate

This is not optional and not a model's job. Each row needs a cheap real-world test *before* the worker is trusted with real files. A design that's internally coherent can still be wrong if it rests on a false claim about vendor behavior — no amount of reasoning about it substitutes for running the test.

| # | Assumption | Status | Cheapest test | If false |
|---|---|---|---|---|
| A1 | MEGAcmd session persists headless on the VM without re-login per run | likely, untested | Log in once, reboot VM, run `mega-ls` | Add a re-auth step; store refresh token securely |
| A2 | `mega-rm` moves to Rubbish (recoverable), not permanent delete | **unverified — highest priority, load-bearing for the whole safety model** | `mega-rm` a throwaway test file, check Rubbish bin contents | Insert an explicit "move to trash folder" step as the real recovery net instead of relying on `rm` semantics |
| A3 | Retention window can key off capture date; Mega/EXIF preserves it on upload | unverified | Upload a test photo, compare its Mega-reported timestamp to its EXIF capture date | Key retention off `discovered_at` (arrival date) instead of `captured_at` |
| A4 | Pixel HEIC stills / HEVC video / motion photos transcode cleanly with the toolchain above | unverified | Push one of each through the ffmpeg/ImageMagick commands by hand | Add libheif/heif-convert pre-step; document per-format branch in `transcoder` |
| A5 | A file mid-upload by the phone's camera-upload won't be picked up mid-write by the worker | unverified | Trigger an upload during a run; check for truncated/corrupt gets | Add a "settling window" — only process files older than N minutes since last modification |

**Do A1–A4 before writing `worker.py`. A2 specifically should be the very first thing tested — the whole reversibility argument in 1.10 depends on it.**

## 1.12 Failure-mode table

| Failure | Effect on state | Recovery |
|---|---|---|
| Worker crashes mid-transcode | stuck at `compressing` | next run resumes; idempotent by design |
| Upload succeeds, verify fails | never reaches `verified` | Job B can't touch it → original stays safe; retry |
| Mega session expired | all ops error, nothing corrupted | bot/log alert; re-auth |
| VM disk fills mid-run | transcode fails → `failed` | cleanup + retry with backoff |
| Keeper misclassified, deleted anyway | `original_deleted` | Rubbish bin recovery window (pending A2) |
| Same file re-listed twice | dedup on `file_id` hash | no double-processing |

## 1.13 Runner / hosting — decision and rationale

No always-on home machine currently — planning around a **cloud VM**, with home hardware as a possible future migration for cost.

**Mega transfer quota is not a constraint.** Mega's paid Lite plan here: 750GB storage, 12TB monthly transfer. Downloads are the metered direction on Mega; uploads may or may not count depending on source, but either way total transfer for even a full one-shot pass over the entire 750GB library is ~6% of the 12TB allowance. **Batch the first run for reversibility/review reasons (eyeball output, confirm delete logic on small sets first — see 1.10), not because of any quota risk.**

**VM bandwidth economics favor this architecture specifically**, and it's worth naming why explicitly since it's counter to the naive assumption: on providers like DigitalOcean, **inbound transfer to a droplet is free; only outbound is metered**, and even small droplets include hundreds of GB to 1TB of outbound per month. In this pipeline, the *large* files (originals) flow **into** the VM (free), and only the *small* compressed files flow **out** (metered, and tiny). The expensive-looking direction is actually the free one. A droplet can be spun up per batch and destroyed after (per-second billing on current DO pricing), costing pennies for the actual compute.

Bottleneck on a cheap VM is CPU (video transcode) and scratch disk during transcode, not bandwidth or Mega quota. Given short clips and small daily volume, a 1–2 vCPU box clears a batch unattended.

## 1.14 Stage 2 (optional, explicitly not MVP) — publish compressed copies back to Google Photos

**Motivation:** the small copies still lose Google Photos' search/faces/memories features once removed. Stage 2 re-adds them without re-adding the storage cost, by uploading the *compressed* version back.

**Confirmed constraints:**
1. This is **add, not edit** — no in-place replace exists on Google's side. Upload new small file via `mediaItems.batchCreate` (≤50/call), then manually delete the large original (already an existing monthly habit).
2. Files uploaded via the API are stored at the size sent — a 480p upload stays small in Google's quota accounting. Storage Saver's own compression doesn't apply to API uploads (irrelevant here — already sending small).
3. Since the March 2025 API scope change, an app can only later query media it uploaded itself via the API. This does **not** affect Google Photos' own in-app search/faces/memories, which index the whole library regardless of uploader — it only means the *worker* shouldn't try to use Google as a query surface. It shouldn't; the manifest already is.
4. No service-account auth — requires one-time human OAuth. Fine for personal use; for the open-source version, every deployer does their own OAuth setup.

**Architectural placement:** a 4th manifest state, `published`, plus a `google_publisher` module. Default: publish only after `verified` in Mega (Mega is the gate, Google mirrors the small copies) — simpler and safer than independent publish paths, no clear benefit to decoupling them.

## 1.15 Open-source packaging

**The manifest schema (1.6) is the fixed interface others build against.** Everything else — VM choice, thresholds, Mega account — is deployer-specific.

What a deployer brings: a VM, paid Mega storage + credentials, (optionally) a Google Cloud project + their own OAuth for Stage 2, and config (keepers path, quality targets, retention days, schedule, Telegram token).

**Secrets are a day-one design decision, not later hardening**, because this runs unattended on rented infrastructure a deployer may not fully control:
- Mega session + Telegram token: `.env`, never committed, locked-down file permissions; prefer MEGAcmd's persisted session over storing a raw password.
- Stage 2 OAuth refresh token: same discipline.
- README should state plainly what credentials the process holds and where, so a stranger can evaluate the exposure honestly before deploying.

## 1.16 Build order (MVP-scoped)

1. **Run the Assumptions Register tests (1.11), especially A2, before writing implementation code.**
2. `manifest` module: schema (1.6) + state machine + query interface. This is the interface everything else honors — get it right first.
3. `mega_client` (wraps MEGAcmd: list/get/put/rm-to-rubbish) and `transcoder` (ffmpeg/ImageMagick wrappers) — the two external edges.
4. `worker` Job A, run in `--dry-run` against a small real folder, validate output quality by hand.
5. `worker` Job B, `--dry-run` first, then live once Job A is trusted.
6. **MVP done here.** Stage 2 (`google_publisher`) and the Telegram `bot` are explicitly out of MVP scope — add only after Job A + Job B run unattended and correctly for real.

## 1.17 Open decisions for the implementer

- Manifest backend: local sqlite (simple, fast) vs. JSON control-file in Mega itself (survives VM loss, more portable). A reasonable middle path: sqlite + periodic backup copy to Mega.
- Scheduler: cron vs. systemd timer on the VM. (GitHub Actions was considered and rejected as the *worker* runtime — 6-hour job ceiling, no persistent disk for the manifest, and it would mean storing Mega session credentials in a CI runner. Could still work as a mere external trigger, not as the execution environment.)
- Settling-window duration for A5, once tested.
- Retention key (`captured_at` vs `discovered_at`) — depends on A3's result.

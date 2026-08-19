"""Job A (compress) and Job B (delete) — IMPLEMENTATION_GUIDE.md Phases 4-5.

Job A never touches an original's existence. Job B never transcodes.
Keeping them structurally separate means a compression bug can't trigger a
bad delete, and a delete bug can't block compression (spec 1.7).
"""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from slimstream.config import Config
from slimstream.manifest import (
    ManifestRecord,
    Manifest,
    STATE_COMPRESSED,
    STATE_COMPRESSING,
    STATE_DISCOVERED,
    STATE_FAILED,
    STATE_ORIGINAL_DELETED,
    STATE_KEEPER,
    STATE_SKIPPED_SMALL,
    STATE_UPLOADED,
    STATE_VERIFIED,
    compute_file_id,
)
from slimstream.mega_client import MegaClient
from slimstream.transcoder import (
    HEIC_EXTENSIONS,
    PHOTO_EXTENSIONS,
    TranscodeError,
    cleanup,
    transcode_image,
    transcode_video,
)
from slimstream.manifest import compute_content_hash

logger = logging.getLogger("slimstream.worker")

MAX_RETRIES = 5


@dataclass(frozen=True)
class JobAResult:
    discovered: int  # newly inserted this run (0 on a rerun over the same listing)
    reaped: int  # stranded 'compressing' records recovered for retry
    succeeded: int
    failed: int
    parked_for_retries: int
    still_pending: int

    @property
    def attempted(self) -> int:
        return self.succeeded + self.failed


@dataclass(frozen=True)
class JobBResult:
    moved_to_trash: int
    failed_to_move: int


class PausedError(Exception):
    """Raised (and caught by the caller) when the pause flag is set."""


def _check_not_paused(manifest: Manifest) -> None:
    if manifest.is_paused():
        raise PausedError("manifest pause flag is set; exiting without action")


def _media_type_for(path: str) -> str | None:
    suffix = Path(path).suffix.lower()
    if suffix in PHOTO_EXTENSIONS:
        return "photo"
    from slimstream.transcoder import VIDEO_EXTENSIONS

    if suffix in VIDEO_EXTENSIONS:
        return "video"
    return None


def _is_under_keepers(remote_path: str, keepers_path: str) -> bool:
    """Defense in depth, not the only thing protecting keepers today.

    mega.list() currently only lists the top level of a folder (no
    recursion), so files inside MEGA_KEEPERS_PATH are never even returned
    by discovery's listing call right now — this check never fires in
    practice on a flat Camera Uploads. It stays anyway: if list() is ever
    made recursive (e.g. to support nested album folders), this is what
    keeps keepers content from being silently swept into the compress
    pipeline instead of relying on "list() happens not to recurse" as the
    only safety net.
    """
    return remote_path.rstrip("/").startswith(keepers_path.rstrip("/") + "/") or remote_path.rstrip("/") == keepers_path.rstrip("/")


# --- Job A: discovery + compress ------------------------------------------


def run_discovery(manifest: Manifest, mega: MegaClient, config: Config) -> int:
    """List the camera-upload folder; insert anything unseen as discovered
    (or keeper). Trusts the manifest as the record of "handled," not
    folder-diffing alone (spec 1.7 step 2).

    Returns the count of newly-inserted records.
    """
    entries = mega.list(config.mega_camera_path)
    inserted = 0
    skipped_small = 0
    for entry in entries:
        if entry.is_dir:
            continue
        media_type = _media_type_for(entry.path)
        if media_type is None:
            logger.warning("skipping unrecognized file type: %s", entry.path)
            continue

        # Keeper wins over the size check: a file the human deliberately
        # set aside stays a keeper regardless of how small it is.
        if _is_under_keepers(entry.path, config.mega_keepers_path):
            initial_state = STATE_KEEPER
        elif entry.size < config.min_size_bytes:
            # Terminal, never `verified`. The original stays as the only
            # copy, so it must also never become deletable — compressing
            # it would produce a *larger* file, and deleting it with no
            # replacement would lose it outright.
            initial_state = STATE_SKIPPED_SMALL
            skipped_small += 1
        else:
            initial_state = STATE_DISCOVERED

        before = manifest.get(compute_file_id(entry.path, entry.size, entry.mtime_iso))
        manifest.upsert_discovered(
            original_path=entry.path,
            original_size=entry.size,
            captured_at=entry.mtime_iso,  # EXIF extraction happens at download time; see A3
            media_type=media_type,
            node_handle=entry.node_handle,
            initial_state=initial_state,
        )
        if before is None:
            inserted += 1
    if skipped_small:
        logger.info(
            "%d file(s) below MIN_SIZE_BYTES=%d left uncompressed (original kept as-is)",
            skipped_small,
            config.min_size_bytes,
        )
    return inserted


def _process_one(
    record: ManifestRecord,
    manifest: Manifest,
    mega: MegaClient,
    config: Config,
) -> bool:
    """Returns True if the file reached a good terminal state for this
    run, False if it landed in `failed`. The caller counts these
    separately so the run summary can't report a batch of failures as
    work done.
    """
    scratch = config.scratch_dir
    scratch.mkdir(parents=True, exist_ok=True)
    local_input: Path | None = None
    local_output: Path | None = None

    try:
        manifest.transition(record.file_id, STATE_COMPRESSING)

        # Manifest-loss recovery: if a compressed copy already exists at
        # this file's mirrored path, this file was already fully
        # processed at some point and the manifest just doesn't know it
        # (e.g. sqlite db lost/reset). Check by path alone, before
        # downloading anything — the whole point of mirroring compressed
        # output under a separate root (not mixed into MEGA_CAMERA_PATH)
        # is that this check needs no manifest state to be trustworthy.
        compressed_path = config.compressed_path_for(record.original_path)
        existing = mega.stat(compressed_path)
        if existing is not None and existing.size > 0:
            logger.info(
                "compressed copy already present at %s; skipping re-processing of %s",
                compressed_path,
                record.original_path,
            )
            manifest.transition(
                record.file_id,
                STATE_COMPRESSED,
                compressed_path=compressed_path,
                compressed_size=existing.size,
            )
            manifest.transition(record.file_id, STATE_UPLOADED, node_handle=existing.node_handle)
            manifest.transition(record.file_id, STATE_VERIFIED)
            return True

        local_input = mega.download(record.original_path, scratch)

        content_hash = compute_content_hash(local_input)
        dupe = manifest.find_by_content_hash(content_hash)
        if dupe is not None and dupe.file_id != record.file_id:
            # Same bytes already fully verified under a different file_id
            # (e.g. the original was renamed/moved in Mega — D1). Short-
            # circuit rather than re-transcode and re-upload.
            logger.info(
                "content_sha256 match with already-verified %s; short-circuiting %s",
                dupe.file_id,
                record.file_id,
            )
            manifest.transition(
                record.file_id,
                STATE_COMPRESSED,
                compressed_path=dupe.compressed_path,
                compressed_size=dupe.compressed_size,
                content_sha256=content_hash,
            )
            manifest.transition(record.file_id, STATE_UPLOADED, node_handle=dupe.node_handle)
            manifest.transition(record.file_id, STATE_VERIFIED, content_sha256=content_hash)
            return True

        suffix = local_input.suffix.lower()
        local_output = scratch / f"{local_input.stem}_compressed{'.mp4' if record.media_type == 'video' else '.jpg'}"

        if record.media_type == "video":
            if suffix in HEIC_EXTENSIONS:
                raise TranscodeError(
                    f"unexpected HEIC suffix on a video record: {local_input}"
                )
            result = transcode_video(
                local_input,
                local_output,
                height=config.video_height,
                crf=config.video_crf,
            )
        else:
            # A4: HEIC stills may need a heif-convert pre-step depending on
            # whether the deployed ImageMagick has libheif support. Not
            # branched here yet — pending A4 result.
            result = transcode_image(
                local_input,
                local_output,
                long_edge=config.image_long_edge,
                quality=config.image_quality,
            )

        # Compressed output lands in a mirrored tree under
        # MEGA_COMPRESSED_ROOT, never back into MEGA_CAMERA_PATH — this is
        # what makes "already compressed?" answerable by path alone (see
        # the pre-download check above) instead of depending on manifest
        # state that can be lost.
        remote_compressed_path = config.compressed_path_for(record.original_path)
        remote_dir = remote_compressed_path.rsplit("/", 1)[0]
        remote_name = remote_compressed_path.rsplit("/", 1)[-1]

        # mega.upload() names the remote file after the local basename, so
        # rename the local scratch file to the original's name before
        # upload — the mirrored tree keeps identical filenames to
        # MEGA_CAMERA_PATH, just under a different root.
        upload_source = result.output_path.with_name(remote_name)
        result.output_path.rename(upload_source)
        local_output = upload_source

        manifest.transition(
            record.file_id,
            STATE_COMPRESSED,
            compressed_path=remote_compressed_path,
            compressed_size=result.output_size,
            content_sha256=content_hash,
        )

        if config.dry_run_upload:
            logger.info(
                "[dry-run-upload] would upload %s to %s", upload_source, remote_compressed_path
            )
            cleanup(local_input, local_output)
            return True

        mega.mkdir_p(remote_dir)
        remote_path = mega.upload(upload_source, remote_dir)

        # Verify: re-stat the uploaded copy — present, non-zero (spec 1.7
        # step d) — before recording UPLOADED, so the node_handle stamped
        # on the transition is confirmed-real, not merely requested.
        stat = mega.stat(remote_path)
        if stat is None or stat.size == 0:
            raise TranscodeError(
                f"post-upload verify failed: {remote_path} missing or zero-size"
            )

        manifest.transition(record.file_id, STATE_UPLOADED, node_handle=stat.node_handle)
        manifest.transition(record.file_id, STATE_VERIFIED)
        return True

    except KeyboardInterrupt:
        # Ctrl+C is a BaseException, so `except Exception` below never saw
        # it: the record stayed parked in whatever in-flight state it had
        # reached, which is one way a stranded record gets created. Mark
        # it failed so it retries normally, then re-raise so the run still
        # stops immediately — an interrupt must remain an interrupt.
        logger.warning("interrupted while processing %s; marking for retry", record.file_id)
        manifest.transition(
            record.file_id, STATE_FAILED, error="interrupted by user (KeyboardInterrupt)"
        )
        raise
    except Exception as exc:  # noqa: BLE001 — any failure -> failed state, never a delete
        logger.exception("processing failed for %s", record.file_id)
        manifest.transition(record.file_id, STATE_FAILED, error=str(exc))
        return False
    finally:
        if local_input is not None:
            cleanup(local_input)
        if local_output is not None:
            cleanup(local_output)


def run_job_a(manifest: Manifest, mega: MegaClient, config: Config) -> JobAResult:
    """Discovery always runs to completion — the manifest must reflect
    the full remote listing on every run, never a partial view. Only the
    processing step (download/transcode/upload) is capped per invocation
    via MAX_BATCH_SIZE, so a library with thousands of backlogged files
    can be worked through gradually across daily runs instead of one run
    attempting everything at once.

    get_pending() orders oldest discovered_at first, so batching naturally
    processes the oldest backlog first and catches up towards the present
    over successive runs.
    """
    _check_not_paused(manifest)

    # Recover anything a previous run stranded mid-file (crash, OOM, or an
    # SSH disconnect killing a long interactive run — all observed). These
    # go back through the normal `failed` retry path rather than being
    # resumed in place, so retry_count still climbs and a file that
    # reliably kills the worker eventually gets parked instead of looping.
    reaped = 0
    for record in manifest.get_stranded_in_flight():
        logger.warning(
            "reaping %s stranded in %r by an earlier interrupted run",
            record.file_id,
            record.state,
        )
        manifest.transition(
            record.file_id,
            STATE_FAILED,
            error=f"interrupted mid-processing in {record.state!r}; reaped for retry",
        )
        reaped += 1

    discovered = run_discovery(manifest, mega, config)

    pending = manifest.get_pending()
    succeeded = 0
    failed = 0
    skipped_for_retries = 0

    for record in pending:
        if succeeded + failed >= config.max_batch_size:
            break
        if record.retry_count >= MAX_RETRIES:
            logger.warning(
                "parking %s after %d failed retries for human review",
                record.file_id,
                record.retry_count,
            )
            skipped_for_retries += 1
            continue
        if _process_one(record, manifest, mega, config):
            succeeded += 1
        else:
            failed += 1

    attempted = succeeded + failed
    remaining = max(len(pending) - attempted - skipped_for_retries, 0)
    logger.info(
        "job A: %d succeeded, %d failed, %d parked (max retries), %d still pending",
        succeeded,
        failed,
        skipped_for_retries,
        remaining,
    )
    return JobAResult(
        discovered=discovered,
        reaped=reaped,
        succeeded=succeeded,
        failed=failed,
        parked_for_retries=skipped_for_retries,
        still_pending=remaining,
    )


# --- Job B: retention delete -----------------------------------------------


def _clamped_run_day(run_day: int, today: datetime) -> int:
    last_day = calendar.monthrange(today.year, today.month)[1]
    return min(run_day, last_day)


def should_run_job_b_today(config: Config, today: datetime | None = None) -> bool:
    today = today or datetime.now(timezone.utc)
    return today.day == _clamped_run_day(config.retention_run_day, today)


def run_job_b(manifest: Manifest, mega: MegaClient, config: Config) -> JobBResult:
    """Monthly retention delete (D4b). Independent of Job A."""
    _check_not_paused(manifest)

    cutoff = datetime.now(timezone.utc) - timedelta(days=config.retention_days)
    cutoff_iso = cutoff.isoformat()

    deletable = manifest.get_deletable(cutoff_iso, retention_key=config.retention_key)

    moved = 0
    failed = 0

    for record in deletable:
        if config.dry_run_delete:
            logger.info(
                "[dry-run-delete] would move %s to %s", record.original_path, config.mega_trash_path
            )
            continue
        try:
            mega.move_to_trash(record.original_path, config.mega_trash_path)
            manifest.transition(record.file_id, STATE_ORIGINAL_DELETED)
            logger.info("moved to trash: %s", record.original_path)
            moved += 1
        except Exception:  # noqa: BLE001
            logger.exception("failed to move %s to trash; leaving as verified", record.file_id)
            # Deliberately do NOT transition to failed here: 'verified' has
            # no outgoing edge to 'failed' in the state machine (spec 1.6),
            # and a delete failure should just be retried next run, not
            # treated as a pipeline defect requiring backoff.
            failed += 1

    logger.info("job B: moved %d to trash, %d failed to move", moved, failed)
    return JobBResult(moved_to_trash=moved, failed_to_move=failed)

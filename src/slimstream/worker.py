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
    STATE_FAILED,
    STATE_ORIGINAL_DELETED,
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
    processed: int
    parked_for_retries: int
    still_pending: int


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
    for entry in entries:
        if entry.is_dir:
            continue
        media_type = _media_type_for(entry.path)
        if media_type is None:
            logger.warning("skipping unrecognized file type: %s", entry.path)
            continue

        is_keeper = _is_under_keepers(entry.path, config.mega_keepers_path)
        before = manifest.get(compute_file_id(entry.path, entry.size, entry.mtime_iso))
        manifest.upsert_discovered(
            original_path=entry.path,
            original_size=entry.size,
            captured_at=entry.mtime_iso,  # EXIF extraction happens at download time; see A3
            media_type=media_type,
            node_handle=entry.node_handle,
            is_keeper=is_keeper,
        )
        if before is None:
            inserted += 1
    return inserted


def _process_one(
    record: ManifestRecord,
    manifest: Manifest,
    mega: MegaClient,
    config: Config,
) -> None:
    scratch = config.scratch_dir
    scratch.mkdir(parents=True, exist_ok=True)
    local_input: Path | None = None
    local_output: Path | None = None

    try:
        manifest.transition(record.file_id, STATE_COMPRESSING)

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
            return

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

        manifest.transition(
            record.file_id,
            STATE_COMPRESSED,
            compressed_path=str(result.output_path),
            compressed_size=result.output_size,
            content_sha256=content_hash,
        )

        remote_dir = record.original_path.rsplit("/", 1)[0]
        if config.dry_run_upload:
            logger.info("[dry-run-upload] would upload %s to %s", result.output_path, remote_dir)
            cleanup(local_input, local_output)
            return

        remote_path = mega.upload(result.output_path, remote_dir)

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

    except Exception as exc:  # noqa: BLE001 — any failure -> failed state, never a delete
        logger.exception("processing failed for %s", record.file_id)
        manifest.transition(record.file_id, STATE_FAILED, error=str(exc))
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

    discovered = run_discovery(manifest, mega, config)

    pending = manifest.get_pending()
    processed = 0
    skipped_for_retries = 0

    for record in pending:
        if processed >= config.max_batch_size:
            break
        if record.retry_count >= MAX_RETRIES:
            logger.warning(
                "parking %s after %d failed retries for human review",
                record.file_id,
                record.retry_count,
            )
            skipped_for_retries += 1
            continue
        _process_one(record, manifest, mega, config)
        processed += 1

    remaining = max(len(pending) - processed - skipped_for_retries, 0)
    logger.info(
        "job A: processed %d, parked %d (max retries), %d still pending for next run",
        processed,
        skipped_for_retries,
        remaining,
    )
    return JobAResult(
        discovered=discovered,
        processed=processed,
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

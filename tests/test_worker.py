"""Worker tests using a fake MegaClient and a fake transcoder — no real
network/MEGAcmd/ffmpeg required. Focused on the invariants
IMPLEMENTATION_GUIDE.md calls out: Job A never deletes, Job B only ever
selects verified rows, failures never delete, dry-run never mutates remote
state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from slimstream.config import Config
from slimstream.manifest import Manifest, STATE_FAILED, STATE_VERIFIED
from slimstream.mega_client import RemoteEntry
import slimstream.worker as worker_mod
from slimstream.worker import (
    run_job_a,
    run_job_b,
    should_run_job_b_today,
    _clamped_run_day,
)


def make_config(tmp_path, **overrides) -> Config:
    defaults = dict(
        mega_camera_path="/Camera Uploads",
        mega_keepers_path="/Camera Uploads/keepers",
        mega_trash_path="/slimstream-trash",
        retention_days=30,
        retention_run_day=30,
        retention_key="captured_at",
        video_height=480,
        video_crf=30,
        image_long_edge=1600,
        image_quality=60,
        scratch_dir=tmp_path / "scratch",
        manifest_db_path=tmp_path / "manifest.db",
        settling_minutes=15,
        dry_run=True,
    )
    defaults.update(overrides)
    return Config(**defaults)


class FakeMegaClient:
    """In-memory stand-in for MegaClient. Tracks calls for assertions."""

    def __init__(self, entries: list[RemoteEntry], local_bytes: bytes = b"fake-media-bytes"):
        self._entries = entries
        self._local_bytes = local_bytes
        self.moved_to_trash: list[tuple[str, str]] = []
        self.uploaded: list[tuple[str, str]] = []
        self._uploaded_stats: dict[str, RemoteEntry] = {}

    def list(self, remote_path: str) -> list[RemoteEntry]:
        return list(self._entries)

    def download(self, remote_path: str, local_dir: Path) -> Path:
        local_dir.mkdir(parents=True, exist_ok=True)
        local_path = local_dir / remote_path.rsplit("/", 1)[-1]
        local_path.write_bytes(self._local_bytes)
        return local_path

    def upload(self, local_path: Path, remote_dir: str) -> str:
        remote_path = f"{remote_dir}/{local_path.name}"
        self.uploaded.append((str(local_path), remote_dir))
        self._uploaded_stats[remote_path] = RemoteEntry(
            path=remote_path,
            size=local_path.stat().st_size,
            is_dir=False,
            node_handle="H:UPLOADED1",
            mtime_iso="2026-01-01T00:00:00Z",
        )
        return remote_path

    def stat(self, remote_path: str) -> RemoteEntry | None:
        return self._uploaded_stats.get(remote_path)

    def move_to_trash(self, remote_path: str, trash_dir: str) -> str:
        self.moved_to_trash.append((remote_path, trash_dir))
        return f"{trash_dir}/{remote_path.rsplit('/', 1)[-1]}"


@pytest.fixture
def manifest(tmp_path):
    m = Manifest(tmp_path / "manifest.db")
    yield m
    m.close()


def _fake_transcode_video(input_path, output_path, **kwargs):
    from slimstream.transcoder import TranscodeResult

    output_path.write_bytes(b"fake-compressed-video")
    return TranscodeResult(output_path=output_path, output_size=output_path.stat().st_size)


def _fake_transcode_image(input_path, output_path, **kwargs):
    from slimstream.transcoder import TranscodeResult

    output_path.write_bytes(b"fake-compressed-image")
    return TranscodeResult(output_path=output_path, output_size=output_path.stat().st_size)


@pytest.fixture(autouse=True)
def patch_transcoders(monkeypatch):
    monkeypatch.setattr(worker_mod, "transcode_video", _fake_transcode_video)
    monkeypatch.setattr(worker_mod, "transcode_image", _fake_transcode_image)


# --- Job A -----------------------------------------------------------------


def test_job_a_discovers_and_marks_verified_when_not_dry_run(tmp_path, manifest):
    config = make_config(tmp_path, dry_run=False)
    entries = [
        RemoteEntry(
            path="/Camera Uploads/IMG_0001.jpg",
            size=2048,
            is_dir=False,
            node_handle="H:AAAAAAAA",
            mtime_iso="2026-01-01T00:00:00Z",
        )
    ]
    mega = FakeMegaClient(entries)

    run_job_a(manifest, mega, config)

    records = manifest.get_pending()
    assert records == []  # nothing left pending; should have reached verified

    all_rows = manifest._conn.execute("SELECT * FROM files").fetchall()
    assert len(all_rows) == 1
    assert all_rows[0]["state"] == STATE_VERIFIED
    assert len(mega.uploaded) == 1


def test_job_a_dry_run_never_uploads(tmp_path, manifest):
    config = make_config(tmp_path, dry_run=True)
    entries = [
        RemoteEntry(
            path="/Camera Uploads/IMG_0002.jpg",
            size=2048,
            is_dir=False,
            node_handle="H:BBBBBBBB",
            mtime_iso="2026-01-01T00:00:00Z",
        )
    ]
    mega = FakeMegaClient(entries)

    run_job_a(manifest, mega, config)

    assert mega.uploaded == []
    assert mega.moved_to_trash == []


def test_job_a_keeper_excluded_and_untouched(tmp_path, manifest):
    config = make_config(tmp_path, dry_run=False)
    entries = [
        RemoteEntry(
            path="/Camera Uploads/keepers/wedding.mp4",
            size=999999,
            is_dir=False,
            node_handle="H:CCCCCCCC",
            mtime_iso="2026-01-01T00:00:00Z",
        )
    ]
    mega = FakeMegaClient(entries)

    run_job_a(manifest, mega, config)

    assert mega.uploaded == []
    row = manifest._conn.execute("SELECT * FROM files").fetchone()
    assert row["state"] == "keeper"


def test_job_a_failure_never_deletes_original(tmp_path, manifest, monkeypatch):
    config = make_config(tmp_path, dry_run=False)
    entries = [
        RemoteEntry(
            path="/Camera Uploads/IMG_0003.jpg",
            size=2048,
            is_dir=False,
            node_handle="H:DDDDDDDD",
            mtime_iso="2026-01-01T00:00:00Z",
        )
    ]
    mega = FakeMegaClient(entries)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated transcode failure")

    monkeypatch.setattr(worker_mod, "transcode_image", _boom)

    run_job_a(manifest, mega, config)

    row = manifest._conn.execute("SELECT * FROM files").fetchone()
    assert row["state"] == STATE_FAILED
    assert row["retry_count"] == 1
    assert mega.moved_to_trash == []  # Job A never touches originals' existence


def test_job_a_respects_pause_flag(tmp_path, manifest):
    config = make_config(tmp_path, dry_run=False)
    manifest.set_paused(True)
    mega = FakeMegaClient([])

    from slimstream.worker import PausedError

    with pytest.raises(PausedError):
        run_job_a(manifest, mega, config)


def test_job_a_rerun_is_idempotent_no_duplicate_processing(tmp_path, manifest):
    config = make_config(tmp_path, dry_run=False)
    entries = [
        RemoteEntry(
            path="/Camera Uploads/IMG_0004.jpg",
            size=2048,
            is_dir=False,
            node_handle="H:EEEEEEEE",
            mtime_iso="2026-01-01T00:00:00Z",
        )
    ]
    mega = FakeMegaClient(entries)

    run_job_a(manifest, mega, config)
    run_job_a(manifest, mega, config)  # second run, same listing

    assert len(mega.uploaded) == 1  # not re-uploaded
    count = manifest._conn.execute("SELECT COUNT(*) c FROM files").fetchone()["c"]
    assert count == 1


# --- Job B -------------------------------------------------------------


def _verified_record(manifest, path, captured_at):
    rec = manifest.upsert_discovered(
        original_path=path,
        original_size=100,
        captured_at=captured_at,
        media_type="photo",
        node_handle="H:XXXXXXXX",
        is_keeper=False,
    )
    manifest.transition(rec.file_id, "compressing")
    manifest.transition(rec.file_id, "compressed", compressed_path="x", compressed_size=10)
    manifest.transition(rec.file_id, "uploaded", node_handle="H:YYYYYYYY")
    manifest.transition(rec.file_id, "verified")
    return rec


def test_job_b_moves_only_verified_past_retention_to_trash(tmp_path, manifest):
    config = make_config(tmp_path, dry_run=False, retention_days=30)
    old_iso = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    new_iso = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

    old_rec = _verified_record(manifest, "/Camera Uploads/old.jpg", old_iso)
    _verified_record(manifest, "/Camera Uploads/new.jpg", new_iso)

    mega = FakeMegaClient([])
    run_job_b(manifest, mega, config)

    assert mega.moved_to_trash == [("/Camera Uploads/old.jpg", "/slimstream-trash")]
    reloaded = manifest.get(old_rec.file_id)
    assert reloaded.state == "original_deleted"


def test_job_b_dry_run_never_moves_anything(tmp_path, manifest):
    config = make_config(tmp_path, dry_run=True, retention_days=30)
    old_iso = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    _verified_record(manifest, "/Camera Uploads/old.jpg", old_iso)

    mega = FakeMegaClient([])
    run_job_b(manifest, mega, config)

    assert mega.moved_to_trash == []
    # state must remain verified, not original_deleted, in dry-run
    row = manifest._conn.execute("SELECT * FROM files").fetchone()
    assert row["state"] == "verified"


def test_job_b_respects_pause_flag(tmp_path, manifest):
    config = make_config(tmp_path, dry_run=False)
    manifest.set_paused(True)
    mega = FakeMegaClient([])

    from slimstream.worker import PausedError

    with pytest.raises(PausedError):
        run_job_b(manifest, mega, config)


def test_job_b_never_touches_non_verified_even_if_old(tmp_path, manifest):
    """Discovered/failed/compressing files, however old, must never be
    selected by Job B — this is the manifest's structural guarantee
    (get_deletable), exercised here through the actual job entrypoint.
    """
    config = make_config(tmp_path, dry_run=False, retention_days=30)
    old_iso = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    manifest.upsert_discovered(
        original_path="/Camera Uploads/stuck.jpg",
        original_size=100,
        captured_at=old_iso,
        media_type="photo",
        node_handle="H:ZZZZZZZZ",
        is_keeper=False,
    )

    mega = FakeMegaClient([])
    run_job_b(manifest, mega, config)

    assert mega.moved_to_trash == []


# --- monthly scheduling (D4b) -------------------------------------------


def test_clamped_run_day_handles_february():
    feb_2026 = datetime(2026, 2, 15, tzinfo=timezone.utc)
    assert _clamped_run_day(30, feb_2026) == 28  # 2026 is not a leap year


def test_clamped_run_day_normal_month():
    jan_2026 = datetime(2026, 1, 15, tzinfo=timezone.utc)
    assert _clamped_run_day(30, jan_2026) == 30


def test_should_run_job_b_today_on_run_day(tmp_path):
    config = make_config(tmp_path, retention_run_day=30)
    assert should_run_job_b_today(config, datetime(2026, 1, 30, tzinfo=timezone.utc)) is True
    assert should_run_job_b_today(config, datetime(2026, 1, 15, tzinfo=timezone.utc)) is False


def test_should_run_job_b_today_clamps_short_month(tmp_path):
    config = make_config(tmp_path, retention_run_day=30)
    assert should_run_job_b_today(config, datetime(2026, 2, 28, tzinfo=timezone.utc)) is True

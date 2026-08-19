"""Tests for the manifest's safety properties (IMPLEMENTATION_GUIDE.md Phase 2
definition-of-done):

  (a) every illegal transition raises
  (b) get_deletable() returns nothing for non-verified rows, even when
      directly asked for them — this is the safety property of the whole
      system and must be tested before Job B exists
  (c) re-running discovery on the same listing produces zero duplicate rows
"""

from __future__ import annotations

import pytest

from slimstream.manifest import (
    IllegalTransitionError,
    Manifest,
    STATE_COMPRESSED,
    STATE_COMPRESSING,
    STATE_DISCOVERED,
    STATE_FAILED,
    STATE_KEEPER,
    STATE_ORIGINAL_DELETED,
    STATE_UPLOADED,
    STATE_VERIFIED,
    UnknownFileError,
    compute_file_id,
)


@pytest.fixture
def manifest(tmp_path):
    m = Manifest(tmp_path / "manifest.db")
    yield m
    m.close()


def _discover(manifest, path="Camera Uploads/IMG_0001.jpg", size=1000, captured="2026-01-01T00:00:00Z"):
    return manifest.upsert_discovered(
        original_path=path,
        original_size=size,
        captured_at=captured,
        media_type="photo",
        node_handle="H:AAAAAAAA",
        initial_state=STATE_DISCOVERED,
    )


# --- (a) illegal transitions raise -------------------------------------


def test_legal_pipeline_transition_succeeds(manifest):
    rec = _discover(manifest)
    rec = manifest.transition(rec.file_id, STATE_COMPRESSING)
    rec = manifest.transition(rec.file_id, STATE_COMPRESSED, compressed_path="x", compressed_size=10)
    rec = manifest.transition(rec.file_id, STATE_UPLOADED, node_handle="H:BBBBBBBB")
    rec = manifest.transition(rec.file_id, STATE_VERIFIED)
    assert rec.state == STATE_VERIFIED
    assert rec.verified_at is not None


def test_cannot_skip_straight_to_verified(manifest):
    rec = _discover(manifest)
    with pytest.raises(IllegalTransitionError):
        manifest.transition(rec.file_id, STATE_VERIFIED)


def test_cannot_delete_from_discovered(manifest):
    rec = _discover(manifest)
    with pytest.raises(IllegalTransitionError):
        manifest.transition(rec.file_id, STATE_ORIGINAL_DELETED)


def test_cannot_delete_from_uploaded_not_yet_verified(manifest):
    rec = _discover(manifest)
    rec = manifest.transition(rec.file_id, STATE_COMPRESSING)
    rec = manifest.transition(rec.file_id, STATE_COMPRESSED, compressed_path="x", compressed_size=10)
    rec = manifest.transition(rec.file_id, STATE_UPLOADED, node_handle="H:BBBBBBBB")
    with pytest.raises(IllegalTransitionError):
        manifest.transition(rec.file_id, STATE_ORIGINAL_DELETED)


def test_original_deleted_is_terminal(manifest):
    rec = _discover(manifest)
    rec = manifest.transition(rec.file_id, STATE_COMPRESSING)
    rec = manifest.transition(rec.file_id, STATE_COMPRESSED, compressed_path="x", compressed_size=10)
    rec = manifest.transition(rec.file_id, STATE_UPLOADED, node_handle="H:BBBBBBBB")
    rec = manifest.transition(rec.file_id, STATE_VERIFIED)
    rec = manifest.transition(rec.file_id, STATE_ORIGINAL_DELETED)
    with pytest.raises(IllegalTransitionError):
        manifest.transition(rec.file_id, STATE_COMPRESSING)


def test_unknown_file_id_raises(manifest):
    with pytest.raises(UnknownFileError):
        manifest.transition("nonexistent", STATE_COMPRESSING)


def test_failed_retry_increments_count(manifest):
    rec = _discover(manifest)
    rec = manifest.transition(rec.file_id, STATE_COMPRESSING)
    rec = manifest.transition(rec.file_id, STATE_FAILED, error="disk full")
    assert rec.retry_count == 1
    assert rec.error == "disk full"
    rec = manifest.transition(rec.file_id, STATE_COMPRESSING)
    assert rec.error is None
    rec = manifest.transition(rec.file_id, STATE_FAILED, error="disk full again")
    assert rec.retry_count == 2


# --- (b) get_deletable structural safety --------------------------------


@pytest.mark.parametrize(
    "terminal_state_reached",
    [STATE_DISCOVERED, STATE_COMPRESSING, STATE_COMPRESSED, STATE_UPLOADED],
)
def test_get_deletable_excludes_non_verified_states(manifest, terminal_state_reached):
    rec = _discover(manifest, captured="2000-01-01T00:00:00Z")
    if terminal_state_reached != STATE_DISCOVERED:
        manifest.transition(rec.file_id, STATE_COMPRESSING)
    if terminal_state_reached in (STATE_COMPRESSED, STATE_UPLOADED):
        manifest.transition(rec.file_id, STATE_COMPRESSED, compressed_path="x", compressed_size=10)
    if terminal_state_reached == STATE_UPLOADED:
        manifest.transition(rec.file_id, STATE_UPLOADED, node_handle="H:BBBBBBBB")

    deletable = manifest.get_deletable("2099-01-01T00:00:00Z")
    assert deletable == []


def test_get_deletable_only_returns_verified_past_cutoff(manifest):
    old = _discover(manifest, path="a.jpg", captured="2000-01-01T00:00:00Z")
    new = _discover(manifest, path="b.jpg", captured="2099-01-01T00:00:00Z")

    for rec in (old, new):
        manifest.transition(rec.file_id, STATE_COMPRESSING)
        manifest.transition(rec.file_id, STATE_COMPRESSED, compressed_path="x", compressed_size=10)
        manifest.transition(rec.file_id, STATE_UPLOADED, node_handle="H:BBBBBBBB")
        manifest.transition(rec.file_id, STATE_VERIFIED)

    deletable = manifest.get_deletable("2050-01-01T00:00:00Z")
    assert [r.file_id for r in deletable] == [old.file_id]


def test_get_deletable_rejects_invalid_retention_key(manifest):
    with pytest.raises(ValueError):
        manifest.get_deletable("2099-01-01T00:00:00Z", retention_key="state; DROP TABLE files;--")


def test_get_deletable_never_returns_original_deleted(manifest):
    rec = _discover(manifest, captured="2000-01-01T00:00:00Z")
    manifest.transition(rec.file_id, STATE_COMPRESSING)
    manifest.transition(rec.file_id, STATE_COMPRESSED, compressed_path="x", compressed_size=10)
    manifest.transition(rec.file_id, STATE_UPLOADED, node_handle="H:BBBBBBBB")
    manifest.transition(rec.file_id, STATE_VERIFIED)
    manifest.transition(rec.file_id, STATE_ORIGINAL_DELETED)

    deletable = manifest.get_deletable("2099-01-01T00:00:00Z")
    assert deletable == []


# --- (c) idempotent discovery --------------------------------------------


def test_rediscovery_of_same_file_is_noop(manifest):
    rec1 = _discover(manifest)
    rec2 = _discover(manifest)
    assert rec1.file_id == rec2.file_id

    count = manifest._conn.execute("SELECT COUNT(*) c FROM files").fetchone()["c"]
    assert count == 1


def test_rediscovery_does_not_reset_progressed_state(manifest):
    rec = _discover(manifest)
    manifest.transition(rec.file_id, STATE_COMPRESSING)
    _discover(manifest)  # same path/size/captured_at -> same file_id
    reloaded = manifest.get(rec.file_id)
    assert reloaded.state == STATE_COMPRESSING


def test_different_path_yields_different_file_id(manifest):
    a = _discover(manifest, path="a.jpg")
    b = _discover(manifest, path="b.jpg")
    assert a.file_id != b.file_id


def test_compute_file_id_deterministic():
    id1 = compute_file_id("a.jpg", 100, "2026-01-01")
    id2 = compute_file_id("a.jpg", 100, "2026-01-01")
    assert id1 == id2


# --- keeper handling ------------------------------------------------------


def test_keeper_is_terminal_and_excluded_from_pending(manifest):
    rec = manifest.upsert_discovered(
        original_path="keepers/wedding.mp4",
        original_size=5000,
        captured_at="2026-01-01T00:00:00Z",
        media_type="video",
        node_handle="H:CCCCCCCC",
        initial_state=STATE_KEEPER,
    )
    assert rec.state == "keeper"
    assert rec not in manifest.get_pending()
    with pytest.raises(IllegalTransitionError):
        manifest.transition(rec.file_id, STATE_COMPRESSING)


# --- pause flag -------------------------------------------------------


def test_pause_flag_roundtrip(manifest):
    assert manifest.is_paused() is False
    manifest.set_paused(True)
    assert manifest.is_paused() is True
    manifest.set_paused(False)
    assert manifest.is_paused() is False


# --- backup / restore (D2) ------------------------------------------------


def _populate(manifest, n=3):
    ids = []
    for i in range(n):
        rec = manifest.upsert_discovered(
            original_path=f"/Camera Uploads/IMG_{i:04d}.jpg",
            original_size=1000 + i,
            captured_at=f"2026-01-{i + 1:02d}",
            media_type="photo",
            node_handle=f"H:{i:08d}",
            initial_state=STATE_DISCOVERED,
        )
        ids.append(rec.file_id)
    return ids


def test_export_then_import_roundtrips_every_field(tmp_path, manifest):
    from slimstream.manifest import export_manifest, import_manifest

    ids = _populate(manifest, 3)
    # advance one record so non-default columns are exercised too
    manifest.transition(ids[0], STATE_COMPRESSING)
    manifest.transition(ids[0], STATE_COMPRESSED, compressed_path="/x/a.jpg", compressed_size=42)
    manifest.set_meta("megacmd_version", "2.5.2.1")

    out = tmp_path / "export.json"
    assert export_manifest(manifest, out) == 3

    restored = Manifest(tmp_path / "restored.db")
    try:
        assert import_manifest(restored, out) == 3
        for file_id in ids:
            assert restored.get(file_id) == manifest.get(file_id)
        assert restored.get_meta("megacmd_version") == "2.5.2.1"
    finally:
        restored.close()


def test_import_refuses_to_clobber_a_populated_manifest(tmp_path, manifest):
    from slimstream.manifest import export_manifest, import_manifest

    _populate(manifest, 2)
    out = tmp_path / "export.json"
    export_manifest(manifest, out)

    other = Manifest(tmp_path / "other.db")
    try:
        _populate(other, 1)
        # Silently merging could resurrect records for files already moved
        # to trash, or overwrite newer state with older.
        with pytest.raises(ValueError, match="already holds"):
            import_manifest(other, out)
        assert import_manifest(other, out, force=True) == 2
    finally:
        other.close()


def test_import_rejects_unknown_export_format_version(tmp_path, manifest):
    import json

    from slimstream.manifest import import_manifest

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"export_format_version": 999, "files": [], "meta": {}}))

    with pytest.raises(ValueError, match="unsupported export_format_version"):
        import_manifest(manifest, bad)


def test_export_is_atomic_leaving_no_partial_file(tmp_path, manifest):
    """An interrupted export must not replace a good backup with a
    truncated one, so the write goes to a temp file and is renamed.
    """
    from slimstream.manifest import export_manifest

    _populate(manifest, 2)
    out = tmp_path / "export.json"
    export_manifest(manifest, out)

    assert out.exists()
    assert not list(tmp_path.glob("*.partial"))

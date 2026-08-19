"""The manifest: schema, state machine, and query interface.

This is the load-bearing module (IMPLEMENTATION_GUIDE.md Phase 2). Every
other module honors what's defined here. Two properties are non-negotiable:

1. State transitions go through one guarded function (`transition`). Illegal
   transitions raise — there is no other way to change `state`.
2. `get_deletable` is structurally incapable of returning a non-`verified`
   row. This is the safety property the entire system depends on.

Identity (see IMPLEMENTATION_GUIDE.md D1): `file_id` is a synthetic key
derived from listing metadata alone (path + size + captured_at), assignable
at discovery time before any download happens. `node_handle` is Mega's
H:XXXXXXXX — treated as a mutable address, not identity, since it's
undocumented whether it survives a move. `content_sha256` is the true
content hash, known only after download, used for dedup.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1

# --- State machine -----------------------------------------------------

STATE_DISCOVERED = "discovered"
STATE_KEEPER = "keeper"
STATE_COMPRESSING = "compressing"
STATE_COMPRESSED = "compressed"
STATE_UPLOADED = "uploaded"
STATE_VERIFIED = "verified"
STATE_ORIGINAL_DELETED = "original_deleted"
STATE_SKIPPED_SMALL = "skipped_small"
STATE_PUBLISHED = "published"
STATE_FAILED = "failed"

ALL_STATES = {
    STATE_DISCOVERED,
    STATE_KEEPER,
    STATE_COMPRESSING,
    STATE_COMPRESSED,
    STATE_UPLOADED,
    STATE_VERIFIED,
    STATE_ORIGINAL_DELETED,
    STATE_SKIPPED_SMALL,
    STATE_PUBLISHED,
    STATE_FAILED,
}

TERMINAL_STATES = {STATE_KEEPER, STATE_ORIGINAL_DELETED, STATE_SKIPPED_SMALL}

# States a record may be created in. Everything else must be reached by a
# guarded transition.
INITIAL_STATES = {STATE_DISCOVERED, STATE_KEEPER, STATE_SKIPPED_SMALL}

# Legal transitions: from_state -> set of allowed to_states.
# This is the single source of truth for the state machine (spec 1.6).
_LEGAL_TRANSITIONS: dict[str, set[str]] = {
    STATE_DISCOVERED: {STATE_KEEPER, STATE_COMPRESSING, STATE_FAILED},
    STATE_COMPRESSING: {STATE_COMPRESSED, STATE_FAILED},
    STATE_COMPRESSED: {STATE_UPLOADED, STATE_FAILED},
    STATE_UPLOADED: {STATE_VERIFIED, STATE_FAILED},
    STATE_VERIFIED: {STATE_ORIGINAL_DELETED, STATE_PUBLISHED},
    STATE_ORIGINAL_DELETED: set(),
    STATE_PUBLISHED: set(),
    STATE_KEEPER: set(),
    # Terminal, and deliberately so. A file too small to benefit from
    # compression keeps its original as the only copy, so it must never
    # reach `verified` — Job B deletes verified originals, and with no
    # compressed replacement that would be outright data loss.
    STATE_SKIPPED_SMALL: set(),
    # failed -> compressing only (retry re-enters the pipeline at the
    # compress step; discovery-level failures also retry from compressing
    # since discovery itself doesn't do failable work beyond the insert).
    STATE_FAILED: {STATE_COMPRESSING},
}


class IllegalTransitionError(ValueError):
    """Raised when a state transition isn't allowed by the state machine."""


class UnknownFileError(KeyError):
    """Raised when an operation targets a file_id not in the manifest."""


def compute_file_id(original_path: str, original_size: int, captured_at: str) -> str:
    """Synthetic primary key, computable from listing metadata alone (D1).

    Deliberately NOT a content hash — discovery happens before download,
    so content isn't available yet. Deliberately NOT the Mega node handle —
    handle survival across a move to trash is unverified (see A2b).
    """
    basis = f"{original_path}\x00{original_size}\x00{captured_at}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def compute_content_hash(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Stream a local file through sha256. Never loads the whole file into memory."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ManifestRecord:
    file_id: str
    original_path: str
    original_size: int
    captured_at: str | None
    discovered_at: str
    media_type: str
    state: str
    node_handle: str | None = None
    content_sha256: str | None = None
    compressed_path: str | None = None
    compressed_size: int | None = None
    compressed_at: str | None = None
    verified_at: str | None = None
    original_deleted_at: str | None = None
    published_at: str | None = None
    google_media_item_id: str | None = None
    error: str | None = None
    retry_count: int = 0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ManifestRecord":
        return cls(**{k: row[k] for k in row.keys()})


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS files (
    file_id             TEXT PRIMARY KEY,
    original_path       TEXT NOT NULL,
    original_size       INTEGER NOT NULL,
    captured_at         TEXT,
    discovered_at       TEXT NOT NULL,
    media_type          TEXT NOT NULL CHECK (media_type IN ('photo', 'video')),
    state               TEXT NOT NULL,
    node_handle         TEXT,
    content_sha256      TEXT,
    compressed_path     TEXT,
    compressed_size     INTEGER,
    compressed_at       TEXT,
    verified_at         TEXT,
    original_deleted_at TEXT,
    published_at        TEXT,
    google_media_item_id TEXT,
    error               TEXT,
    retry_count         INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_files_state ON files(state);
CREATE INDEX IF NOT EXISTS idx_files_content_sha256 ON files(content_sha256);
CREATE INDEX IF NOT EXISTS idx_files_captured_at ON files(captured_at);
CREATE INDEX IF NOT EXISTS idx_files_discovered_at ON files(discovered_at);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Manifest:
    """Owns the sqlite connection and all state transitions/queries.

    Opens with journal_mode=WAL (D2) so Job A and Job B, running as separate
    processes, can overlap safely.
    """

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        # executescript() implicitly commits any open transaction, so it
        # can't run inside our explicit BEGIN IMMEDIATE/COMMIT wrapper.
        self._conn.executescript(_SCHEMA_SQL)
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Manifest":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- meta / pause flag ------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row is not None else default

    def is_paused(self) -> bool:
        return self.get_meta("paused", "false") == "true"

    def set_paused(self, paused: bool) -> None:
        self.set_meta("paused", "true" if paused else "false")

    # --- discovery ----------------------------------------------------

    def upsert_discovered(
        self,
        *,
        original_path: str,
        original_size: int,
        captured_at: str | None,
        media_type: str,
        node_handle: str | None,
        initial_state: str = STATE_DISCOVERED,
    ) -> ManifestRecord:
        """Insert a newly-listed file in one of the INITIAL_STATES.

        Takes the state explicitly rather than a set of booleans: the
        caller already knows whether this is a keeper, a too-small file,
        or ordinary work, and encoding that as flags here would mean
        re-deriving precedence rules inside the manifest.

        A no-op if file_id already exists (re-running discovery on the same
        listing must not duplicate or reset rows) — this is what makes
        discovery idempotent (guide Phase 2 definition-of-done, item c).
        """
        if initial_state not in INITIAL_STATES:
            raise IllegalTransitionError(
                f"{initial_state!r} is not a valid initial state {sorted(INITIAL_STATES)}"
            )

        file_id = compute_file_id(original_path, original_size, captured_at or "")
        existing = self.get(file_id)
        if existing is not None:
            return existing

        state = initial_state
        now = _utcnow_iso()
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO files (
                    file_id, original_path, original_size, captured_at,
                    discovered_at, media_type, state, node_handle, retry_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    file_id,
                    original_path,
                    original_size,
                    captured_at,
                    now,
                    media_type,
                    state,
                    node_handle,
                ),
            )
        return self.get(file_id)  # type: ignore[return-value]

    # --- reads --------------------------------------------------------

    def get(self, file_id: str) -> ManifestRecord | None:
        row = self._conn.execute(
            "SELECT * FROM files WHERE file_id = ?", (file_id,)
        ).fetchone()
        return ManifestRecord.from_row(row) if row is not None else None

    def find_by_content_hash(self, content_sha256: str) -> ManifestRecord | None:
        row = self._conn.execute(
            "SELECT * FROM files WHERE content_sha256 = ? AND state = ? "
            "ORDER BY verified_at DESC LIMIT 1",
            (content_sha256, STATE_VERIFIED),
        ).fetchone()
        return ManifestRecord.from_row(row) if row is not None else None

    def get_pending(self) -> list[ManifestRecord]:
        """Files Job A should work on: discovered or failed (for retry).

        Deliberately excludes `compressing`: a record in that state is
        either being worked on right now, or was stranded by a worker
        that died mid-file. Stranded ones are recovered by
        `get_stranded_compressing()` + an explicit reap at the start of
        the run, not by being silently re-selected here.
        """
        rows = self._conn.execute(
            "SELECT * FROM files WHERE state IN (?, ?) ORDER BY discovered_at ASC",
            (STATE_DISCOVERED, STATE_FAILED),
        ).fetchall()
        return [ManifestRecord.from_row(r) for r in rows]

    def get_stranded_in_flight(self) -> list[ManifestRecord]:
        """Records left mid-pipeline by a worker that died before reaching
        a terminal state.

        Spec 1.12 promises "next run resumes; idempotent by design", but
        nothing delivered that: `get_pending()` selects only `discovered`
        and `failed`, so a hard interruption stranded the in-flight record
        permanently. It would never be compressed and — since Job B only
        ever touches `verified` — never deleted either: silently dropped
        out of the pipeline with no error recorded anywhere.

        All three non-terminal working states are strandable, not just
        `compressing`. `_process_one` passes through
        compressing → compressed → uploaded → verified, and a process
        killed between any two of those steps leaves the record parked in
        the earlier one:

        - `compressing` — died during download or transcode
        - `compressed`  — transcode finished, died before/during upload
        - `uploaded`    — upload finished, died before the verify stamp

        Reaping `uploaded` is safe and in fact self-healing: the retry's
        pre-download check finds the already-present compressed copy at
        the mirrored path and short-circuits straight back to `verified`
        without redoing any work (D5).

        Assumes Job A does not run concurrently with itself, which is
        already required (overlapping runs would double-process the same
        files) and holds for both the systemd `Type=oneshot` unit and
        manual invocation.
        """
        rows = self._conn.execute(
            "SELECT * FROM files WHERE state IN (?, ?, ?) ORDER BY discovered_at ASC",
            (STATE_COMPRESSING, STATE_COMPRESSED, STATE_UPLOADED),
        ).fetchall()
        return [ManifestRecord.from_row(r) for r in rows]

    def get_deletable(self, cutoff_iso: str, retention_key: str = "captured_at") -> list[ManifestRecord]:
        """Files eligible for Job B to move to trash.

        Structurally restricted to state == 'verified': the WHERE clause
        hardcodes the state literal, it is not a parameter, so there is no
        call shape that can smuggle a non-verified row through this query.
        This is the safety property in IMPLEMENTATION_GUIDE.md Phase 2.
        """
        if retention_key not in ("captured_at", "discovered_at"):
            raise ValueError(f"invalid retention_key: {retention_key!r}")

        # retention_key is validated against a closed set above, never
        # interpolated from unchecked input, so this is safe to format in.
        query = (
            f"SELECT * FROM files WHERE state = 'verified' "  # noqa: S608
            f"AND {retention_key} IS NOT NULL AND {retention_key} < ? "
            f"ORDER BY {retention_key} ASC"
        )
        rows = self._conn.execute(query, (cutoff_iso,)).fetchall()
        return [ManifestRecord.from_row(r) for r in rows]

    def get_failed_over_threshold(self, max_retries: int) -> list[ManifestRecord]:
        rows = self._conn.execute(
            "SELECT * FROM files WHERE state = ? AND retry_count >= ? "
            "ORDER BY discovered_at ASC",
            (STATE_FAILED, max_retries),
        ).fetchall()
        return [ManifestRecord.from_row(r) for r in rows]

    # --- transitions ----------------------------------------------------

    def transition(
        self,
        file_id: str,
        to_state: str,
        *,
        node_handle: str | None = None,
        content_sha256: str | None = None,
        compressed_path: str | None = None,
        compressed_size: int | None = None,
        error: str | None = None,
    ) -> ManifestRecord:
        """The only sanctioned way to change a record's state.

        Raises IllegalTransitionError for any transition not in
        _LEGAL_TRANSITIONS. Raises UnknownFileError if file_id doesn't
        exist. Stamps the appropriate timestamp column for the target
        state and commits everything in one transaction.
        """
        if to_state not in ALL_STATES:
            raise IllegalTransitionError(f"unknown target state: {to_state!r}")

        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM files WHERE file_id = ?", (file_id,)
            ).fetchone()
            if row is None:
                raise UnknownFileError(file_id)

            current = ManifestRecord.from_row(row)
            allowed = _LEGAL_TRANSITIONS.get(current.state, set())
            if to_state not in allowed:
                raise IllegalTransitionError(
                    f"{file_id}: illegal transition {current.state!r} -> {to_state!r} "
                    f"(allowed: {sorted(allowed)})"
                )

            now = _utcnow_iso()
            set_clauses = ["state = ?"]
            params: list[object] = [to_state]

            if to_state == STATE_FAILED:
                set_clauses += ["error = ?", "retry_count = retry_count + 1"]
                params.append(error or "unknown error")
            elif current.state == STATE_FAILED and to_state == STATE_COMPRESSING:
                set_clauses.append("error = NULL")

            if to_state == STATE_COMPRESSED:
                set_clauses += ["compressed_path = ?", "compressed_size = ?", "compressed_at = ?"]
                params += [compressed_path, compressed_size, now]
            if to_state == STATE_UPLOADED and node_handle is not None:
                set_clauses.append("node_handle = ?")
                params.append(node_handle)
            if content_sha256 is not None:
                set_clauses.append("content_sha256 = ?")
                params.append(content_sha256)
            if to_state == STATE_VERIFIED:
                set_clauses.append("verified_at = ?")
                params.append(now)
            if to_state == STATE_ORIGINAL_DELETED:
                set_clauses.append("original_deleted_at = ?")
                params.append(now)
            if to_state == STATE_PUBLISHED:
                set_clauses.append("published_at = ?")
                params.append(now)

            params.append(file_id)
            conn.execute(
                f"UPDATE files SET {', '.join(set_clauses)} WHERE file_id = ?",  # noqa: S608
                params,
            )

            row = conn.execute(
                "SELECT * FROM files WHERE file_id = ?", (file_id,)
            ).fetchone()
            return ManifestRecord.from_row(row)


# --- backup / restore (D2) ------------------------------------------------
#
# The manifest is the only record of what has been processed, what failed,
# and — once Job B runs — what was moved to trash. It lives on one VM's
# disk, so losing that VM loses all of it. D5's mirrored-path check makes
# loss *survivable* (rediscovery re-stats and short-circuits), but that is
# a slow rebuild that still discards retry history and the delete audit
# trail permanently.
#
# Export is JSON rather than a copy of the .db so it stays readable and
# restorable across schema changes, and import exists because an export
# nobody can restore from is not a backup.

EXPORT_FORMAT_VERSION = 1


def export_manifest(manifest: "Manifest", out_path: Path) -> int:
    """Dump the whole manifest to JSON. Returns the record count."""
    out_path.parent.mkdir(parents=True, exist_ok=True)

    files = [
        dict(row)
        for row in manifest._conn.execute("SELECT * FROM files ORDER BY discovered_at")
    ]
    meta = {
        row["key"]: row["value"]
        for row in manifest._conn.execute("SELECT key, value FROM meta")
    }

    payload = {
        "export_format_version": EXPORT_FORMAT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "exported_at": _utcnow_iso(),
        "record_count": len(files),
        "meta": meta,
        "files": files,
    }

    # Write to a temp file and rename, so an interrupted export can never
    # leave a truncated file where a valid backup used to be.
    tmp_path = out_path.with_suffix(out_path.suffix + ".partial")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    tmp_path.replace(out_path)

    return len(files)


def import_manifest(manifest: "Manifest", in_path: Path, *, force: bool = False) -> int:
    """Restore a manifest from a JSON export. Returns the record count.

    Refuses to touch a manifest that already holds records unless `force`
    is set: silently merging a backup into live state could resurrect
    records for files already moved to trash, or overwrite newer state
    with older.
    """
    with open(in_path, encoding="utf-8") as f:
        payload = json.load(f)

    version = payload.get("export_format_version")
    if version != EXPORT_FORMAT_VERSION:
        raise ValueError(
            f"unsupported export_format_version {version!r} "
            f"(this build reads {EXPORT_FORMAT_VERSION})"
        )

    existing = manifest._conn.execute("SELECT COUNT(*) c FROM files").fetchone()["c"]
    if existing and not force:
        raise ValueError(
            f"refusing to import into a manifest that already holds {existing} "
            f"records; pass force=True to replace them"
        )

    files = payload["files"]
    with manifest._transaction() as conn:
        if existing:
            conn.execute("DELETE FROM files")
        for row in files:
            columns = ", ".join(row.keys())
            placeholders = ", ".join("?" for _ in row)
            conn.execute(
                f"INSERT INTO files ({columns}) VALUES ({placeholders})",  # noqa: S608
                list(row.values()),
            )
        for key, value in payload.get("meta", {}).items():
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    return len(files)

"""Wraps MEGAcmd's scriptable `mega-*` commands.

Two design decisions from IMPLEMENTATION_GUIDE.md this module must honor:

- D4a: `move_to_trash` is always `mv` to a configured trash path — never
  `mega-rm`. This makes the safety net independent of undocumented `rm`
  semantics.
- D4c: MEGAcmd documents no stable machine-readable output format. Every
  parser here is strict — an unparseable line raises rather than being
  silently skipped, because a silently-skipped file is an invisible
  data-loss path. Output is forced into as deterministic a shape as
  possible via explicit flags (-l, --show-handles, --time-format=ISO6081).

None of these commands are executed with shell=True, and every remote path
is passed as a distinct argv element — never interpolated into a shell
string — so a filename containing shell metacharacters cannot inject
commands.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

MEGACMD_TIME_FORMAT = "ISO6081"

# Header row emitted by `mega-ls -l`, used to detect/skip it defensively
# (see _parse_ls_line docstring for why we don't rely on line position).
_LS_HEADER_PREFIX = "FLAGS"


class MegaClientError(RuntimeError):
    """A mega-* command failed or its output could not be parsed."""


class MegaParseError(MegaClientError):
    """Output from a mega-* command didn't match the expected shape.

    Raised instead of skipping the offending line (D4c) — an unparseable
    line must stop the run, not vanish from the listing.
    """


@dataclass(frozen=True)
class RemoteEntry:
    path: str
    size: int
    is_dir: bool
    node_handle: str
    mtime_iso: str


# Real `mega-ls -l --show-handles` output, captured against a live account
# (Phase 0 / A6 — confirmed real, not guessed from docs, since MEGAcmd
# documents no stable machine-readable format):
#
#   FLAGS VERS      SIZE    DATE          HANDLE NAME
#   ----    1      3470559 2026-08-03 H:GEFhiD7K 2026-08-03 10.10.48.jpg
#   ----    1            4 2026-08-18 H:vdElBb7Y moved.txt
#
# Notable, non-obvious things this format forces on the parser:
# - FLAGS is 4 dashes for a plain file, not the 10-char `-rwx...` shape a
#   *nix `ls -l` would suggest. Directory flags are unconfirmed — treated
#   as unparseable (raise) rather than guessed, per D4c.
# - DATE has no time component (`2026-08-03`), not the ISO8601-with-time
#   the guide assumed --time-format=ISO6081 would produce.
# - NAME can itself contain spaces and even look like a date
#   ("2026-08-03 10.10.48.jpg" — Pixel's own default filename format),
#   so column-count splitting must split on the first 5 whitespace runs
#   only and take everything after HANDLE as NAME, whole.
_LS_FLAGS_RE = re.compile(r"^[-d]{4}$")
_LS_MIN_FIELDS = 6  # FLAGS VERS SIZE DATE HANDLE NAME(>=1 word)


def _run(args: list[str], *, input_text: str | None = None, timeout: int = 300) -> str:
    """Run a mega-* command. Never through a shell, args passed as a list."""
    try:
        result = subprocess.run(
            args,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise MegaClientError(f"command not found: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise MegaClientError(f"timed out: {' '.join(args)}") from exc

    if result.returncode != 0:
        raise MegaClientError(
            f"{' '.join(args)} failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def _parse_ls_line(line: str, *, parent_path: str) -> RemoteEntry:
    # FLAGS VERS SIZE DATE HANDLE NAME — split on the first 5 whitespace
    # runs only, since NAME itself may contain spaces (see format notes
    # above _LS_FLAGS_RE).
    parts = line.split(maxsplit=5)
    if len(parts) < _LS_MIN_FIELDS:
        raise MegaParseError(f"unrecognized mega-ls line shape: {line!r}")

    flags, _vers, size_str, mtime, handle, name = parts

    if not _LS_FLAGS_RE.match(flags):
        raise MegaParseError(f"unrecognized flags column {flags!r} in line: {line!r}")
    if not handle.startswith("H:"):
        raise MegaParseError(f"unrecognized handle column {handle!r} in line: {line!r}")

    try:
        size = int(size_str)
    except ValueError as exc:
        raise MegaParseError(f"non-integer size in line: {line!r}") from exc

    # Directory flag shape is unconfirmed against real output (Phase 0
    # only exercised plain files) — deliberately not guessed. is_dir stays
    # False here; a real directory listing will surface as a parse
    # failure until this is confirmed and encoded explicitly.
    is_dir = False

    full_path = f"{parent_path.rstrip('/')}/{name}"

    return RemoteEntry(
        path=full_path,
        size=size,
        is_dir=is_dir,
        node_handle=handle,
        mtime_iso=mtime,
    )


class MegaClient:
    """Thin wrapper around the mega-* CLI. Every method returns/accepts
    manifest-friendly types — callers never see raw CLI text.
    """

    def __init__(self, *, mega_bin_prefix: str = "mega-"):
        self._prefix = mega_bin_prefix

    def _bin(self, name: str) -> str:
        return f"{self._prefix}{name}"

    def list(self, remote_path: str) -> list[RemoteEntry]:
        """List a remote folder. Strict parsing (D4c) — an unparseable
        line raises MegaParseError instead of being dropped.
        """
        output = _run(
            [
                self._bin("ls"),
                "-l",
                "--show-handles",
                f"--time-format={MEGACMD_TIME_FORMAT}",
                remote_path,
            ]
        )
        entries = []
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            # Real output (A6) leads with a "/path/:" folder header line
            # and a "FLAGS VERS SIZE DATE HANDLE NAME" column header —
            # neither documented, both confirmed empirically. Skip by
            # shape, not by line position, since either could in
            # principle be absent (e.g. listing a single file).
            if line.endswith(":") and "H:" not in line:
                continue
            if line.startswith(_LS_HEADER_PREFIX):
                continue
            entries.append(_parse_ls_line(line, parent_path=remote_path))
        return entries

    def stat(self, remote_path: str) -> RemoteEntry | None:
        """Stat a single remote file. Returns None if it doesn't exist —
        this is a normal, expected outcome (e.g. verify-before-delete
        checks), not an error.
        """
        parent = remote_path.rsplit("/", 1)[0] or "/"
        name = remote_path.rsplit("/", 1)[-1]
        for entry in self.list(parent):
            if entry.path.rsplit("/", 1)[-1] == name:
                return entry
        return None

    def download(self, remote_path: str, local_dir: Path) -> Path:
        local_dir.mkdir(parents=True, exist_ok=True)
        _run([self._bin("get"), remote_path, str(local_dir)])
        local_path = local_dir / remote_path.rsplit("/", 1)[-1]
        if not local_path.exists():
            raise MegaClientError(
                f"download reported success but file not found at {local_path}"
            )
        return local_path

    def mkdir_p(self, remote_dir: str) -> None:
        """Create a remote directory, including parents, if it doesn't
        already exist. Used before uploading into the mirrored compressed
        tree, since MEGAcmd's `put` doesn't create missing parent folders.
        """
        _run([self._bin("mkdir"), "-p", remote_dir])

    def upload(self, local_path: Path, remote_dir: str) -> str:
        """Upload a local file to a remote directory. Returns the resulting
        remote path (caller should verify via stat() afterward — see
        transcoder/worker verify-before-trust requirement).
        """
        _run([self._bin("put"), str(local_path), remote_dir])
        return f"{remote_dir.rstrip('/')}/{local_path.name}"

    def move_to_trash(self, remote_path: str, trash_dir: str) -> str:
        """D4a: the only delete mechanism. Always `mv`, never `mega-rm`.

        Deterministic and self-documenting rather than depending on
        undocumented Rubbish-bin semantics (A2 is informational only).
        """
        _run([self._bin("mv"), remote_path, trash_dir])
        return f"{trash_dir.rstrip('/')}/{remote_path.rsplit('/', 1)[-1]}"

    def whoami(self) -> str:
        return _run([self._bin("whoami")]).strip()

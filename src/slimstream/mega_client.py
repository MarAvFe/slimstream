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


# Matches `mega-ls -l --show-handles --time-format=ISO6081` output lines.
# Real shape must be confirmed against captured fixtures (Phase 0 / A6);
# this pattern is deliberately strict and documented so it fails loud
# rather than silently mis-parsing when the real format differs.
#
# Expected columns: FLAGS SIZE DATE HANDLE NAME
# e.g.: "-rw-------  1234567  2026-01-15T10:22:03  H:AbCd1234  IMG_0001.jpg"
_LS_LINE_RE = re.compile(
    r"^(?P<flags>[-dl][-rwx]{9})\s+"
    r"(?P<size>\d+)\s+"
    r"(?P<mtime>\S+)\s+"
    r"(?P<handle>H:[A-Za-z0-9_-]+)\s+"
    r"(?P<name>.+)$"
)


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
    match = _LS_LINE_RE.match(line)
    if match is None:
        raise MegaParseError(f"unrecognized mega-ls line shape: {line!r}")

    flags = match.group("flags")
    name = match.group("name")
    is_dir = flags[0] == "d"
    full_path = f"{parent_path.rstrip('/')}/{name}"

    try:
        size = int(match.group("size"))
    except ValueError as exc:
        raise MegaParseError(f"non-integer size in line: {line!r}") from exc

    return RemoteEntry(
        path=full_path,
        size=size,
        is_dir=is_dir,
        node_handle=match.group("handle"),
        mtime_iso=match.group("mtime"),
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

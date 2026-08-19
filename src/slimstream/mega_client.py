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


# MEGAcmd exit codes, probed empirically on 2026-08-19 rather than taken
# from docs (they aren't documented). Assuming POSIX-like semantics here
# is exactly what broke the first two real runs: `mkdir -p` does NOT mean
# "succeed if it already exists", and `ls` on a missing path is a hard
# error rather than an empty listing.
EXIT_NOT_FOUND = 53  # `Couldn't find "<path>"` — missing file OR folder
EXIT_ALREADY_EXISTS = 54  # `Folder already exists: <name>`


class MegaClientError(RuntimeError):
    """A mega-* command failed or its output could not be parsed."""

    def __init__(self, message: str, *, returncode: int | None = None):
        super().__init__(message)
        self.returncode = returncode


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


# Real `mega-ls -l --show-handles` output, audited against a live 22,146-file
# account (Phase 0 / A6 + the 2026-08-19 full-library audit — confirmed
# empirically, never guessed from docs, since MEGAcmd documents no stable
# machine-readable format):
#
#   FLAGS VERS      SIZE    DATE          HANDLE NAME
#   ----    1      3470559 2026-08-03 H:GEFhiD7K 2026-08-03 10.10.48.jpg
#   -ep-    1      1246296 2022-10-30 H:aA0GmAKK 2022-10-30 06.06.58.jpg
#   d---    -            - 2026-08-18 H:mZ80GSKI keepers
#
# Three things the real data forced, each of which crashed an earlier,
# stricter version of this parser:
#
# 1. FLAGS is NOT always "----". The live library contains 133 rows of
#    "-ep-" (exported / public-link files) alongside 22,011 "----" and 2
#    "d---". Only position 0 is load-bearing here (d = directory); the
#    remaining characters encode export/share status we never consume.
#    Validating characters we don't use turned harmless vendor variation
#    into a hard crash, so we now check only what we actually depend on.
#    This is the narrow reading of D4c's fail-loud rule: be strict about
#    fields that drive behavior, not about decoration.
#
# 2. DATE may be one token ("2026-08-03", with --time-format=ISO6081) or
#    two ("18Aug2026 04:57:36", the default format). A fixed 6-field split
#    silently depends on the former: the audit measured 0 / 22,146 lines
#    parsing under the default format, i.e. a single flag change would
#    make every file invisible to discovery. Anchoring on the H: handle
#    token instead of counting fields from the left parses 22,146 / 22,146
#    under BOTH formats.
#
# 3. NAME can contain spaces and can itself look like a date
#    ("2026-08-03 10.10.48.jpg" — Pixel's own filename format), so NAME is
#    everything after the handle, taken whole.
_LS_LINE_RE = re.compile(
    r"^(?P<flags>\S+)\s+"
    r"(?P<vers>\S+)\s+"
    r"(?P<size>\S+)\s+"
    r"(?P<date>.*?)\s+"  # non-greedy: absorbs a 1- or 2-token date
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
        # MEGAcmd writes its error text to stdout, not stderr, so include
        # both — an empty "failed (exit 53):" message is useless to debug.
        detail = (result.stderr.strip() or result.stdout.strip()).splitlines()
        raise MegaClientError(
            f"{' '.join(args)} failed (exit {result.returncode}): "
            f"{detail[0] if detail else '<no output>'}",
            returncode=result.returncode,
        )
    return result.stdout


def _parse_ls_line(line: str, *, parent_path: str) -> RemoteEntry:
    """Parse one `mega-ls -l --show-handles` row. See the format notes
    above _LS_LINE_RE for the empirical basis of each rule.
    """
    match = _LS_LINE_RE.match(line)
    if match is None:
        raise MegaParseError(f"unrecognized mega-ls line shape: {line!r}")

    flags = match.group("flags")
    size_str = match.group("size")
    handle = match.group("handle")
    name = match.group("name")
    mtime = match.group("date")

    # Only position 0 of FLAGS drives behavior (d = directory). The rest
    # ("-ep-" for exported files, etc.) is status we never consume, so it
    # is deliberately not validated — see note 1 above _LS_LINE_RE.
    if not flags or flags[0] not in ("-", "d"):
        raise MegaParseError(
            f"unrecognized flags column {flags!r} (expected leading '-' or 'd') "
            f"in line: {line!r}"
        )

    is_dir = flags[0] == "d"

    # Directory rows print "-" for both VERS and SIZE (confirmed real
    # output: "d---    -            - 2026-08-18 H:mZ80GSKI keepers").
    # Files always carry a real integer size; a non-integer on a file row
    # is not something the audit ever saw, so it still fails loud.
    if is_dir:
        if size_str != "-":
            raise MegaParseError(
                f"expected '-' size for directory row, got {size_str!r} in line: {line!r}"
            )
        size = 0
    else:
        try:
            size = int(size_str)
        except ValueError as exc:
            raise MegaParseError(f"non-integer size in line: {line!r}") from exc

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
        """Stat a single remote FILE. Returns None if it doesn't exist —
        a normal, expected outcome (the already-compressed pre-check and
        the post-upload verify both rely on it), never an exception.

        Lists the exact path rather than the containing folder. Two
        reasons, both learned the hard way:

        - `mega-ls` on a missing path exits 53 rather than returning an
          empty listing, so listing the *parent* raised whenever the
          parent didn't exist yet — which is precisely the state on the
          very first run, before MEGA_COMPRESSED_ROOT has been created.
        - Listing the parent is O(folder size) per call. With the
          compressed root eventually holding ~22k files, statting each
          file would re-fetch and re-parse the entire listing every time.

        Only meaningful for files: `mega-ls <dir>` lists a directory's
        *contents*, so an empty directory is indistinguishable from a
        missing one here. Every caller passes a file path.
        """
        try:
            output = _run(
                [
                    self._bin("ls"),
                    "-l",
                    "--show-handles",
                    f"--time-format={MEGACMD_TIME_FORMAT}",
                    remote_path,
                ]
            )
        except MegaClientError as exc:
            if exc.returncode == EXIT_NOT_FOUND:
                return None
            raise

        parent = remote_path.rsplit("/", 1)[0] or "/"
        for line in output.splitlines():
            line = line.strip()
            if not line or line.startswith(_LS_HEADER_PREFIX):
                continue
            if line.endswith(":") and "H:" not in line:
                continue
            return _parse_ls_line(line, parent_path=parent)
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
        """Create a remote directory including parents; a no-op if it
        already exists.

        MEGAcmd's `-p` creates missing parents but, unlike POSIX
        `mkdir -p`, still exits 54 when the target itself exists —
        assuming otherwise crashed every upload as soon as the compressed
        root had been created once. Verified by probe on 2026-08-19.
        """
        try:
            _run([self._bin("mkdir"), "-p", remote_dir])
        except MegaClientError as exc:
            if exc.returncode == EXIT_ALREADY_EXISTS:
                return
            raise

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

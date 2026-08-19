"""Tests for the MEGAcmd wrapper, against real captured output and real
probed failure semantics (Phase 0 / A6 + the 2026-08-19 full-library
audit — IMPLEMENTATION_GUIDE.md D4c/D7). MEGAcmd documents neither its
output format nor its exit codes, so everything asserted here is
empirical, never inferred from how a POSIX tool would behave.

Real-world behaviour these lock in — every one of them broke a live run
first:
- FLAGS is 4 chars whose position 0 is the directory bit; positions 1-3
  carry status ("-ep-" = exported/public link, 133 rows in the real
  library) that must be tolerated, not validated
- DATE is one token under --time-format=ISO6081 but two under the
  default, so the parser anchors on the H: handle rather than counting
  fields
- NAME can contain spaces and look like a date (Pixel's own filename
  format: "2026-08-03 10.10.48.jpg")
- Output may lead with a "/path/:" line and a "FLAGS VERS ..." header,
  both skipped by shape rather than by position
- `ls` on a missing path exits 53 (not an empty listing); `mkdir -p` on
  an existing folder exits 54 (unlike POSIX mkdir -p)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from slimstream.mega_client import (
    EXIT_ALREADY_EXISTS,
    EXIT_NOT_FOUND,
    MegaClient,
    MegaClientError,
    MegaParseError,
    RemoteEntry,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text()


class _FakeRun:
    """Swaps mega_client._run for one that returns fixture text instead
    of actually invoking mega-ls.
    """

    def __init__(self, output: str):
        self.output = output
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        return self.output


class _FailingRun:
    """Swaps mega_client._run for one that fails the way the real CLI
    does, with a specific exit code attached.
    """

    def __init__(self, returncode: int, message: str = "boom"):
        self.returncode = returncode
        self.message = message
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        raise MegaClientError(self.message, returncode=self.returncode)


def test_parses_real_captured_output(monkeypatch):
    import slimstream.mega_client as mc

    fake_run = _FakeRun(_load_fixture("ls_output_sample.txt"))
    monkeypatch.setattr(mc, "_run", fake_run)

    client = MegaClient()
    entries = client.list("/slimstream-test")

    assert len(entries) == 7  # header + path lines correctly skipped

    by_name = {e.path.rsplit("/", 1)[-1]: e for e in entries}

    assert "2026-08-03 10.10.48.jpg" in by_name  # name-with-spaces preserved whole
    entry = by_name["2026-08-03 10.10.48.jpg"]
    assert entry.size == 3470559
    assert entry.node_handle == "H:GEFhiD7K"
    assert entry.mtime_iso == "2026-08-03"
    assert entry.is_dir is False

    small = by_name["moved.txt"]
    assert small.size == 4
    assert small.node_handle == "H:vdElBb7Y"

    keepers_dir = by_name["keepers"]
    assert keepers_dir.is_dir is True
    assert keepers_dir.size == 0
    assert keepers_dir.node_handle == "H:mZ80GSKI"


def test_parses_exported_public_link_rows(monkeypatch):
    """133 of 22,146 rows in the real library carry "-ep-" flags
    (exported / public-link files) rather than "----". An earlier parser
    validated all four flag characters and crashed on every one of them,
    even though only position 0 (directory bit) drives any behavior.
    """
    import slimstream.mega_client as mc

    monkeypatch.setattr(mc, "_run", _FakeRun(_load_fixture("ls_output_sample.txt")))

    client = MegaClient()
    entries = client.list("/slimstream-test")
    by_name = {e.path.rsplit("/", 1)[-1]: e for e in entries}

    exported = by_name["2022-10-30 06.06.58.jpg"]
    assert exported.is_dir is False
    assert exported.size == 1246296
    assert exported.node_handle == "H:aA0GmAKK"


def test_parses_default_time_format_with_two_token_date(monkeypatch):
    """DATE is one token under --time-format=ISO6081 ("2026-08-03") but
    two under MEGAcmd's default ("18Aug2026 04:57:36"). A fixed-field
    split silently depended on the former — the full-library audit
    measured 0 / 22,146 rows parsing under the default format, meaning a
    single flag change would have made every file invisible to discovery
    rather than raising. Anchoring on the H: handle parses both.
    """
    import slimstream.mega_client as mc

    monkeypatch.setattr(
        mc, "_run", _FakeRun(_load_fixture("ls_output_default_timeformat.txt"))
    )

    client = MegaClient()
    entries = client.list("/Camera Uploads")

    assert len(entries) == 5
    by_name = {e.path.rsplit("/", 1)[-1]: e for e in entries}

    photo = by_name["2014-11-14 10.24.15_1.jpg"]
    assert photo.size == 10038
    assert photo.node_handle == "H:6cllkZDL"
    assert photo.is_dir is False
    # the whole two-token date is preserved, not truncated to its first half
    assert photo.mtime_iso == "14Nov2014 16:24:15"

    assert by_name["keepers"].is_dir is True
    assert by_name["recyclingbin"].is_dir is True
    assert by_name["2022-10-30 06.06.58.jpg"].size == 1246296


def test_parses_full_paths_correctly(monkeypatch):
    import slimstream.mega_client as mc

    fake_run = _FakeRun(_load_fixture("ls_output_sample.txt"))
    monkeypatch.setattr(mc, "_run", fake_run)

    client = MegaClient()
    entries = client.list("/slimstream-test")

    paths = {e.path for e in entries}
    assert "/slimstream-test/moved.txt" in paths
    assert "/slimstream-test/2026-08-03 10.10.48.jpg" in paths


def test_unparseable_line_raises_not_skips(monkeypatch):
    import slimstream.mega_client as mc

    bad_output = "/slimstream-test/:\nFLAGS VERS SIZE DATE HANDLE NAME\nthis is not a valid ls line\n"
    monkeypatch.setattr(mc, "_run", _FakeRun(bad_output))

    client = MegaClient()
    with pytest.raises(MegaParseError):
        client.list("/slimstream-test")


def test_unrecognized_flags_column_raises(monkeypatch):
    """Position 0 of FLAGS is the one character we depend on, so a value
    that is neither '-' nor 'd' still fails loud — unlike positions 1-3,
    which carry status we never consume and must not crash on.
    """
    import slimstream.mega_client as mc

    bad_output = (
        "/slimstream-test/:\n"
        "FLAGS VERS SIZE DATE HANDLE NAME\n"
        "xep-    1      3470559 2026-08-03 H:GEFhiD7K somefile.jpg\n"
    )
    monkeypatch.setattr(mc, "_run", _FakeRun(bad_output))

    client = MegaClient()
    with pytest.raises(MegaParseError):
        client.list("/slimstream-test")


def test_unknown_status_flags_are_tolerated(monkeypatch):
    """The counterpart to the test above: unfamiliar characters in FLAGS
    positions 1-3 must NOT crash discovery. MEGAcmd is free to add status
    letters we've never seen, and losing the whole run over decoration we
    don't read is the exact failure the audit found in production.
    """
    import slimstream.mega_client as mc

    output = (
        "/slimstream-test/:\n"
        "FLAGS VERS SIZE DATE HANDLE NAME\n"
        "-xyz    1      3470559 2026-08-03 H:GEFhiD7K somefile.jpg\n"
    )
    monkeypatch.setattr(mc, "_run", _FakeRun(output))

    client = MegaClient()
    entries = client.list("/slimstream-test")
    assert len(entries) == 1
    assert entries[0].size == 3470559
    assert entries[0].is_dir is False


def test_non_integer_size_raises(monkeypatch):
    import slimstream.mega_client as mc

    bad_output = (
        "/slimstream-test/:\n"
        "FLAGS VERS SIZE DATE HANDLE NAME\n"
        "----    1      notanumber 2026-08-03 H:GEFhiD7K somefile.jpg\n"
    )
    monkeypatch.setattr(mc, "_run", _FakeRun(bad_output))

    client = MegaClient()
    with pytest.raises(MegaParseError):
        client.list("/slimstream-test")


def test_directory_row_parses_with_dash_size(monkeypatch):
    """Real captured output (2026-08-19, against /Camera Uploads which
    contains a real 'keepers' subfolder): directory rows print '-' for
    both VERS and SIZE, not a numeric size. This crashed run_discovery in
    production before the parser handled it explicitly.
    """
    import slimstream.mega_client as mc

    output = (
        "/Camera Uploads/:\n"
        "FLAGS VERS      SIZE    DATE          HANDLE NAME\n"
        "d---    -            - 2026-08-18 H:mZ80GSKI keepers\n"
    )
    monkeypatch.setattr(mc, "_run", _FakeRun(output))

    client = MegaClient()
    entries = client.list("/Camera Uploads")

    assert len(entries) == 1
    assert entries[0].is_dir is True
    assert entries[0].size == 0
    assert entries[0].node_handle == "H:mZ80GSKI"
    assert entries[0].path == "/Camera Uploads/keepers"


def test_directory_row_with_unexpected_nonzero_size_raises(monkeypatch):
    """A directory row with a real number instead of '-' hasn't been seen
    in real output — raise rather than silently accept an unverified
    shape (D4c)."""
    import slimstream.mega_client as mc

    output = (
        "/Camera Uploads/:\n"
        "FLAGS VERS SIZE DATE HANDLE NAME\n"
        "d---    -      12345 2026-08-18 H:mZ80GSKI weird-dir\n"
    )
    monkeypatch.setattr(mc, "_run", _FakeRun(output))

    client = MegaClient()
    with pytest.raises(MegaParseError):
        client.list("/Camera Uploads")


# --- real-CLI failure semantics (probed 2026-08-19, see EXIT_* constants) ---
#
# Both behaviours below crashed real production runs. They were invisible
# to the earlier tests because the fakes were more forgiving than MEGAcmd:
# the fake stat() politely returned None and the fake mkdir_p was a no-op,
# so the test doubles validated the assumption instead of the behaviour.


def test_stat_returns_none_when_path_missing(monkeypatch):
    """`mega-ls` on a missing path exits 53 rather than returning an empty
    listing. stat() must absorb that into None — its documented contract —
    instead of raising, or the very first run (before the compressed root
    exists) fails on every single file.
    """
    import slimstream.mega_client as mc

    failing = _FailingRun(EXIT_NOT_FOUND, 'Couldn\'t find "/pics-archive/x.jpg"')
    monkeypatch.setattr(mc, "_run", failing)

    client = MegaClient()
    assert client.stat("/pics-archive/x.jpg") is None


def test_stat_reraises_unexpected_failures(monkeypatch):
    """Only 'not found' is absorbed. A different failure (auth, network,
    quota) must still surface — silently treating those as 'file absent'
    would make the pipeline re-do work it already did, or worse, believe a
    verify step passed.
    """
    import slimstream.mega_client as mc

    monkeypatch.setattr(mc, "_run", _FailingRun(9, "session expired"))

    client = MegaClient()
    with pytest.raises(MegaClientError):
        client.stat("/pics-archive/x.jpg")


def test_stat_lists_exact_path_not_parent_folder(monkeypatch):
    """stat() must query the file itself. Listing the parent is both
    wrong (raises when the parent is missing) and O(folder size) per
    call — with ~22k files in the compressed root that is a full listing
    fetched and parsed for every single file processed.
    """
    import slimstream.mega_client as mc

    single_row = (
        "FLAGS VERS      SIZE    DATE          HANDLE NAME\n"
        "----    1       629821 2014-11-14 H:3VkDgQKJ photo.jpg\n"
    )
    fake = _FakeRun(single_row)
    monkeypatch.setattr(mc, "_run", fake)

    client = MegaClient()
    entry = client.stat("/pics-archive/photo.jpg")

    assert entry is not None
    assert entry.path == "/pics-archive/photo.jpg"
    assert entry.size == 629821
    assert fake.calls[0][-1] == "/pics-archive/photo.jpg"  # not "/pics-archive"


def test_mkdir_p_is_idempotent_when_folder_exists(monkeypatch):
    """MEGAcmd's `-p` creates parents but still exits 54 if the target
    exists — unlike POSIX `mkdir -p`. Assuming otherwise crashed every
    upload once the compressed root had been created.
    """
    import slimstream.mega_client as mc

    monkeypatch.setattr(
        mc, "_run", _FailingRun(EXIT_ALREADY_EXISTS, "Folder already exists: pics-archive")
    )

    client = MegaClient()
    client.mkdir_p("/pics-archive")  # must not raise


def test_mkdir_p_reraises_other_failures(monkeypatch):
    import slimstream.mega_client as mc

    monkeypatch.setattr(mc, "_run", _FailingRun(9, "session expired"))

    client = MegaClient()
    with pytest.raises(MegaClientError):
        client.mkdir_p("/pics-archive")

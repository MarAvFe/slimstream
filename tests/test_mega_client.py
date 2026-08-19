"""Parser tests against real captured MEGAcmd output (Phase 0 / A6 —
IMPLEMENTATION_GUIDE.md D4c). MEGAcmd documents no stable machine-readable
format, so this fixture is the actual ground truth, not a guess: captured
2026-08-18 against a live account via
`mega-ls -l --show-handles --time-format=ISO6081 /slimstream-test/`.

Notable real-world shape this locks in:
- FLAGS is 4 dashes for a file ("----"), not *nix ls's 10-char -rwx... form
- DATE has no time component despite --time-format=ISO6081
- NAME can contain spaces and look like a date (Pixel's own filename
  format: "2026-08-03 10.10.48.jpg") — column splitting must account for
  this, not split on all whitespace
- Output leads with a "/path/:" line and a "FLAGS VERS SIZE..." header
  line, both of which must be skipped without being mistaken for entries
"""

from __future__ import annotations

from pathlib import Path

import pytest

from slimstream.mega_client import MegaClient, MegaParseError, RemoteEntry

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


def test_parses_real_captured_output(monkeypatch):
    import slimstream.mega_client as mc

    fake_run = _FakeRun(_load_fixture("ls_output_sample.txt"))
    monkeypatch.setattr(mc, "_run", fake_run)

    client = MegaClient()
    entries = client.list("/slimstream-test")

    assert len(entries) == 6  # header + path lines correctly skipped

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
    import slimstream.mega_client as mc

    bad_output = (
        "/slimstream-test/:\n"
        "FLAGS VERS SIZE DATE HANDLE NAME\n"
        "drwx    1      3470559 2026-08-03 H:GEFhiD7K somefile.jpg\n"
    )
    monkeypatch.setattr(mc, "_run", _FakeRun(bad_output))

    client = MegaClient()
    with pytest.raises(MegaParseError):
        client.list("/slimstream-test")


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

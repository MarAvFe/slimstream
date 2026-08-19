"""CLI entrypoint. Loads config once at startup — a bad config must fail
before any file is touched (config.py's contract), not partway through a
run.

Logging is split into two files under config.log_dir, since thousands of
per-file lines in a terminal (or one flat log) makes finding "what happened
across recent runs" impractical:

- slimstream.log: full verbose log (every file processed, every error) —
  tail -f this while a run is in progress.
- runs.log: one line per invocation, params + outcome — tail this for a
  fast, human-readable history of past runs without wading through the
  verbose log.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from slimstream.config import Config, ConfigError, load_config
from slimstream.manifest import Manifest, export_manifest, import_manifest
from slimstream.mega_client import MegaClient
from slimstream.worker import (
    JobAResult,
    JobBResult,
    run_job_a,
    run_job_b,
    should_run_job_b_today,
    PausedError,
)

RUNS_LOG_NAME = "runs.log"
VERBOSE_LOG_NAME = "slimstream.log"


def _setup_logging(config: Config) -> None:
    config.log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    file_handler = logging.FileHandler(config.log_dir / VERBOSE_LOG_NAME)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()  # avoid duplicate handlers if main() runs more than once in-process
    root.addHandler(file_handler)
    root.addHandler(console_handler)


def _append_run_summary(config: Config, line: str) -> None:
    """One appended line per invocation — cheap to `tail -f` for a
    human-readable history distinct from the verbose per-file log.
    """
    config.log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    with open(config.log_dir / RUNS_LOG_NAME, "a") as f:
        f.write(f"{timestamp} UTC  {line}\n")


def _job_a_summary_line(config: Config, result: JobAResult) -> str:
    mode = "upload=LIVE" if not config.dry_run_upload else "upload=dry-run"
    # succeeded/failed are reported separately and never merged into one
    # "processed" number: a batch where every file errored must not read
    # as a batch of work done, since runs.log is the monitoring surface.
    reaped = f" reaped={result.reaped}" if result.reaped else ""
    return (
        f"job-a  {mode}  batch<={config.max_batch_size}  "
        f"discovered={result.discovered} ok={result.succeeded} failed={result.failed}"
        f"{reaped} parked={result.parked_for_retries} still_pending={result.still_pending}"
    )


def _job_b_summary_line(config: Config, result: JobBResult) -> str:
    mode = "delete=LIVE" if not config.dry_run_delete else "delete=dry-run"
    return (
        f"job-b  {mode}  retention={config.retention_days}d key={config.retention_key}  "
        f"moved={result.moved_to_trash} failed={result.failed_to_move}"
    )


def _load_env_file(explicit_path: str | None) -> None:
    """Load .env into the process environment before config.load_config()
    reads it. Values already set in the real environment (e.g. by
    systemd's EnvironmentFile=, or explicit shell export) are NOT
    overridden — .env only fills gaps, so the same command behaves
    correctly whether run manually, via systemd, or via cron, without
    requiring `source .env` beforehand.
    """
    if explicit_path is not None:
        path = Path(explicit_path)
        if not path.is_file():
            print(f"--env-file {explicit_path} not found", file=sys.stderr)
            sys.exit(2)
        load_dotenv(path, override=False)
        return

    # Default: .env in the current working directory. This matches the
    # documented manual-run pattern (`cd ~/slimstream/app && slimstream
    # job-a`) and systemd's WorkingDirectory=, so no flag is needed in
    # either case — only an unusual invocation needs --env-file.
    default_path = Path.cwd() / ".env"
    if default_path.is_file():
        load_dotenv(default_path, override=False)


def _default_export_path(config: Config) -> Path:
    return config.manifest_db_path.parent / "manifest-export.json"


def _upload_export(config: Config, mega: MegaClient, local: Path, logger) -> None:
    remote_dir = config.manifest_export_path.rsplit("/", 1)[0] or "/"
    mega.mkdir_p(remote_dir)
    mega.upload(local, remote_dir)
    logger.info("uploaded manifest export to %s", config.manifest_export_path)


def _export(config: Config, manifest: Manifest, mega: MegaClient, logger) -> None:
    """Back up after a run. A backup failure must not fail the run itself,
    but it must be loud — a silently-missing backup is worse than none,
    because it is trusted.
    """
    try:
        out = _default_export_path(config)
        count = export_manifest(manifest, out)
        _upload_export(config, mega, out, logger)
        logger.info("manifest backup: %d records", count)
    except Exception:  # noqa: BLE001
        logger.exception("MANIFEST BACKUP FAILED — run itself was fine, backup is stale")
        _append_run_summary(config, "  !! manifest backup FAILED (see slimstream.log)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="slimstream")
    parser.add_argument(
        "--env-file",
        default=None,
        help="path to a .env file to load (default: .env in the current directory)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    job_a = sub.add_parser("job-a", help="discover + compress new arrivals")
    exp = sub.add_parser("export-manifest", help="back up the manifest to Mega")
    exp.add_argument("--output", default=None, help="local JSON path (default: alongside the db)")
    exp.add_argument("--no-upload", action="store_true", help="write locally, skip the Mega upload")
    imp = sub.add_parser("import-manifest", help="restore the manifest from a JSON export")
    imp.add_argument("input", help="path to a JSON export")
    imp.add_argument(
        "--force", action="store_true",
        help="replace existing records (refuses to touch a non-empty manifest otherwise)",
    )
    job_b = sub.add_parser("job-b", help="rolling retention delete")
    job_b.add_argument(
        "--force",
        action="store_true",
        help="run even if today isn't the configured RETENTION_RUN_DAY",
    )

    args = parser.parse_args(argv)
    _load_env_file(args.env_file)

    try:
        config = load_config()
    except ConfigError as exc:
        # log_dir isn't known yet if config itself is broken — this one
        # error has nowhere to go but stderr.
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    _setup_logging(config)
    logger = logging.getLogger("slimstream.cli")

    # Not fatal — it works today — but it is a live footgun worth naming on
    # every run. Trashed originals keep sitting inside the tree discovery
    # scans, and are only invisible because mega.list() happens not to
    # recurse (D6). If recursion is ever added, every trashed original gets
    # rediscovered at its new path, earns a new file_id, and is
    # re-processed from scratch.
    if config.mega_trash_path.rstrip("/").startswith(config.mega_camera_path.rstrip("/") + "/"):
        logger.warning(
            "MEGA_TRASH_PATH (%s) is inside MEGA_CAMERA_PATH (%s); safe only while "
            "listing is non-recursive. Prefer a trash folder outside the scanned tree.",
            config.mega_trash_path,
            config.mega_camera_path,
        )

    manifest = Manifest(config.manifest_db_path)
    mega = MegaClient()

    try:
        if args.command == "job-a":
            result = run_job_a(manifest, mega, config)
            _append_run_summary(config, _job_a_summary_line(config, result))
            if config.manifest_export_enabled:
                _export(config, manifest, mega, logger)
        elif args.command == "export-manifest":
            out = Path(args.output) if args.output else _default_export_path(config)
            count = export_manifest(manifest, out)
            logger.info("exported %d records to %s", count, out)
            if not args.no_upload:
                _upload_export(config, mega, out, logger)
            _append_run_summary(config, f"export-manifest  records={count} -> {out}")
        elif args.command == "import-manifest":
            count = import_manifest(manifest, Path(args.input), force=args.force)
            logger.info("imported %d records from %s", count, args.input)
            _append_run_summary(config, f"import-manifest  records={count} <- {args.input}")
        elif args.command == "job-b":
            if not args.force and not should_run_job_b_today(config):
                msg = (
                    f"job-b  skipped (not RETENTION_RUN_DAY={config.retention_run_day}, "
                    f"use --force to override)"
                )
                logger.info(msg)
                _append_run_summary(config, msg)
                return 0
            result = run_job_b(manifest, mega, config)
            _append_run_summary(config, _job_b_summary_line(config, result))
    except PausedError as exc:
        logger.info("%s", exc)
        _append_run_summary(config, f"{args.command}  skipped (paused)")
        return 0
    except Exception as exc:  # noqa: BLE001
        # Any unhandled failure must still land in both logs — a crash
        # that's only visible via journalctl defeats the point of having
        # runs.log/slimstream.log to tail for an unattended pipeline.
        logger.exception("%s crashed", args.command)
        _append_run_summary(config, f"{args.command}  CRASHED: {exc}")
        return 1
    finally:
        manifest.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())

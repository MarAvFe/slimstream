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
from slimstream.manifest import Manifest
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
    return (
        f"job-a  {mode}  batch<={config.max_batch_size}  "
        f"discovered={result.discovered} ok={result.succeeded} failed={result.failed} "
        f"parked={result.parked_for_retries} still_pending={result.still_pending}"
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="slimstream")
    parser.add_argument(
        "--env-file",
        default=None,
        help="path to a .env file to load (default: .env in the current directory)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    job_a = sub.add_parser("job-a", help="discover + compress new arrivals")
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

    manifest = Manifest(config.manifest_db_path)
    mega = MegaClient()

    try:
        if args.command == "job-a":
            result = run_job_a(manifest, mega, config)
            _append_run_summary(config, _job_a_summary_line(config, result))
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

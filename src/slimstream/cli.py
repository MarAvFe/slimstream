"""CLI entrypoint. Loads config once at startup — a bad config must fail
before any file is touched (config.py's contract), not partway through a
run.
"""

from __future__ import annotations

import argparse
import logging
import sys

from slimstream.config import ConfigError, load_config
from slimstream.manifest import Manifest
from slimstream.mega_client import MegaClient
from slimstream.worker import run_job_a, run_job_b, should_run_job_b_today, PausedError


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="slimstream")
    sub = parser.add_subparsers(dest="command", required=True)

    job_a = sub.add_parser("job-a", help="discover + compress new arrivals")
    job_b = sub.add_parser("job-b", help="rolling retention delete")
    job_b.add_argument(
        "--force",
        action="store_true",
        help="run even if today isn't the configured RETENTION_RUN_DAY",
    )

    args = parser.parse_args(argv)
    _setup_logging()
    logger = logging.getLogger("slimstream.cli")

    try:
        config = load_config()
    except ConfigError as exc:
        logger.error("configuration error: %s", exc)
        return 2

    manifest = Manifest(config.manifest_db_path)
    mega = MegaClient()

    try:
        if args.command == "job-a":
            run_job_a(manifest, mega, config)
        elif args.command == "job-b":
            if not args.force and not should_run_job_b_today(config):
                logger.info(
                    "not the configured retention run day (RETENTION_RUN_DAY=%d); "
                    "skipping (use --force to override)",
                    config.retention_run_day,
                )
                return 0
            run_job_b(manifest, mega, config)
    except PausedError as exc:
        logger.info("%s", exc)
        return 0
    finally:
        manifest.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())

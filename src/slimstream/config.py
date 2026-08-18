"""Configuration loading and validation.

All settings come from environment variables (see .env.example). Validation
happens at load time, not at point of use — a bad value must fail before any
file is touched, not midway through a delete loop.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_TRUE_VALUES = {"1", "true", "yes", "on"}
_VALID_RETENTION_KEYS = {"captured_at", "discovered_at"}


class ConfigError(ValueError):
    """Raised when configuration is missing or fails validation."""


@dataclass(frozen=True)
class Config:
    mega_camera_path: str
    mega_keepers_path: str
    mega_trash_path: str

    retention_days: int
    retention_run_day: int
    retention_key: str

    video_height: int
    video_crf: int
    image_long_edge: int
    image_quality: int

    scratch_dir: Path
    manifest_db_path: Path

    settling_minutes: int
    dry_run: bool

    @property
    def manifest_export_path(self) -> str:
        """Remote Mega path for the nightly manifest export (D2)."""
        return f"{self.mega_trash_path.rsplit('/', 1)[0]}/slimstream-manifest-export.json"


def _require(env: dict[str, str], key: str) -> str:
    value = env.get(key)
    if value is None or value.strip() == "":
        raise ConfigError(f"missing required env var: {key}")
    return value


def _parse_int(env: dict[str, str], key: str, default: int | None = None) -> int:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        if default is None:
            raise ConfigError(f"missing required env var: {key}")
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc


def _parse_bool(env: dict[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUE_VALUES


def load_config(env: dict[str, str] | None = None) -> Config:
    """Load and validate config from the given mapping (defaults to os.environ).

    Raises ConfigError on anything invalid. Call this once at process start —
    every worker entrypoint must fail fast on bad config before touching Mega.
    """
    env = os.environ if env is None else env

    mega_camera_path = _require(env, "MEGA_CAMERA_PATH")
    mega_keepers_path = _require(env, "MEGA_KEEPERS_PATH")
    mega_trash_path = _require(env, "MEGA_TRASH_PATH")

    retention_days = _parse_int(env, "RETENTION_DAYS", default=30)
    if retention_days <= 0:
        raise ConfigError(f"RETENTION_DAYS must be positive, got {retention_days}")

    retention_run_day = _parse_int(env, "RETENTION_RUN_DAY", default=30)
    if not (1 <= retention_run_day <= 31):
        raise ConfigError(
            f"RETENTION_RUN_DAY must be 1-31, got {retention_run_day}"
        )

    # Default is discovered_at, not captured_at: A3 (IMPLEMENTATION_GUIDE.md
    # Phase 0) found that Mega's reported timestamp is upload/file time, not
    # EXIF capture time — --show-creation-time does not surface EXIF data.
    # captured_at remains selectable for setups where it's verified accurate.
    retention_key = env.get("RETENTION_KEY", "discovered_at").strip()
    if retention_key not in _VALID_RETENTION_KEYS:
        raise ConfigError(
            f"RETENTION_KEY must be one of {_VALID_RETENTION_KEYS}, got {retention_key!r}"
        )

    video_height = _parse_int(env, "VIDEO_HEIGHT", default=480)
    if video_height <= 0:
        raise ConfigError(f"VIDEO_HEIGHT must be positive, got {video_height}")

    video_crf = _parse_int(env, "VIDEO_CRF", default=30)
    if not (0 <= video_crf <= 51):
        raise ConfigError(f"VIDEO_CRF must be 0-51 (libx264 range), got {video_crf}")

    # 1200, not spec 1.9's original 1600 — see .env.example for tuning notes
    image_long_edge = _parse_int(env, "IMAGE_LONG_EDGE", default=1200)
    if image_long_edge <= 0:
        raise ConfigError(f"IMAGE_LONG_EDGE must be positive, got {image_long_edge}")

    image_quality = _parse_int(env, "IMAGE_QUALITY", default=60)
    if not (1 <= image_quality <= 100):
        raise ConfigError(f"IMAGE_QUALITY must be 1-100, got {image_quality}")

    scratch_dir = Path(_require(env, "SCRATCH_DIR"))
    manifest_db_path = Path(_require(env, "MANIFEST_DB_PATH"))

    settling_minutes = _parse_int(env, "SETTLING_MINUTES", default=15)
    if settling_minutes < 0:
        raise ConfigError(f"SETTLING_MINUTES must be >= 0, got {settling_minutes}")

    dry_run = _parse_bool(env, "DRY_RUN", default=True)

    return Config(
        mega_camera_path=mega_camera_path,
        mega_keepers_path=mega_keepers_path,
        mega_trash_path=mega_trash_path,
        retention_days=retention_days,
        retention_run_day=retention_run_day,
        retention_key=retention_key,
        video_height=video_height,
        video_crf=video_crf,
        image_long_edge=image_long_edge,
        image_quality=image_quality,
        scratch_dir=scratch_dir,
        manifest_db_path=manifest_db_path,
        settling_minutes=settling_minutes,
        dry_run=dry_run,
    )

from __future__ import annotations

import pytest

from slimstream.config import ConfigError, load_config

BASE_ENV = {
    "MEGA_CAMERA_PATH": "/Camera Uploads",
    "MEGA_KEEPERS_PATH": "/Camera Uploads/keepers",
    "MEGA_TRASH_PATH": "/slimstream-trash",
    "SCRATCH_DIR": "/tmp/scratch",
    "MANIFEST_DB_PATH": "/tmp/manifest.db",
}


def test_loads_with_defaults():
    cfg = load_config(dict(BASE_ENV))
    assert cfg.retention_days == 30
    assert cfg.retention_run_day == 30
    assert cfg.retention_key == "discovered_at"  # A3: Mega timestamp is upload time, not EXIF
    assert cfg.video_crf == 30
    assert cfg.dry_run is True
    assert cfg.max_batch_size == 100


def test_missing_required_var_raises():
    env = dict(BASE_ENV)
    del env["MEGA_CAMERA_PATH"]
    with pytest.raises(ConfigError):
        load_config(env)


def test_bad_retention_days_raises_before_use():
    env = dict(BASE_ENV, RETENTION_DAYS="not-a-number")
    with pytest.raises(ConfigError):
        load_config(env)


def test_negative_retention_days_raises():
    env = dict(BASE_ENV, RETENTION_DAYS="-5")
    with pytest.raises(ConfigError):
        load_config(env)


def test_invalid_retention_key_raises():
    env = dict(BASE_ENV, RETENTION_KEY="whatever")
    with pytest.raises(ConfigError):
        load_config(env)


def test_crf_out_of_range_raises():
    env = dict(BASE_ENV, VIDEO_CRF="99")
    with pytest.raises(ConfigError):
        load_config(env)


def test_retention_run_day_out_of_range_raises():
    env = dict(BASE_ENV, RETENTION_RUN_DAY="32")
    with pytest.raises(ConfigError):
        load_config(env)


def test_dry_run_parses_various_truthy_values():
    for val in ("true", "True", "1", "yes", "on"):
        assert load_config(dict(BASE_ENV, DRY_RUN=val)).dry_run is True
    for val in ("false", "0", "no", "off"):
        assert load_config(dict(BASE_ENV, DRY_RUN=val)).dry_run is False


def test_dry_run_defaults_true_when_unset():
    env = dict(BASE_ENV)
    env.pop("DRY_RUN", None)
    assert load_config(env).dry_run is True


def test_max_batch_size_custom_value():
    env = dict(BASE_ENV, MAX_BATCH_SIZE="20")
    assert load_config(env).max_batch_size == 20


def test_max_batch_size_zero_raises():
    env = dict(BASE_ENV, MAX_BATCH_SIZE="0")
    with pytest.raises(ConfigError):
        load_config(env)


def test_max_batch_size_negative_raises():
    env = dict(BASE_ENV, MAX_BATCH_SIZE="-10")
    with pytest.raises(ConfigError):
        load_config(env)

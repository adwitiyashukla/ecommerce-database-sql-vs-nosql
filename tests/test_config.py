from datetime import date

import pytest

from ecomdb import config
from ecomdb.cli import main

from .conftest import ROOT


def base():
    return config.load(ROOT / "config.toml")


def test_repo_config_loads_with_types():
    cfg = base()
    assert cfg.app.engines == ("postgres", "mysql", "dynamodb")
    assert cfg.data.start_date == date(2025, 1, 1)
    assert cfg.data.out_dir == ROOT / "data" / "csv"
    assert cfg.postgres.settings["shared_buffers"] == "512MB"
    assert cfg.flash.strategies[0] == "naive"


def test_overrides_parse_toml_values():
    cfg = base().with_overrides(
        ["data.scale=0.5", 'postgres.host="localhost"', "mysql.binlog=false"]
    )
    assert cfg.data.scale == 0.5
    assert cfg.postgres.host == "localhost"
    assert cfg.mysql.binlog is False


@pytest.mark.parametrize(
    "override, message",
    [
        ("data.scale=0", "data.scale"),
        ("postgres.port=0", "postgres.port"),
        ('postgres.mode="cloud"', "postgres.mode"),
        ("data.bogus=1", "unknown keys"),
        ("bench.processes=99", "bench.processes"),
        ("flash.buckets=99999999", "flash.buckets"),
        ('flash.strategies=["guess"]', "flash.strategies"),
        ('dynamodb.heap="2gb"', "dynamodb.heap"),
        ('dynamodb.endpoint="localhost:8000"', "dynamodb.endpoint"),
        ('data.start_date="yesterday"', "data.start_date"),
        ('app.engines=["oracle"]', "app.engines"),
        ("isolation.step_wait=true", "isolation.step_wait"),
    ],
)
def test_bad_values_are_rejected(override, message):
    with pytest.raises(config.ConfigError, match=message):
        base().with_overrides([override])


def test_override_needs_section_and_key():
    with pytest.raises(config.ConfigError):
        base().with_overrides(["scale=1"])


def test_aws_mode_allows_empty_endpoint():
    cfg = base().with_overrides(['dynamodb.mode="aws"', 'dynamodb.endpoint=""'])
    assert cfg.dynamodb.mode == "aws"


def test_cli_reports_config_errors(capsys):
    assert main(["status", "--config", str(ROOT / "config.toml"), "--set", "data.scale=0"]) == 2
    assert "config error" in capsys.readouterr().err


def test_cli_rejects_unknown_engines(capsys):
    assert main(["status", "--config", str(ROOT / "config.toml"), "--engines", "oracle"]) == 2

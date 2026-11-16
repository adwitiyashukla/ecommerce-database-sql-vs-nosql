from functools import cache
from pathlib import Path

import pytest

from ecomdb import config, flashsale, generate, load, servers
from ecomdb.workload import Dataset, open_store

ROOT = Path(__file__).resolve().parents[1]

TEST_OVERRIDES = (
    "data.scale=0.003",
    'data.out_dir="data/test_csv"',
    'postgres.database="ecomdb_test"',
    'mysql.database="ecomdb_test"',
    'dynamodb.table="ecomdb_test"',
    'app.results_dir="data/test_results"',
    "check.samples=30",
    "check.writes=80",
    "flash.clients=8",
    "flash.processes=2",
    "flash.stock=200",
    "flash.buckets=4",
    "flash.max_seconds=60.0",
    "isolation.step_wait=1.0",
)


@cache
def base_config():
    return config.load(ROOT / "config.toml").with_overrides(TEST_OVERRIDES)


ISOLATION_CELLS = {
    "postgres": (
        ("lost_update", "READ COMMITTED", "anomaly"),
        ("lost_update", "REPEATABLE READ", "serialization error"),
        ("lost_update_locking", "READ COMMITTED", "lock wait"),
        ("write_skew", "REPEATABLE READ", "anomaly"),
        ("write_skew", "SERIALIZABLE", "serialization error"),
        ("phantom_locking", "REPEATABLE READ", "anomaly"),
        ("non_repeatable_read", "REPEATABLE READ", "prevented"),
        ("dirty_read", "READ UNCOMMITTED", "prevented"),
    ),
    "mysql": (
        ("lost_update", "REPEATABLE READ", "anomaly"),
        ("lost_update", "SERIALIZABLE", "deadlock error"),
        ("lost_update_locking", "REPEATABLE READ", "lock wait"),
        ("write_skew", "SERIALIZABLE", "deadlock error"),
        ("phantom_locking", "READ COMMITTED", "anomaly"),
        ("phantom_locking", "REPEATABLE READ", "deadlock error"),
        ("non_repeatable_read", "SERIALIZABLE", "lock wait"),
        ("dirty_read", "READ UNCOMMITTED", "anomaly"),
    ),
}


def pytest_generate_tests(metafunc):
    engines = list(base_config().app.engines)
    sql = [e for e in engines if e != "dynamodb"]
    if "engine" in metafunc.fixturenames:
        metafunc.parametrize("engine", engines)
    if "sql_engine" in metafunc.fixturenames:
        metafunc.parametrize("sql_engine", sql)
    if "flash_case" in metafunc.fixturenames:
        cases = [
            (engine, strategy)
            for engine in engines
            for strategy in flashsale.strategies_for(engine, config.STRATEGIES)
            if strategy != "naive"
        ]
        metafunc.parametrize("flash_case", cases, ids=[f"{e}-{s}" for e, s in cases])
    if "isolation_case" in metafunc.fixturenames:
        cases = [(engine, *cell) for engine in sql for cell in ISOLATION_CELLS[engine]]
        ids = [f"{e}-{name}-{level.lower().replace(' ', '_')}" for e, name, level, _ in cases]
        metafunc.parametrize("isolation_case", cases, ids=ids)


@pytest.fixture(scope="session")
def cfg():
    return base_config()


@pytest.fixture(scope="session")
def loaded(cfg):
    generate.generate(cfg.data, cfg.app.low_stock)
    for engine in cfg.app.engines:
        if not servers.reachable(cfg, engine):
            pytest.fail(f"{engine} is not reachable, start it with python -m ecomdb up")
        load.LOADERS[engine](cfg)
    return cfg


@pytest.fixture(scope="session")
def dataset(loaded):
    return Dataset(loaded)


@pytest.fixture
def store(loaded, engine):
    opened = open_store(loaded, engine)
    yield opened
    opened.close()

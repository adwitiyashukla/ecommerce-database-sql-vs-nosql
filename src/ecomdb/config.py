import copy
import os
import re
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ENGINES = ("postgres", "mysql", "dynamodb")
STRATEGIES = (
    "naive",
    "row_lock",
    "conditional",
    "version",
    "serializable",
    "transaction",
    "buckets",
)
SQL_MODES = ("managed", "external")
DYNAMO_MODES = ("managed", "external", "aws")
SETTING_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
HEAP_SIZE = re.compile(r"^[1-9][0-9]*[kmgKMG]$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class AppConfig:
    engines: tuple[str, ...]
    low_stock: int
    results_dir: Path


@dataclass(frozen=True)
class DataConfig:
    seed: int
    scale: float
    start_date: date
    days: int
    out_dir: Path


@dataclass(frozen=True)
class PostgresConfig:
    mode: str
    bin_dir: Path | None
    host: str
    port: int
    user: str
    password: str
    database: str
    data_dir: Path
    settings: dict


@dataclass(frozen=True)
class MySQLConfig:
    mode: str
    bin_dir: Path | None
    host: str
    port: int
    user: str
    password: str
    database: str
    data_dir: Path
    binlog: bool
    settings: dict


@dataclass(frozen=True)
class DynamoConfig:
    mode: str
    endpoint: str
    region: str
    table: str
    local_dir: Path | None
    java: str
    heap: str
    in_memory: bool
    data_dir: Path
    sales_shards: int
    load_threads: int


@dataclass(frozen=True)
class CheckConfig:
    samples: int
    writes: int


@dataclass(frozen=True)
class BenchConfig:
    clients: int
    processes: int
    calls: int
    warmup: int


@dataclass(frozen=True)
class FlashConfig:
    clients: int
    processes: int
    stock: int
    buckets: int
    max_seconds: float
    strategies: tuple[str, ...]


@dataclass(frozen=True)
class IsolationConfig:
    step_wait: float


@dataclass(frozen=True)
class Config:
    root: Path
    raw: dict
    app: AppConfig
    data: DataConfig
    postgres: PostgresConfig
    mysql: MySQLConfig
    dynamodb: DynamoConfig
    check: CheckConfig
    bench: BenchConfig
    flash: FlashConfig
    isolation: IsolationConfig

    def with_overrides(self, items):
        raw = copy.deepcopy(self.raw)
        for item in items:
            apply_override(raw, item)
        return from_raw(raw, self.root)


class Section:
    def __init__(self, raw, name, root):
        value = raw.get(name)
        if not isinstance(value, dict):
            raise ConfigError(f"missing section [{name}]")
        self.name = name
        self.values = value
        self.root = root
        self.used = set()

    def _get(self, key):
        if key not in self.values:
            raise ConfigError(f"{self.name}.{key} is missing")
        self.used.add(key)
        return self.values[key]

    def _fail(self, key, message):
        raise ConfigError(f"{self.name}.{key} {message}")

    def integer(self, key, lo, hi):
        value = self._get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            self._fail(key, "must be an integer")
        if not lo <= value <= hi:
            self._fail(key, f"must be between {lo} and {hi}")
        return value

    def number(self, key, lo, hi):
        value = self._get(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            self._fail(key, "must be a number")
        value = float(value)
        if not lo <= value <= hi:
            self._fail(key, f"must be between {lo} and {hi}")
        return value

    def text(self, key, allow_empty=False):
        value = self._get(key)
        if not isinstance(value, str):
            self._fail(key, "must be a string")
        if not allow_empty and not value.strip():
            self._fail(key, "must not be empty")
        return value

    def flag(self, key):
        value = self._get(key)
        if not isinstance(value, bool):
            self._fail(key, "must be true or false")
        return value

    def choice(self, key, options):
        value = self.text(key)
        if value not in options:
            self._fail(key, f"must be one of {', '.join(options)}")
        return value

    def names(self, key, options):
        value = self._get(key)
        if not isinstance(value, list) or not value:
            self._fail(key, "must be a non-empty list")
        for item in value:
            if item not in options:
                self._fail(key, f"has unknown entry {item!r}")
        if len(set(value)) != len(value):
            self._fail(key, "has duplicate entries")
        return tuple(value)

    def path(self, key):
        value = Path(self.text(key))
        return value if value.is_absolute() else self.root / value

    def optional_path(self, key):
        text = self.text(key, allow_empty=True)
        if not text.strip():
            return None
        value = Path(text)
        return value if value.is_absolute() else self.root / value

    def day(self, key):
        value = self._get(key)
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError:
                pass
        self._fail(key, "must be a date like 2025-01-01")

    def settings(self, key):
        value = self._get(key)
        if not isinstance(value, dict):
            self._fail(key, "must be a table")
        for name, item in value.items():
            if not SETTING_NAME.match(name):
                self._fail(key, f"has invalid setting name {name!r}")
            if not isinstance(item, str | int | float | bool):
                self._fail(key, f"setting {name} must be a string, number or boolean")
        return dict(value)

    def finish(self):
        unknown = sorted(set(self.values) - self.used)
        if unknown:
            raise ConfigError(f"unknown keys in [{self.name}]: {', '.join(unknown)}")


def parse_value(text):
    try:
        return tomllib.loads(f"v = {text}")["v"]
    except tomllib.TOMLDecodeError:
        return text


def apply_override(raw, item):
    if "=" not in item:
        raise ConfigError(f"override {item!r} must look like section.key=value")
    name, text = item.split("=", 1)
    parts = name.strip().split(".")
    if len(parts) < 2 or not all(parts):
        raise ConfigError(f"override {item!r} must look like section.key=value")
    target = raw
    for part in parts[:-1]:
        target = target.setdefault(part, {})
        if not isinstance(target, dict):
            raise ConfigError(f"override {item!r} does not point at a table")
    target[parts[-1]] = parse_value(text.strip())


def endpoint(section, mode):
    value = section.text("endpoint", allow_empty=True)
    if mode != "aws" and not value.startswith(("http://", "https://")):
        section._fail("endpoint", "must be an http:// or https:// URL")
    return value


def from_raw(raw, root):
    root = Path(root)

    app = Section(raw, "app", root)
    app_cfg = AppConfig(
        engines=app.names("engines", ENGINES),
        low_stock=app.integer("low_stock", 1, 1000),
        results_dir=app.path("results_dir"),
    )
    app.finish()

    data = Section(raw, "data", root)
    data_cfg = DataConfig(
        seed=data.integer("seed", 0, 2**32 - 1),
        scale=data.number("scale", 0.001, 20.0),
        start_date=data.day("start_date"),
        days=data.integer("days", 30, 3660),
        out_dir=data.path("out_dir"),
    )
    data.finish()

    pg = Section(raw, "postgres", root)
    pg_cfg = PostgresConfig(
        mode=pg.choice("mode", SQL_MODES),
        bin_dir=pg.optional_path("bin_dir"),
        host=pg.text("host"),
        port=pg.integer("port", 1, 65535),
        user=pg.text("user"),
        password=pg.text("password", allow_empty=True),
        database=pg.text("database"),
        data_dir=pg.path("data_dir"),
        settings=pg.settings("settings"),
    )
    pg.finish()

    my = Section(raw, "mysql", root)
    my_cfg = MySQLConfig(
        mode=my.choice("mode", SQL_MODES),
        bin_dir=my.optional_path("bin_dir"),
        host=my.text("host"),
        port=my.integer("port", 1, 65535),
        user=my.text("user"),
        password=my.text("password", allow_empty=True),
        database=my.text("database"),
        data_dir=my.path("data_dir"),
        binlog=my.flag("binlog"),
        settings=my.settings("settings"),
    )
    my.finish()

    dy = Section(raw, "dynamodb", root)
    mode = dy.choice("mode", DYNAMO_MODES)
    heap = dy.text("heap")
    if not HEAP_SIZE.match(heap):
        dy._fail("heap", "must look like 512m or 1g")
    dy_cfg = DynamoConfig(
        mode=mode,
        endpoint=endpoint(dy, mode),
        region=dy.text("region"),
        table=dy.text("table"),
        local_dir=dy.optional_path("local_dir"),
        java=dy.text("java"),
        heap=heap,
        in_memory=dy.flag("in_memory"),
        data_dir=dy.path("data_dir"),
        sales_shards=dy.integer("sales_shards", 1, 64),
        load_threads=dy.integer("load_threads", 1, 64),
    )
    dy.finish()

    ck = Section(raw, "check", root)
    check_cfg = CheckConfig(
        samples=ck.integer("samples", 1, 1_000_000),
        writes=ck.integer("writes", 0, 1_000_000),
    )
    ck.finish()

    bn = Section(raw, "bench", root)
    bench_cfg = BenchConfig(
        clients=bn.integer("clients", 1, 512),
        processes=bn.integer("processes", 1, 64),
        calls=bn.integer("calls", 1, 10_000_000),
        warmup=bn.integer("warmup", 0, 1_000_000),
    )
    if bench_cfg.processes > bench_cfg.clients:
        bn._fail("processes", "must not exceed bench.clients")
    bn.finish()

    fl = Section(raw, "flash", root)
    flash_cfg = FlashConfig(
        clients=fl.integer("clients", 1, 1024),
        processes=fl.integer("processes", 1, 64),
        stock=fl.integer("stock", 1, 10_000_000),
        buckets=fl.integer("buckets", 1, 1024),
        max_seconds=fl.number("max_seconds", 1.0, 3600.0),
        strategies=fl.names("strategies", STRATEGIES),
    )
    if flash_cfg.processes > flash_cfg.clients:
        fl._fail("processes", "must not exceed flash.clients")
    if flash_cfg.buckets > flash_cfg.stock:
        fl._fail("buckets", "must not exceed flash.stock")
    fl.finish()

    iso = Section(raw, "isolation", root)
    iso_cfg = IsolationConfig(step_wait=iso.number("step_wait", 0.1, 30.0))
    iso.finish()

    unknown = sorted(
        set(raw)
        - {"app", "data", "postgres", "mysql", "dynamodb", "check", "bench", "flash", "isolation"}
    )
    if unknown:
        raise ConfigError(f"unknown sections: {', '.join(unknown)}")

    return Config(
        root=root,
        raw=raw,
        app=app_cfg,
        data=data_cfg,
        postgres=pg_cfg,
        mysql=my_cfg,
        dynamodb=dy_cfg,
        check=check_cfg,
        bench=bench_cfg,
        flash=flash_cfg,
        isolation=iso_cfg,
    )


def load(path=None, overrides=()):
    path = Path(path) if path else Path.cwd() / "config.toml"
    if not path.exists():
        raise ConfigError(f"{path} not found")
    with open(path, "rb") as handle:
        try:
            raw = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: {exc}") from exc
    items = [item for item in os.environ.get("ECOMDB_SET", "").split(";") if item.strip()]
    for item in [*items, *overrides]:
        apply_override(raw, item)
    return from_raw(raw, path.resolve().parent)

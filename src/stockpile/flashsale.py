import multiprocessing as mp
import os
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

import MySQLdb
import numpy as np
import psycopg
from botocore.exceptions import ClientError

from . import config as config_module
from .db import dynamo_client, mysql_connect, pg_connect
from .ddb import n, plain, s
from .servers import WINDOWS

SQL_STRATEGIES = ("naive", "row_lock", "conditional", "version", "serializable", "buckets")
DYNAMO_STRATEGIES = ("naive", "conditional", "version", "transaction", "buckets")
SOLD = "sold"
SOLD_OUT = "sold_out"
ORDER_BASE = 1_000_000_000

PG_TABLES = """
DROP TABLE IF EXISTS sale_orders;
DROP TABLE IF EXISTS sale_stock;
DROP SEQUENCE IF EXISTS sale_order_ids;
CREATE SEQUENCE sale_order_ids;
CREATE TABLE sale_stock (
    item_id integer NOT NULL,
    bucket integer NOT NULL,
    available integer NOT NULL CHECK (available >= 0),
    version integer NOT NULL DEFAULT 0,
    PRIMARY KEY (item_id, bucket)
);
CREATE TABLE sale_orders (
    order_id bigint PRIMARY KEY,
    item_id integer NOT NULL,
    bucket integer NOT NULL,
    customer_id bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
"""

MYSQL_TABLES = (
    "DROP TABLE IF EXISTS sale_orders",
    "DROP TABLE IF EXISTS sale_stock",
    """CREATE TABLE sale_stock (
    item_id INT NOT NULL,
    bucket INT NOT NULL,
    available INT NOT NULL CHECK (available >= 0),
    version INT NOT NULL DEFAULT 0,
    PRIMARY KEY (item_id, bucket)
)""",
    """CREATE TABLE sale_orders (
    order_id BIGINT PRIMARY KEY,
    item_id INT NOT NULL,
    bucket INT NOT NULL,
    customer_id BIGINT NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
)""",
)

PGBENCH_SCRIPT = """\\set cust random(1, 1000000)
BEGIN;
WITH claimed AS (
    UPDATE sale_stock SET available = available - 1
    WHERE item_id = 1 AND bucket = 0 AND available >= 1
    RETURNING 1
)
SELECT count(*) AS claimed FROM claimed \\gset
\\if :claimed
INSERT INTO sale_orders (order_id, item_id, bucket, customer_id)
VALUES (nextval('sale_order_ids'), 1, 0, :cust);
\\endif
COMMIT;
"""


def split_stock(stock, buckets):
    return [stock // buckets + (1 if i < stock % buckets else 0) for i in range(buckets)]


def strategies_for(engine, wanted):
    allowed = DYNAMO_STRATEGIES if engine == "dynamodb" else SQL_STRATEGIES
    return [name for name in wanted if name in allowed]


class SqlSale:
    def __init__(self, cfg, engine, strategy, worker):
        self.engine = engine
        self.bucket_count = cfg.flash.buckets
        self.next_order = worker * ORDER_BASE
        self.rng = random.Random(worker)
        if engine == "postgres":
            self.conn = pg_connect(cfg.postgres, autocommit=False)
            if strategy == "serializable":
                self.conn.isolation_level = psycopg.IsolationLevel.SERIALIZABLE
        else:
            self.conn = mysql_connect(cfg.mysql, autocommit=False)
            if strategy == "serializable":
                self.conn.cursor().execute("SET SESSION TRANSACTION ISOLATION LEVEL SERIALIZABLE")
        self.cur = self.conn.cursor()
        self.strategy = getattr(self, strategy)
        self.requests = 0

    def close(self):
        self.conn.close()

    def run(self, statement, params=()):
        self.requests += 1
        self.cur.execute(statement, params)

    def one(self, statement, params=()):
        self.run(statement, params)
        return self.cur.fetchone()

    def commit(self):
        self.requests += 1
        self.conn.commit()

    def rollback(self):
        self.requests += 1
        try:
            self.conn.rollback()
        except Exception:
            pass

    def insert_order(self, bucket):
        self.next_order += 1
        self.run(
            "INSERT INTO sale_orders (order_id, item_id, bucket, customer_id) "
            "VALUES (%s, 1, %s, %s)",
            (self.next_order, bucket, self.rng.randint(1, 1_000_000)),
        )

    def retry_reason(self, exc):
        if self.engine == "postgres":
            if isinstance(exc, psycopg.errors.SerializationFailure):
                return "serialization"
            if isinstance(exc, psycopg.errors.DeadlockDetected):
                return "deadlock"
            if isinstance(exc, psycopg.errors.LockNotAvailable):
                return "lock_timeout"
            return None
        code = exc.args[0] if isinstance(exc, MySQLdb.MySQLError) and exc.args else None
        return {1213: "deadlock", 1205: "lock_timeout"}.get(code)

    def attempt(self):
        try:
            return self.strategy()
        except (psycopg.Error, MySQLdb.MySQLError) as exc:
            self.rollback()
            reason = self.retry_reason(exc)
            if reason is None:
                raise
            return reason

    def naive(self):
        available = self.one("SELECT available FROM sale_stock WHERE item_id = 1 AND bucket = 0")[0]
        if available < 1:
            self.rollback()
            return SOLD_OUT
        self.run(
            "UPDATE sale_stock SET available = %s WHERE item_id = 1 AND bucket = 0",
            (available - 1,),
        )
        self.insert_order(0)
        self.commit()
        return SOLD

    serializable = naive

    def row_lock(self):
        available = self.one(
            "SELECT available FROM sale_stock WHERE item_id = 1 AND bucket = 0 FOR UPDATE"
        )[0]
        if available < 1:
            self.rollback()
            return SOLD_OUT
        self.run("UPDATE sale_stock SET available = available - 1 WHERE item_id = 1 AND bucket = 0")
        self.insert_order(0)
        self.commit()
        return SOLD

    def conditional(self):
        self.run(
            "UPDATE sale_stock SET available = available - 1 "
            "WHERE item_id = 1 AND bucket = 0 AND available >= 1"
        )
        if self.cur.rowcount == 0:
            self.rollback()
            return SOLD_OUT
        self.insert_order(0)
        self.commit()
        return SOLD

    def version(self):
        available, version = self.one(
            "SELECT available, version FROM sale_stock WHERE item_id = 1 AND bucket = 0"
        )
        if available < 1:
            self.rollback()
            return SOLD_OUT
        self.run(
            "UPDATE sale_stock SET available = available - 1, version = version + 1 "
            "WHERE item_id = 1 AND bucket = 0 AND version = %s",
            (version,),
        )
        if self.cur.rowcount == 0:
            self.rollback()
            return "conflict"
        self.insert_order(0)
        self.commit()
        return SOLD

    def buckets(self):
        row = self.one(
            "SELECT bucket FROM sale_stock WHERE item_id = 1 AND available > 0 "
            "ORDER BY bucket LIMIT 1 FOR UPDATE SKIP LOCKED"
        )
        if row is None:
            left = self.one("SELECT COUNT(*) FROM sale_stock WHERE item_id = 1 AND available > 0")
            self.rollback()
            return SOLD_OUT if left[0] == 0 else "busy"
        self.run(
            "UPDATE sale_stock SET available = available - 1 WHERE item_id = 1 AND bucket = %s",
            (row[0],),
        )
        self.insert_order(row[0])
        self.commit()
        return SOLD


class DynamoSale:
    def __init__(self, cfg, engine, strategy, worker):
        self.client = dynamo_client(cfg.dynamodb, attempts=1, pool=8)
        self.table = flash_table(cfg)
        self.bucket_count = cfg.flash.buckets
        self.next_order = worker * ORDER_BASE
        self.rng = random.Random(worker)
        self.empty = set()
        self.strategy = getattr(self, strategy)
        self.requests = 0
        self.capacity = 0.0

    def close(self):
        closer = getattr(self.client, "close", None)
        if closer:
            closer()

    def call(self, operation, **kwargs):
        self.requests += 1
        kwargs.setdefault("ReturnConsumedCapacity", "TOTAL")
        response = getattr(self.client, operation)(**kwargs)
        consumed = response.get("ConsumedCapacity")
        if isinstance(consumed, dict):
            consumed = [consumed]
        for entry in consumed or ():
            self.capacity += float(entry.get("CapacityUnits", 0.0))
        return response

    def stock_key(self, bucket):
        return {"PK": s("STOCK"), "SK": s(f"B#{bucket:04d}")}

    def order_item(self, bucket):
        self.next_order += 1
        return {
            "PK": s(f"ORDER#{self.next_order}"),
            "SK": s("META"),
            "bucket": n(bucket),
            "customer_id": n(self.rng.randint(1, 1_000_000)),
        }

    def put_order(self, bucket):
        self.call("put_item", TableName=self.table, Item=self.order_item(bucket))

    def read_stock(self, bucket):
        item = self.call(
            "get_item", TableName=self.table, Key=self.stock_key(bucket), ConsistentRead=True
        )
        return plain(item["Item"])

    def attempt(self):
        try:
            return self.strategy()
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("ProvisionedThroughputExceededException", "ThrottlingException"):
                return "throttled"
            if code == "TransactionConflictException":
                return "conflict"
            raise

    def decrement(self, bucket):
        try:
            self.call(
                "update_item",
                TableName=self.table,
                Key=self.stock_key(bucket),
                UpdateExpression="SET available = available - :one",
                ConditionExpression="available >= :one",
                ExpressionAttributeValues={":one": n(1)},
            )
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    def naive(self):
        available = self.read_stock(0)["available"]
        if available < 1:
            return SOLD_OUT
        self.call(
            "update_item",
            TableName=self.table,
            Key=self.stock_key(0),
            UpdateExpression="SET available = :value",
            ExpressionAttributeValues={":value": n(available - 1)},
        )
        self.put_order(0)
        return SOLD

    def conditional(self):
        if not self.decrement(0):
            return SOLD_OUT
        self.put_order(0)
        return SOLD

    def version(self):
        stock = self.read_stock(0)
        if stock["available"] < 1:
            return SOLD_OUT
        try:
            self.call(
                "update_item",
                TableName=self.table,
                Key=self.stock_key(0),
                UpdateExpression="SET available = :left, version = :next",
                ConditionExpression="version = :seen",
                ExpressionAttributeValues={
                    ":left": n(stock["available"] - 1),
                    ":next": n(stock["version"] + 1),
                    ":seen": n(stock["version"]),
                },
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return "conflict"
            raise
        self.put_order(0)
        return SOLD

    def transaction(self):
        actions = [
            {
                "Update": {
                    "TableName": self.table,
                    "Key": self.stock_key(0),
                    "UpdateExpression": "SET available = available - :one",
                    "ConditionExpression": "available >= :one",
                    "ExpressionAttributeValues": {":one": n(1)},
                }
            },
            {
                "Put": {
                    "TableName": self.table,
                    "Item": self.order_item(0),
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            },
        ]
        try:
            self.call("transact_write_items", TransactItems=actions)
            return SOLD
        except ClientError as exc:
            error = exc.response.get("Error", {})
            if error.get("Code") != "TransactionCanceledException":
                raise
            reasons = exc.response.get("CancellationReasons") or []
            codes = [r.get("Code") for r in reasons]
            message = error.get("Message", "")
            if (codes and codes[0] == "ConditionalCheckFailed") or (
                not codes and "ConditionalCheckFailed" in message
            ):
                return SOLD_OUT
            return "conflict"

    def buckets(self):
        order = [b for b in range(self.bucket_count) if b not in self.empty]
        self.rng.shuffle(order)
        for bucket in order:
            if self.decrement(bucket):
                self.put_order(bucket)
                return SOLD
            self.empty.add(bucket)
        return SOLD_OUT


def flash_table(cfg):
    return f"{cfg.dynamodb.table}_flash"


def reset(cfg, engine, strategy, stock):
    buckets = cfg.flash.buckets if strategy == "buckets" else 1
    amounts = split_stock(stock, buckets)
    if engine == "postgres":
        with pg_connect(cfg.postgres) as conn:
            conn.execute(PG_TABLES)
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO sale_stock (item_id, bucket, available) VALUES (1, %s, %s)",
                    list(enumerate(amounts)),
                )
    elif engine == "mysql":
        conn = mysql_connect(cfg.mysql)
        try:
            cur = conn.cursor()
            for statement in MYSQL_TABLES:
                cur.execute(statement)
            cur.executemany(
                "INSERT INTO sale_stock (item_id, bucket, available) VALUES (1, %s, %s)",
                list(enumerate(amounts)),
            )
        finally:
            conn.close()
    else:
        client = dynamo_client(cfg.dynamodb, attempts=10)
        name = flash_table(cfg)
        try:
            client.delete_table(TableName=name)
            client.get_waiter("table_not_exists").wait(TableName=name, WaiterConfig={"Delay": 1})
        except client.exceptions.ResourceNotFoundException:
            pass
        client.create_table(
            TableName=name,
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        client.get_waiter("table_exists").wait(TableName=name, WaiterConfig={"Delay": 1})
        for bucket, amount in enumerate(amounts):
            client.put_item(
                TableName=name,
                Item={
                    "PK": s("STOCK"),
                    "SK": s(f"B#{bucket:04d}"),
                    "available": n(amount),
                    "version": n(0),
                },
            )


def outcome_counts(cfg, engine):
    if engine == "postgres":
        with pg_connect(cfg.postgres) as conn:
            sold, first, last = conn.execute(
                "SELECT count(*), min(created_at), max(created_at) FROM sale_orders"
            ).fetchone()
            left = conn.execute("SELECT coalesce(sum(available), 0) FROM sale_stock").fetchone()[0]
        span = (last - first).total_seconds() if sold > 1 else 0.0
        return int(sold), int(left), span
    if engine == "mysql":
        conn = mysql_connect(cfg.mysql)
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM sale_orders")
            sold, first, last = cur.fetchone()
            cur.execute("SELECT COALESCE(SUM(available), 0) FROM sale_stock")
            left = cur.fetchone()[0]
        finally:
            conn.close()
        span = (last - first).total_seconds() if sold > 1 else 0.0
        return int(sold), int(left), span
    client = dynamo_client(cfg.dynamodb, attempts=10)
    sold = 0
    left = 0
    kwargs = {"TableName": flash_table(cfg)}
    while True:
        response = client.scan(**kwargs)
        for item in response.get("Items", []):
            item = plain(item)
            if item["PK"] == "STOCK":
                left += item["available"]
            else:
                sold += 1
        if "LastEvaluatedKey" not in response:
            break
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
    return sold, left, None


def make_sale(cfg, engine, strategy, worker):
    if engine == "dynamodb":
        return DynamoSale(cfg, engine, strategy, worker)
    return SqlSale(cfg, engine, strategy, worker)


def client_loop(sale, go, max_seconds, stats):
    go.wait()
    deadline = time.perf_counter() + max_seconds
    while True:
        started = time.perf_counter()
        while True:
            outcome = sale.attempt()
            stats["attempts"] += 1
            if outcome == SOLD:
                stats["latencies"].append(time.perf_counter() - started)
                stats["sold"] += 1
                break
            if outcome == SOLD_OUT:
                stats["done"] = time.time()
                return
            stats["retries"][outcome] += 1
            if outcome == "busy":
                time.sleep(0.0005)
            if time.perf_counter() >= deadline:
                stats["done"] = time.time()
                stats["timed_out"] = True
                return
        if time.perf_counter() >= deadline:
            stats["done"] = time.time()
            stats["timed_out"] = True
            return


def process_main(raw, root, engine, strategy, first_worker, threads, max_seconds, ready, go, out):
    cfg = config_module.from_raw(raw, Path(root))
    sales = [make_sale(cfg, engine, strategy, first_worker + i) for i in range(threads)]
    stats = [
        {
            "attempts": 0,
            "sold": 0,
            "latencies": [],
            "retries": Counter(),
            "done": None,
            "timed_out": False,
        }
        for _ in sales
    ]
    start_gate = threading.Event()
    workers = [
        threading.Thread(target=client_loop, args=(sale, start_gate, max_seconds, st))
        for sale, st in zip(sales, stats, strict=True)
    ]
    for worker in workers:
        worker.start()
    ready.put(threads)
    go.wait()
    start_gate.set()
    for worker in workers:
        worker.join()
    requests = sum(sale.requests for sale in sales)
    capacity = sum(getattr(sale, "capacity", 0.0) for sale in sales)
    for sale in sales:
        sale.close()
    out.put(
        {
            "attempts": sum(st["attempts"] for st in stats),
            "sold": sum(st["sold"] for st in stats),
            "latencies": [x for st in stats for x in st["latencies"]],
            "retries": dict(sum((st["retries"] for st in stats), Counter())),
            "done": max(st["done"] or 0.0 for st in stats),
            "timed_out": sum(1 for st in stats if st["timed_out"]),
            "requests": requests,
            "capacity": capacity,
        }
    )


def run_clients(cfg, engine, strategy, clients, processes, max_seconds):
    ctx = mp.get_context("spawn")
    ready = ctx.Queue()
    out = ctx.Queue()
    go = ctx.Event()
    per = [clients // processes + (1 if i < clients % processes else 0) for i in range(processes)]
    procs = []
    first = 1
    for count in per:
        args = (cfg.raw, str(cfg.root), engine, strategy, first, count, max_seconds, ready, go, out)
        proc = ctx.Process(target=process_main, args=args, daemon=True)
        proc.start()
        procs.append(proc)
        first += count
    for _ in procs:
        ready.get(timeout=120)
    started = time.time()
    go.set()
    parts = [out.get(timeout=max_seconds + 300) for _ in procs]
    for proc in procs:
        proc.join(timeout=60)
    latencies = np.array([x for p in parts for x in p["latencies"]])
    retries = Counter()
    for p in parts:
        retries.update(p["retries"])
    sold = sum(p["sold"] for p in parts)
    seconds = max(p["done"] for p in parts) - started
    return {
        "clients": clients,
        "attempts": sum(p["attempts"] for p in parts),
        "client_sold": sold,
        "seconds": seconds,
        "per_second": sold / seconds if seconds > 0 else 0.0,
        "p50_ms": float(np.percentile(latencies, 50) * 1000) if len(latencies) else None,
        "p95_ms": float(np.percentile(latencies, 95) * 1000) if len(latencies) else None,
        "p99_ms": float(np.percentile(latencies, 99) * 1000) if len(latencies) else None,
        "retries": dict(retries),
        "timed_out": sum(p["timed_out"] for p in parts),
        "requests": sum(p["requests"] for p in parts),
        "capacity": sum(p["capacity"] for p in parts),
    }


def run_strategy(cfg, engine, strategy, stock=None, max_seconds=None):
    stock = stock or cfg.flash.stock
    reset(cfg, engine, strategy, stock)
    result = run_clients(
        cfg,
        engine,
        strategy,
        cfg.flash.clients,
        cfg.flash.processes,
        max_seconds or cfg.flash.max_seconds,
    )
    sold, left, span = outcome_counts(cfg, engine)
    result.update(
        {
            "engine": engine,
            "strategy": strategy,
            "stock": stock,
            "sold": sold,
            "left": left,
            "oversold": max(0, sold - (stock - left)),
            "db_per_second": sold / span if span else None,
            "requests_per_sale": result["requests"] / sold if sold else None,
            "capacity_per_sale": result["capacity"] / sold if sold else None,
        }
    )
    return result


def pgbench_binary(cfg):
    name = "pgbench.exe" if WINDOWS else "pgbench"
    if cfg.postgres.bin_dir and (cfg.postgres.bin_dir / name).exists():
        return str(cfg.postgres.bin_dir / name)
    return shutil.which("pgbench")


def pgbench_check(cfg, seconds=10):
    binary = pgbench_binary(cfg)
    if binary is None:
        return None
    pg = cfg.postgres
    reset(cfg, "postgres", "conditional", 1_000_000_000)
    with tempfile.TemporaryDirectory() as folder:
        script = Path(folder) / "flash.sql"
        script.write_text(PGBENCH_SCRIPT, encoding="utf-8")
        args = [
            binary,
            "-n",
            "-c",
            str(cfg.flash.clients),
            "-j",
            str(min(cfg.flash.clients, cfg.flash.processes * 2)),
            "-T",
            str(seconds),
            "-f",
            str(script),
            "-h",
            pg.host,
            "-p",
            str(pg.port),
            "-U",
            pg.user,
            pg.database,
        ]
        env = None
        if pg.password:
            env = {**os.environ, "PGPASSWORD": pg.password}
        output = subprocess.run(
            args, capture_output=True, text=True, env=env, timeout=seconds + 120
        )
    match = re.search(r"tps = ([0-9.]+) \(without initial connection time\)", output.stdout)
    if output.returncode != 0 or not match:
        return {"error": (output.stderr or output.stdout).strip()[-500:]}
    harness = run_strategy(cfg, "postgres", "conditional", stock=1_000_000_000, max_seconds=seconds)
    return {
        "pgbench_tps": float(match.group(1)),
        "harness_tps": harness["per_second"],
        "seconds": seconds,
    }


def run_flash(cfg, engines, log=print):
    results = {}
    for engine in engines:
        results[engine] = {}
        for strategy in strategies_for(engine, cfg.flash.strategies):
            result = run_strategy(cfg, engine, strategy)
            results[engine][strategy] = result
            log(
                f"flash {engine} {strategy}: sold {result['sold']} of {result['stock']}, "
                f"oversold {result['oversold']}, {result['per_second']:.0f}/s, "
                f"p99 {result['p99_ms'] or 0:.1f} ms, retries {result['retries']}"
            )
    return results

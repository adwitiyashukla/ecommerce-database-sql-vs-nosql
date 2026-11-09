import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field

import MySQLdb
import psycopg
from botocore.exceptions import ClientError

from .db import dynamo_client, mysql_connect, pg_connect
from .ddb import n, plain, s

LEVELS = ("READ UNCOMMITTED", "READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE")

TABLES = {
    "postgres": (
        "DROP TABLE IF EXISTS iso_stock, iso_listing, iso_redemption, iso_price",
        "CREATE TABLE iso_stock (item_id integer PRIMARY KEY, available integer NOT NULL)",
        "CREATE TABLE iso_listing (sku_id integer, seller_id integer, active integer NOT NULL, "
        "PRIMARY KEY (sku_id, seller_id))",
        "CREATE TABLE iso_redemption (customer_id integer, coupon varchar(16), nonce integer, "
        "PRIMARY KEY (customer_id, coupon, nonce))",
        "CREATE TABLE iso_price (item_id integer PRIMARY KEY, price integer NOT NULL)",
    ),
    "mysql": (
        "DROP TABLE IF EXISTS iso_stock, iso_listing, iso_redemption, iso_price",
        "CREATE TABLE iso_stock (item_id INT PRIMARY KEY, available INT NOT NULL)",
        "CREATE TABLE iso_listing (sku_id INT, seller_id INT, active INT NOT NULL, "
        "PRIMARY KEY (sku_id, seller_id))",
        "CREATE TABLE iso_redemption (customer_id INT, coupon VARCHAR(16), nonce INT, "
        "PRIMARY KEY (customer_id, coupon, nonce))",
        "CREATE TABLE iso_price (item_id INT PRIMARY KEY, price INT NOT NULL)",
    ),
}

SEED = (
    "INSERT INTO iso_stock VALUES (1, 10)",
    "INSERT INTO iso_listing VALUES (1, 1, 1), (1, 2, 1)",
    "INSERT INTO iso_redemption VALUES (5, 'SAVE10', 0), (9, 'SAVE10', 0)",
    "INSERT INTO iso_price VALUES (1, 100)",
)

STOCK = "SELECT available FROM iso_stock WHERE item_id = 1"
ACTIVE = "SELECT COUNT(*) FROM iso_listing WHERE sku_id = 1 AND active = 1"
REDEEMED = "SELECT COUNT(*) FROM iso_redemption WHERE customer_id = 7 AND coupon = 'SAVE10'"
PRICE = "SELECT price FROM iso_price WHERE item_id = 1"


@dataclass
class Step:
    session: str
    kind: str
    sql: str = ""
    params: object = ()
    label: str = ""


@dataclass
class Scenario:
    name: str
    title: str
    steps: list
    check: str = ""
    reads: bool = False
    verdict: object = None
    extra: dict = field(default_factory=dict)


def lost_update(locking):
    read = STOCK + (" FOR UPDATE" if locking else "")
    write = "UPDATE iso_stock SET available = %s WHERE item_id = 1"
    return Scenario(
        name="lost_update_locking" if locking else "lost_update",
        title="lost update, read with FOR UPDATE" if locking else "lost update, plain read",
        steps=[
            Step("A", "begin"),
            Step("A", "read", read, label="a"),
            Step("B", "begin"),
            Step("B", "read", read, label="b"),
            Step("A", "write", write, lambda r: (r["a"] - 1,)),
            Step("A", "commit"),
            Step("B", "write", write, lambda r: (r["b"] - 1,)),
            Step("B", "commit"),
        ],
        check=STOCK,
        verdict=lambda final, ok, reads: final != 10 - len(ok),
    )


def write_skew():
    return Scenario(
        name="write_skew",
        title="write skew, two sellers delist the last listings",
        steps=[
            Step("A", "begin"),
            Step("A", "read", ACTIVE, label="a"),
            Step("B", "begin"),
            Step("B", "read", ACTIVE, label="b"),
            Step(
                "A", "write", "UPDATE iso_listing SET active = 0 WHERE sku_id = 1 AND seller_id = 1"
            ),
            Step(
                "B", "write", "UPDATE iso_listing SET active = 0 WHERE sku_id = 1 AND seller_id = 2"
            ),
            Step("A", "commit"),
            Step("B", "commit"),
        ],
        check=ACTIVE,
        verdict=lambda final, ok, reads: len(ok) == 2 and final == 0,
    )


def phantom(locking):
    read = REDEEMED
    kind = "read"
    if locking:
        read = (
            "SELECT nonce FROM iso_redemption WHERE customer_id = 7 AND coupon = 'SAVE10' "
            "FOR UPDATE"
        )
        kind = "rows"
    insert = "INSERT INTO iso_redemption VALUES (7, 'SAVE10', %s)"
    return Scenario(
        name="phantom_locking" if locking else "phantom",
        title="phantom, coupon check with FOR UPDATE" if locking else "phantom, coupon check",
        steps=[
            Step("A", "begin"),
            Step("A", kind, read, label="a"),
            Step("B", "begin"),
            Step("B", kind, read, label="b"),
            Step("A", "write", insert, (1,)),
            Step("B", "write", insert, (2,)),
            Step("A", "commit"),
            Step("B", "commit"),
        ],
        check=REDEEMED,
        verdict=lambda final, ok, reads: len(ok) == 2 and final == 2,
    )


def non_repeatable_read():
    return Scenario(
        name="non_repeatable_read",
        title="non-repeatable read of a price",
        steps=[
            Step("A", "begin"),
            Step("A", "read", PRICE, label="first"),
            Step("B", "begin"),
            Step("B", "write", "UPDATE iso_price SET price = 120 WHERE item_id = 1"),
            Step("B", "commit"),
            Step("A", "read", PRICE, label="second"),
            Step("A", "commit"),
        ],
        reads=True,
        verdict=lambda final, ok, reads: reads.get("first") != reads.get("second"),
    )


def dirty_read():
    return Scenario(
        name="dirty_read",
        title="dirty read of an uncommitted price",
        steps=[
            Step("B", "begin"),
            Step("B", "write", "UPDATE iso_price SET price = 999 WHERE item_id = 1"),
            Step("A", "begin"),
            Step("A", "read", PRICE, label="seen"),
            Step("B", "rollback"),
            Step("A", "commit"),
        ],
        reads=True,
        verdict=lambda final, ok, reads: reads.get("seen") == 999,
    )


def scenarios():
    return [
        lost_update(False),
        lost_update(True),
        write_skew(),
        phantom(False),
        phantom(True),
        non_repeatable_read(),
        dirty_read(),
    ]


class Session:
    def __init__(self, cfg, engine):
        self.engine = engine
        if engine == "postgres":
            self.conn = pg_connect(cfg.postgres, autocommit=True)
            self.conn.execute("SET lock_timeout = '10s'")
            try:
                self.conn.execute("SET deadlock_timeout = '200ms'")
            except psycopg.Error:
                pass
        else:
            self.conn = mysql_connect(cfg.mysql, autocommit=True)
            self.conn.cursor().execute("SET SESSION innodb_lock_wait_timeout = 10")
        self.pool = ThreadPoolExecutor(max_workers=1)

    def execute(self, statement, params=()):
        cur = self.conn.cursor()
        try:
            cur.execute(statement, params or None)
            return cur.fetchall() if cur.description else None
        finally:
            cur.close()

    def begin(self, level):
        if self.engine == "postgres":
            self.execute(f"BEGIN ISOLATION LEVEL {level}")
        else:
            self.execute(f"SET SESSION TRANSACTION ISOLATION LEVEL {level}")
            self.execute("START TRANSACTION")

    def close(self):
        try:
            self.execute("ROLLBACK")
        except Exception:
            pass
        self.pool.shutdown(wait=False)
        self.conn.close()


def error_kind(engine, exc):
    if engine == "postgres":
        if isinstance(exc, psycopg.errors.SerializationFailure):
            return "serialization error"
        if isinstance(exc, psycopg.errors.DeadlockDetected):
            return "deadlock error"
        if isinstance(exc, psycopg.errors.LockNotAvailable):
            return "lock timeout"
        return None
    code = exc.args[0] if isinstance(exc, MySQLdb.MySQLError) and exc.args else None
    return {1213: "deadlock error", 1205: "lock timeout"}.get(code)


def prepare(cfg, engine):
    if engine == "postgres":
        conn = pg_connect(cfg.postgres)
    else:
        conn = mysql_connect(cfg.mysql)
    cur = conn.cursor()
    for statement in (*TABLES[engine], *SEED):
        cur.execute(statement)
    cur.close()
    return conn


def run_scenario(cfg, engine, scenario, level, wait):
    admin = prepare(cfg, engine)
    sessions = {"A": Session(cfg, engine), "B": Session(cfg, engine)}
    queues = {"A": [], "B": []}
    in_flight = {}
    reads = {}
    errors = {}
    committed = set()
    waited = False

    def task(name, step):
        session = sessions[name]
        if step.kind == "begin":
            session.begin(level)
            return None
        if step.kind in ("commit", "rollback"):
            session.execute(step.kind.upper())
            return None
        params = step.params(reads) if callable(step.params) else step.params
        return session.execute(step.sql, params)

    def finish(name, step, future):
        try:
            rows = future.result()
        except Exception as exc:
            kind = error_kind(engine, exc)
            if kind is None:
                raise
            errors[name] = kind
            try:
                sessions[name].execute("ROLLBACK")
            except Exception:
                pass
            return
        if step.kind == "read" and rows:
            reads[step.label] = rows[0][0]
        if step.kind == "rows":
            reads[step.label] = len(rows or [])
        if step.kind == "commit":
            committed.add(name)

    def pump(name, timeout):
        nonlocal waited
        while True:
            if name in in_flight:
                step, future = in_flight[name]
                try:
                    future.result(timeout=timeout)
                except FutureTimeout:
                    waited = True
                    return
                except Exception:
                    pass
                del in_flight[name]
                finish(name, step, future)
            if not queues[name]:
                return
            step = queues[name].pop(0)
            if name in errors and step.kind not in ("commit", "rollback"):
                continue
            if name in errors:
                continue
            in_flight[name] = (step, sessions[name].pool.submit(task, name, step))

    try:
        for step in scenario.steps:
            queues[step.session].append(step)
            pump(step.session, wait)
            for other in sessions:
                if other != step.session:
                    pump(other, 0.05)
        deadline = time.monotonic() + wait * 15
        while (in_flight or any(queues.values())) and time.monotonic() < deadline:
            for name in sessions:
                pump(name, wait)
        if in_flight or any(queues.values()):
            raise RuntimeError(f"{engine} {scenario.name} at {level} did not finish")
        final = None
        if scenario.check:
            cur = admin.cursor()
            cur.execute(scenario.check)
            final = cur.fetchone()[0]
            cur.close()
    finally:
        for session in sessions.values():
            session.close()
        admin.close()
    anomaly = scenario.verdict(final, committed, reads)
    if anomaly:
        cell = "anomaly"
    elif errors:
        cell = sorted(set(errors.values()))[0]
    elif waited:
        cell = "lock wait"
    else:
        cell = "prevented"
    return {"cell": cell, "errors": errors, "reads": reads, "final": final, "waited": waited}


def run_sql_matrix(cfg, engine, log=print):
    matrix = {}
    for scenario in scenarios():
        row = {}
        for level in LEVELS:
            row[level] = run_scenario(cfg, engine, scenario, level, cfg.isolation.step_wait)["cell"]
        matrix[scenario.name] = row
        log(f"isolation {engine} {scenario.name}: {row}")
    return matrix


class DynamoLab:
    def __init__(self, cfg):
        self.client = dynamo_client(cfg.dynamodb, attempts=3)
        self.table = f"{cfg.dynamodb.table}_iso"

    def reset(self):
        try:
            self.client.delete_table(TableName=self.table)
            self.client.get_waiter("table_not_exists").wait(
                TableName=self.table, WaiterConfig={"Delay": 1}
            )
        except self.client.exceptions.ResourceNotFoundException:
            pass
        self.client.create_table(
            TableName=self.table,
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
        self.client.get_waiter("table_exists").wait(TableName=self.table, WaiterConfig={"Delay": 1})
        self.put({"PK": s("STOCK#1"), "SK": s("META"), "available": n(10), "version": n(0)})
        for seller in (1, 2):
            self.put({"PK": s("LISTING#1"), "SK": s(f"SELLER#{seller}"), "active": n(1)})

    def put(self, item, condition=None):
        kwargs = {"TableName": self.table, "Item": item}
        if condition:
            kwargs["ConditionExpression"] = condition
        self.client.put_item(**kwargs)

    def get(self, pk, sk):
        response = self.client.get_item(
            TableName=self.table, Key={"PK": s(pk), "SK": s(sk)}, ConsistentRead=True
        )
        return plain(response["Item"])

    def active(self):
        response = self.client.query(
            TableName=self.table,
            KeyConditionExpression="PK = :pk",
            FilterExpression="active = :one",
            ExpressionAttributeValues={":pk": s("LISTING#1"), ":one": n(1)},
            ConsistentRead=True,
        )
        return len(response["Items"])

    def redeemed(self):
        response = self.client.query(
            TableName=self.table,
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": s("COUPON#SAVE10#7")},
            ConsistentRead=True,
        )
        return len(response["Items"])

    def guarded(self, action):
        try:
            action()
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("ConditionalCheckFailedException", "TransactionCanceledException"):
                return False
            raise

    def lost_update(self, guard):
        self.reset()
        a = self.get("STOCK#1", "META")
        b = self.get("STOCK#1", "META")
        outcomes = []
        for seen in (a, b):
            kwargs = {
                "TableName": self.table,
                "Key": {"PK": s("STOCK#1"), "SK": s("META")},
                "UpdateExpression": "SET available = :left, version = :next",
                "ExpressionAttributeValues": {
                    ":left": n(seen["available"] - 1),
                    ":next": n(seen["version"] + 1),
                },
            }
            if guard:
                kwargs["ConditionExpression"] = "version = :seen"
                kwargs["ExpressionAttributeValues"][":seen"] = n(seen["version"])
            outcomes.append(self.guarded(lambda kw=kwargs: self.client.update_item(**kw)))
        final = self.get("STOCK#1", "META")["available"]
        return final != 10 - sum(outcomes), outcomes

    def write_skew(self, guard):
        self.reset()
        counts = [self.active(), self.active()]
        outcomes = []
        for me, other in ((1, 2), (2, 1)):
            if counts[me - 1] < 2:
                outcomes.append(False)
                continue
            update = {
                "TableName": self.table,
                "Key": {"PK": s("LISTING#1"), "SK": s(f"SELLER#{me}")},
                "UpdateExpression": "SET active = :zero",
                "ExpressionAttributeValues": {":zero": n(0)},
            }
            if guard:
                check = {
                    "TableName": self.table,
                    "Key": {"PK": s("LISTING#1"), "SK": s(f"SELLER#{other}")},
                    "ConditionExpression": "active = :one",
                    "ExpressionAttributeValues": {":one": n(1)},
                }
                items = [{"Update": update}, {"ConditionCheck": check}]
                outcomes.append(
                    self.guarded(lambda t=items: self.client.transact_write_items(TransactItems=t))
                )
            else:
                outcomes.append(self.guarded(lambda u=update: self.client.update_item(**u)))
        return all(outcomes) and self.active() == 0, outcomes

    def phantom(self, guard):
        self.reset()
        counts = [self.redeemed(), self.redeemed()]
        outcomes = []
        for nonce, seen in zip((1, 2), counts, strict=True):
            if seen:
                outcomes.append(False)
                continue
            sk = "REDEEMED" if guard else f"REDEEMED#{nonce}"
            item = {"PK": s("COUPON#SAVE10#7"), "SK": s(sk), "nonce": n(nonce)}
            condition = "attribute_not_exists(PK)" if guard else None
            outcomes.append(self.guarded(lambda i=item, c=condition: self.put(i, c)))
        return all(outcomes) and self.redeemed() == 2, outcomes


def run_dynamo_matrix(cfg, log=print):
    lab = DynamoLab(cfg)
    matrix = {}
    for name in ("lost_update", "write_skew", "phantom"):
        row = {}
        for mode, guard in (("plain writes", False), ("conditional writes", True)):
            anomaly, outcomes = getattr(lab, name)(guard)
            if anomaly:
                row[mode] = "anomaly"
            elif not all(outcomes):
                row[mode] = "condition failed"
            else:
                row[mode] = "prevented"
        matrix[name] = row
        log(f"isolation dynamodb {name}: {row}")
    try:
        lab.client.delete_table(TableName=lab.table)
    except ClientError:
        pass
    return matrix


def run_isolation(cfg, engines, log=print):
    result = {}
    for engine in engines:
        if engine == "dynamodb":
            result[engine] = run_dynamo_matrix(cfg, log)
        else:
            result[engine] = run_sql_matrix(cfg, engine, log)
    return result

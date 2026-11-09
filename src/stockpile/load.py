import csv
import json
import random
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from psycopg import sql

from . import ddb
from .db import dynamo_client, mysql_connect, pg_connect
from .generate import TABLES

LOAD_ORDER = (
    "categories",
    "sellers",
    "customers",
    "products",
    "skus",
    "offers",
    "cart_items",
    "orders",
    "order_items",
    "shipments",
    "reviews",
)

NULLABLE = {"categories": ("parent_id",), "shipments": ("delivered_at",)}


def render(cfg, name):
    text = (cfg.root / "schema" / name).read_text(encoding="utf-8")
    return text.replace("{low_stock}", str(cfg.app.low_stock))


def split_mysql(script):
    statements = []
    delimiter = ";"
    buffer = []
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("DELIMITER "):
            delimiter = stripped.split(None, 1)[1]
            continue
        buffer.append(line)
        if stripped.endswith(delimiter):
            text = "\n".join(buffer).strip()
            statements.append(text[: -len(delimiter)].strip())
            buffer = []
    if "\n".join(buffer).strip():
        statements.append("\n".join(buffer).strip())
    return [s for s in statements if s]


def read_rows(out_dir, table):
    with open(out_dir / f"{table}.csv", newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        next(reader)
        yield from reader


def reset_postgres(cfg):
    pg = cfg.postgres
    with pg_connect(pg, database="postgres") as admin:
        name = sql.Identifier(pg.database)
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(name))
        admin.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' LOCALE 'C'").format(name)
        )
    with pg_connect(pg) as conn:
        conn.execute(render(cfg, "postgres.sql"))


def load_postgres(cfg):
    out_dir = cfg.data.out_dir
    timings = {}
    started = time.perf_counter()
    reset_postgres(cfg)
    timings["schema"] = time.perf_counter() - started
    with pg_connect(cfg.postgres) as conn:
        for table in LOAD_ORDER:
            t = time.perf_counter()
            columns = sql.SQL(", ").join(sql.Identifier(c) for c in TABLES[table])
            statement = sql.SQL("COPY {} ({}) FROM STDIN WITH (FORMAT csv, HEADER true)").format(
                sql.Identifier(table), columns
            )
            with conn.cursor() as cur, cur.copy(statement) as copy:
                with open(out_dir / f"{table}.csv", "rb") as handle:
                    while chunk := handle.read(1 << 20):
                        copy.write(chunk)
            timings[table] = time.perf_counter() - t
        t = time.perf_counter()
        conn.execute("VACUUM (ANALYZE)")
        timings["vacuum_analyze"] = time.perf_counter() - t
    timings["total"] = time.perf_counter() - started
    return timings


def reset_mysql(cfg):
    my = cfg.mysql
    conn = mysql_connect(my, database=False, local_infile=True)
    cur = conn.cursor()
    cur.execute(f"DROP DATABASE IF EXISTS `{my.database}`")
    cur.execute(f"CREATE DATABASE `{my.database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin")
    cur.execute(f"USE `{my.database}`")
    for statement in split_mysql(render(cfg, "mysql.sql")):
        cur.execute(statement)
    return conn


def load_mysql(cfg):
    out_dir = cfg.data.out_dir
    timings = {}
    started = time.perf_counter()
    conn = reset_mysql(cfg)
    cur = conn.cursor()
    timings["schema"] = time.perf_counter() - started
    if cfg.mysql.mode == "external":
        try:
            cur.execute("SET GLOBAL local_infile = 1")
        except Exception:
            pass
    try:
        for table in LOAD_ORDER:
            t = time.perf_counter()
            targets = []
            sets = []
            for column in TABLES[table]:
                if column in NULLABLE.get(table, ()):
                    targets.append(f"@{column}")
                    sets.append(f"{column} = NULLIF(@{column}, '')")
                else:
                    targets.append(column)
            path = (out_dir / f"{table}.csv").resolve().as_posix().replace("'", "''")
            statement = (
                f"LOAD DATA LOCAL INFILE '{path}' INTO TABLE {table} CHARACTER SET utf8mb4 "
                "FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '\"' ESCAPED BY '' "
                f"LINES TERMINATED BY '\\n' IGNORE 1 LINES ({', '.join(targets)})"
            )
            if sets:
                statement += f" SET {', '.join(sets)}"
            cur.execute(statement)
            cur.execute("SHOW WARNINGS")
            warnings = cur.fetchall()
            if warnings:
                raise RuntimeError(f"LOAD DATA into {table} raised warnings: {warnings[:3]}")
            timings[table] = time.perf_counter() - t
        t = time.perf_counter()
        for table in LOAD_ORDER:
            cur.execute(f"ANALYZE TABLE {table}")
            cur.fetchall()
        timings["analyze"] = time.perf_counter() - t
    finally:
        conn.close()
    timings["total"] = time.perf_counter() - started
    return timings


def reset_dynamodb(cfg, client=None):
    client = client or dynamo_client(cfg.dynamodb, attempts=10)
    name = cfg.dynamodb.table
    try:
        client.delete_table(TableName=name)
        client.get_waiter("table_not_exists").wait(
            TableName=name, WaiterConfig={"Delay": 1, "MaxAttempts": 600}
        )
    except client.exceptions.ResourceNotFoundException:
        pass
    spec = json.loads((cfg.root / "schema" / "dynamodb.json").read_text(encoding="utf-8"))
    client.create_table(TableName=name, **spec)
    client.get_waiter("table_exists").wait(
        TableName=name, WaiterConfig={"Delay": 1, "MaxAttempts": 600}
    )
    return client


def write_batches(client, table, items, threads):
    batch = []
    futures = []
    written = 0

    def flush(chunk):
        requests = {table: [{"PutRequest": {"Item": item}} for item in chunk]}
        delay = 0.05
        while requests:
            response = client.batch_write_item(RequestItems=requests)
            requests = response.get("UnprocessedItems") or {}
            if requests:
                time.sleep(delay + random.random() * delay)
                delay = min(delay * 2, 2.0)
        return len(chunk)

    with ThreadPoolExecutor(max_workers=threads) as pool:
        for item in items:
            batch.append(item)
            if len(batch) == 25:
                futures.append(pool.submit(flush, batch))
                batch = []
            if len(futures) >= threads * 8:
                written += sum(f.result() for f in futures)
                futures = []
        if batch:
            futures.append(pool.submit(flush, batch))
        written += sum(f.result() for f in futures)
    return written


def dynamo_items(cfg, table, lookups):
    out_dir = cfg.data.out_dir
    low = cfg.app.low_stock
    rows = read_rows(out_dir, table)
    if table == "categories":
        for r in rows:
            yield ddb.category_item(*r)
    elif table == "sellers":
        for r in rows:
            yield ddb.seller_item(*r)
    elif table == "customers":
        for r in rows:
            yield ddb.customer_item(*r)
    elif table == "products":
        for r in rows:
            yield ddb.product_item(*r)
    elif table == "skus":
        for sku_id, product_id, variant in rows:
            yield ddb.sku_item(product_id, sku_id, variant)
    elif table == "offers":
        for offer_id, sku_id, seller_id, price, list_price, available in rows:
            product_id = lookups["sku_product"][int(sku_id)]
            yield ddb.offer_item(
                product_id, sku_id, offer_id, seller_id, price, list_price, available, low
            )
    elif table == "cart_items":
        for customer_id, offer_id, qty, added_at in rows:
            sku_id = lookups["offer_sku"][int(offer_id)]
            product_id = lookups["sku_product"][sku_id]
            yield ddb.cart_item(customer_id, offer_id, product_id, sku_id, qty, added_at)
    elif table == "orders":
        for r in rows:
            yield ddb.order_item(*r)
    elif table == "order_items":
        for order_id, line_no, offer_id, seller_id, qty, price, created_at in rows:
            sku_id = lookups["offer_sku"][int(offer_id)]
            product_id = lookups["sku_product"][sku_id]
            yield ddb.line_item(
                order_id, line_no, offer_id, product_id, sku_id, seller_id, qty, price, created_at
            )
    elif table == "shipments":
        for r in rows:
            yield ddb.shipment_item(*r)
    elif table == "reviews":
        for review_id, product_id, customer_id, rating, title, created_at in rows:
            yield ddb.review_item(product_id, review_id, customer_id, rating, title, created_at)


def derived_items(cfg):
    out_dir = cfg.data.out_dir
    summary = defaultdict(lambda: [0, 0])
    for _, product_id, _, rating, _, _ in read_rows(out_dir, "reviews"):
        entry = summary[int(product_id)]
        entry[0] += 1
        entry[1] += int(rating)
    for product_id, (count, total) in summary.items():
        yield ddb.summary_item(product_id, count, total)
    shards = cfg.dynamodb.sales_shards
    sales = defaultdict(lambda: [0, 0])
    for order_id, _, _, created_at, _, total, _, _ in read_rows(out_dir, "orders"):
        entry = sales[(created_at[:10], int(order_id) % shards)]
        entry[0] += 1
        entry[1] += int(total)
    for (day, shard), (orders, gross) in sales.items():
        yield ddb.sales_item(day, shard, orders, gross)


def dynamo_lookups(cfg):
    out_dir = cfg.data.out_dir
    sku_product = [0]
    for sku_id, product_id, _ in read_rows(out_dir, "skus"):
        if int(sku_id) != len(sku_product):
            raise RuntimeError("skus.csv ids must be contiguous from 1")
        sku_product.append(int(product_id))
    offer_sku = [0]
    for row in read_rows(out_dir, "offers"):
        if int(row[0]) != len(offer_sku):
            raise RuntimeError("offers.csv ids must be contiguous from 1")
        offer_sku.append(int(row[1]))
    return {"sku_product": sku_product, "offer_sku": offer_sku}


def load_dynamodb(cfg):
    timings = {}
    started = time.perf_counter()
    client = reset_dynamodb(cfg)
    timings["schema"] = time.perf_counter() - started
    lookups = dynamo_lookups(cfg)
    threads = cfg.dynamodb.load_threads
    counts = {}
    for table in LOAD_ORDER:
        t = time.perf_counter()
        counts[table] = write_batches(
            client, cfg.dynamodb.table, dynamo_items(cfg, table, lookups), threads
        )
        timings[table] = time.perf_counter() - t
    t = time.perf_counter()
    counts["derived"] = write_batches(client, cfg.dynamodb.table, derived_items(cfg), threads)
    timings["derived"] = time.perf_counter() - t
    timings["total"] = time.perf_counter() - started
    timings["items"] = sum(counts.values())
    return timings


LOADERS = {"postgres": load_postgres, "mysql": load_mysql, "dynamodb": load_dynamodb}

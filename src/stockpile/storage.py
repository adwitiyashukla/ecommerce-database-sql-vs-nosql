import json
from collections import defaultdict

from .db import dynamo_client, mysql_connect, pg_connect
from .load import LOAD_ORDER, derived_items, dynamo_items, dynamo_lookups
from .sizing import OVERHEAD, index_entry_size, item_size

ORDER_TABLES = ("orders", "order_items", "shipments")


def summarize(tables, orders):
    order_bytes = sum(tables[t]["data"] + tables[t]["indexes"] for t in ORDER_TABLES)
    return {
        "tables": tables,
        "orders": orders,
        "total_bytes": sum(t["data"] + t["indexes"] for t in tables.values()),
        "index_bytes": sum(t["indexes"] for t in tables.values()),
        "bytes_per_order": order_bytes / orders if orders else None,
    }


def postgres_storage(cfg):
    tables = {}
    with pg_connect(cfg.postgres) as conn:
        for table in LOAD_ORDER:
            data, indexes = conn.execute(
                "SELECT pg_table_size(%s::regclass), pg_indexes_size(%s::regclass)", (table, table)
            ).fetchone()
            tables[table] = {"data": int(data), "indexes": int(indexes)}
        orders = conn.execute("SELECT count(*) FROM orders").fetchone()[0]
    return summarize(tables, int(orders))


def mysql_storage(cfg):
    conn = mysql_connect(cfg.mysql)
    try:
        cur = conn.cursor()
        cur.execute("SET SESSION information_schema_stats_expiry = 0")
        for table in LOAD_ORDER:
            cur.execute(f"ANALYZE TABLE {table}")
            cur.fetchall()
        cur.execute(
            "SELECT TABLE_NAME, DATA_LENGTH, INDEX_LENGTH FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = %s",
            (cfg.mysql.database,),
        )
        found = {name: (int(data), int(index)) for name, data, index in cur.fetchall()}
        cur.execute("SELECT COUNT(*) FROM orders")
        orders = cur.fetchone()[0]
    finally:
        conn.close()
    tables = {t: {"data": found[t][0], "indexes": found[t][1]} for t in LOAD_ORDER}
    return summarize(tables, int(orders))


def dynamo_storage(cfg):
    spec = json.loads((cfg.root / "schema" / "dynamodb.json").read_text(encoding="utf-8"))
    indexes = [
        (
            [key["AttributeName"] for key in index["KeySchema"]],
            index["Projection"].get("NonKeyAttributes", []),
        )
        for index in spec["GlobalSecondaryIndexes"]
    ]
    totals = defaultdict(lambda: {"data": 0, "indexes": 0, "items": 0})

    def add(table, item):
        entry = totals[table]
        entry["data"] += item_size(item) + OVERHEAD
        entry["items"] += 1
        for keys, projected in indexes:
            if all(key in item for key in keys):
                entry["indexes"] += index_entry_size(item, keys, projected) + OVERHEAD

    lookups = dynamo_lookups(cfg)
    for table in LOAD_ORDER:
        for item in dynamo_items(cfg, table, lookups):
            add(table, item)
    for item in derived_items(cfg):
        add("summaries", item)
    orders = totals["orders"]["items"]
    result = summarize(dict(totals), orders)
    try:
        described = dynamo_client(cfg.dynamodb, attempts=3).describe_table(
            TableName=cfg.dynamodb.table
        )["Table"]
        result["reported_bytes"] = described.get("TableSizeBytes")
        result["reported_items"] = described.get("ItemCount")
    except Exception:
        result["reported_bytes"] = None
        result["reported_items"] = None
    return result


MEASURES = {"postgres": postgres_storage, "mysql": mysql_storage, "dynamodb": dynamo_storage}


def run_storage(cfg, engines, log=print):
    result = {}
    for engine in engines:
        result[engine] = MEASURES[engine](cfg)
        per_order = result[engine]["bytes_per_order"]
        log(
            f"storage {engine}: {result[engine]['total_bytes'] / 2**20:.1f} MiB total, "
            f"{per_order:.0f} bytes per order"
        )
    return result

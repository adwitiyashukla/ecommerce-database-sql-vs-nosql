import csv

import MySQLdb
import psycopg
import pytest

from ecomdb.db import mysql_connect, pg_connect

BAD_WRITES = {
    "negative stock": "UPDATE offers SET available = -1 WHERE offer_id = 1",
    "list price below price": "UPDATE offers SET list_price_cents = 1 WHERE offer_id = 1",
    "missing offer": (
        "INSERT INTO cart_items (customer_id, offer_id, qty, added_at) "
        "VALUES (1, 999999999, 1, '2026-01-01 00:00:00')"
    ),
    "too many in cart": (
        "INSERT INTO cart_items (customer_id, offer_id, qty, added_at) "
        "VALUES (1, 1, 11, '2026-01-01 00:00:00')"
    ),
    "bad zip": "UPDATE customers SET zip = 'ABCDE' WHERE customer_id = 1",
    "unknown status": "UPDATE orders SET status = 'lost' WHERE order_id = 1",
}


def connect(cfg, engine, autocommit=True):
    if engine == "postgres":
        return pg_connect(cfg.postgres, autocommit=autocommit)
    return mysql_connect(cfg.mysql, autocommit=autocommit)


def delivered_order(cfg):
    with open(cfg.data.out_dir / "orders.csv", newline="", encoding="utf-8") as handle:
        return next(
            int(r["order_id"]) for r in csv.DictReader(handle) if r["status"] == "delivered"
        )


@pytest.mark.parametrize("name", sorted(BAD_WRITES))
def test_schema_rejects_bad_writes(loaded, sql_engine, name):
    conn = connect(loaded, sql_engine)
    try:
        with pytest.raises((psycopg.Error, MySQLdb.MySQLError)):
            conn.cursor().execute(BAD_WRITES[name])
    finally:
        conn.close()


def test_trigger_blocks_invalid_status_moves(loaded, sql_engine):
    order = delivered_order(loaded)
    conn = connect(loaded, sql_engine)
    try:
        with pytest.raises((psycopg.Error, MySQLdb.MySQLError)):
            conn.cursor().execute(
                "UPDATE orders SET status = 'pending' WHERE order_id = %s", (order,)
            )
    finally:
        conn.close()
    conn = connect(loaded, sql_engine, autocommit=False)
    try:
        cur = conn.cursor()
        cur.execute("UPDATE orders SET status = 'returned' WHERE order_id = %s", (order,))
        cur.execute("SELECT status FROM orders WHERE order_id = %s", (order,))
        assert cur.fetchone()[0] == "returned"
        conn.rollback()
    finally:
        conn.close()

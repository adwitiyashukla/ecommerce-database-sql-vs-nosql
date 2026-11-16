from datetime import UTC, date, datetime, timedelta

import MySQLdb
import psycopg

from .db import mysql_connect, pg_connect

FAR_FUTURE = ("9999-12-31T23:59:59", 9_999_999_999_999_999)
START_CURSOR = (0, 0)


def ts(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(UTC).replace(tzinfo=None)
        return value.strftime("%Y-%m-%dT%H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def parse_ts(text):
    return datetime.fromisoformat(text)


def product_result(rows):
    if not rows:
        return None
    first = rows[0]
    product = (int(first[0]), first[1], first[2], int(first[3]), int(first[4]))
    skus = []
    offers = []
    buybox = []
    for row in rows:
        sku = (int(row[5]), row[6])
        if not skus or skus[-1] != sku:
            skus.append(sku)
        if row[7] is None:
            continue
        offer = (int(row[7]), sku[0], int(row[8]), int(row[9]), int(row[10]), int(row[11]))
        offers.append(offer)
        if int(row[12]) == 1 and offer[5] > 0:
            buybox.append((sku[0], offer[0]))
    return {"product": product, "skus": skus, "offers": offers, "buybox": sorted(buybox)}


def order_result(rows):
    if not rows:
        return None
    first = rows[0]
    order = (
        int(first[0]),
        int(first[1]),
        first[2],
        ts(first[3]),
        int(first[4]),
        int(first[5]),
        first[6],
        first[7],
    )
    items = [(int(r[8]), int(r[9]), int(r[10]), int(r[11]), int(r[12])) for r in rows]
    shipment = None
    if first[13] is not None:
        shipment = (first[13], first[14], ts(first[15]), ts(first[16]))
    return {"order": order, "items": items, "shipment": shipment}


PRODUCT_PAGE = """
SELECT p.product_id, p.title, p.brand, p.category_id, p.min_price_cents,
    s.sku_id, s.variant,
    o.offer_id, o.seller_id, o.price_cents, o.list_price_cents, o.available,
    ROW_NUMBER() OVER (
        PARTITION BY s.sku_id
        ORDER BY CASE WHEN o.available > 0 THEN 0 ELSE 1 END, o.price_cents, o.offer_id
    ) AS sku_rank
FROM products p
JOIN skus s ON s.product_id = p.product_id
LEFT JOIN offers o ON o.sku_id = s.sku_id
WHERE p.product_id = %s
ORDER BY s.sku_id, o.offer_id
"""

GET_CART = """
SELECT offer_id, qty, added_at FROM cart_items WHERE customer_id = %s ORDER BY offer_id
"""

GET_ORDER = """
SELECT o.order_id, o.customer_id, o.status, o.created_at, o.item_count, o.total_cents,
    o.ship_state, o.ship_zip,
    i.line_no, i.offer_id, i.seller_id, i.qty, i.unit_price_cents,
    s.carrier, s.tracking, s.shipped_at, s.delivered_at
FROM orders o
JOIN order_items i ON i.order_id = o.order_id
LEFT JOIN shipments s ON s.order_id = o.order_id
WHERE o.order_id = %s
ORDER BY i.line_no
"""

SELLER_ORDERS = """
SELECT created_at, order_id, line_no, offer_id, qty, unit_price_cents
FROM order_items
WHERE seller_id = %s AND created_at >= %s AND created_at < %s
ORDER BY created_at, order_id, line_no
LIMIT %s
"""

PRODUCT_REVIEWS = """
SELECT review_id, customer_id, rating, title, created_at,
    COUNT(*) OVER () AS review_count, SUM(rating) OVER () AS rating_sum
FROM reviews
WHERE product_id = %s
ORDER BY created_at DESC, review_id DESC
LIMIT %s
"""


class SqlStore:
    engine = ""

    def __init__(self, cfg):
        self.cfg = cfg
        self.low = cfg.app.low_stock
        self.conn = self.connect()
        self.requests = 0

    def close(self):
        self.conn.close()

    def time_param(self, text):
        return parse_ts(text)

    def rows(self, statement, params=()):
        self.requests += 1
        cur = self.conn.cursor()
        try:
            cur.execute(statement, params)
            return cur.fetchall()
        finally:
            cur.close()

    def execute(self, statement, params=()):
        self.requests += 1
        cur = self.conn.cursor()
        try:
            cur.execute(statement, params)
        finally:
            cur.close()

    def product_page(self, product_id):
        return product_result(self.rows(PRODUCT_PAGE, (product_id,)))

    def browse_category(self, category_id, after=START_CURSOR, limit=24):
        price, product_id = after
        rows = self.rows(self.BROWSE, self.browse_params(category_id, price, product_id, limit))
        return [(int(r[0]), r[1], int(r[2])) for r in rows]

    def get_cart(self, customer_id):
        return [(int(r[0]), int(r[1]), ts(r[2])) for r in self.rows(GET_CART, (customer_id,))]

    def set_cart_item(self, customer_id, product_id, sku_id, offer_id, qty, added_at):
        self.execute(self.UPSERT_CART, (customer_id, offer_id, qty, self.time_param(added_at)))

    def get_order(self, order_id):
        return order_result(self.rows(GET_ORDER, (order_id,)))

    def customer_orders(self, customer_id, before=FAR_FUTURE, limit=10):
        created, order_id = before
        rows = self.rows(
            self.CUSTOMER_ORDERS,
            self.customer_params(customer_id, self.time_param(created), order_id, limit),
        )
        return [(int(r[0]), ts(r[1]), r[2], int(r[3]), int(r[4])) for r in rows]

    def seller_orders(self, seller_id, start, end, limit=50):
        rows = self.rows(
            SELLER_ORDERS, (seller_id, self.time_param(start), self.time_param(end), limit)
        )
        return [(ts(r[0]), int(r[1]), int(r[2]), int(r[3]), int(r[4]), int(r[5])) for r in rows]

    def product_reviews(self, product_id, limit=10):
        rows = self.rows(PRODUCT_REVIEWS, (product_id, limit))
        reviews = [(int(r[0]), int(r[1]), int(r[2]), r[3], ts(r[4])) for r in rows]
        count = int(rows[0][5]) if rows else 0
        total = int(rows[0][6]) if rows else 0
        return {"count": count, "rating_sum": total, "reviews": reviews}

    def low_stock(self, seller_id):
        return [(int(r[0]), int(r[1])) for r in self.rows(self.low_stock_sql(), (seller_id,))]

    def daily_sales(self, first_day, last_day):
        end_day = (date.fromisoformat(last_day) + timedelta(days=1)).isoformat()
        params = (self.time_param(f"{first_day}T00:00:00"), self.time_param(f"{end_day}T00:00:00"))
        return [(ts(r[0]), int(r[1]), int(r[2])) for r in self.rows(self.DAILY_SALES, params)]


class PostgresStore(SqlStore):
    engine = "postgres"

    BROWSE = """
SELECT product_id, title, min_price_cents
FROM products
WHERE category_id = %s AND (min_price_cents, product_id) > (%s, %s)
ORDER BY min_price_cents, product_id
LIMIT %s
"""

    UPSERT_CART = """
INSERT INTO cart_items (customer_id, offer_id, qty, added_at)
VALUES (%s, %s, %s, %s)
ON CONFLICT (customer_id, offer_id) DO UPDATE SET qty = EXCLUDED.qty, added_at = EXCLUDED.added_at
"""

    CUSTOMER_ORDERS = """
SELECT order_id, created_at, status, item_count, total_cents
FROM orders
WHERE customer_id = %s AND (created_at, order_id) < (%s, %s)
ORDER BY created_at DESC, order_id DESC
LIMIT %s
"""

    DAILY_SALES = """
SELECT created_at::date AS day, count(*), sum(total_cents)
FROM orders
WHERE created_at >= %s AND created_at < %s
GROUP BY 1
ORDER BY 1
"""

    def connect(self):
        return pg_connect(self.cfg.postgres)

    def low_stock_sql(self):
        return (
            "SELECT offer_id, available FROM offers "
            f"WHERE seller_id = %s AND available < {int(self.low)} ORDER BY offer_id"
        )

    def time_param(self, text):
        return parse_ts(text).replace(tzinfo=UTC)

    def browse_params(self, category_id, price, product_id, limit):
        return (category_id, price, product_id, limit)

    def customer_params(self, customer_id, created, order_id, limit):
        return (customer_id, created, order_id, limit)

    def checkout(self, customer_id, order_id, created_at, state, zip_code):
        try:
            row = self.rows(
                "SELECT result, line_count, amount FROM checkout(%s, %s, %s, %s, %s)",
                (customer_id, order_id, self.time_param(created_at), state, zip_code),
            )[0]
        except psycopg.errors.UniqueViolation:
            return {"result": "duplicate", "line_count": 0, "amount": 0}
        return {"result": row[0], "line_count": int(row[1]), "amount": int(row[2])}

    def cancel_order(self, order_id):
        return bool(self.rows("SELECT cancel_order(%s)", (order_id,))[0][0])


class MySQLStore(SqlStore):
    engine = "mysql"

    BROWSE = """
SELECT product_id, title, min_price_cents
FROM products
WHERE category_id = %s AND min_price_cents >= %s
    AND NOT (min_price_cents = %s AND product_id <= %s)
ORDER BY min_price_cents, product_id
LIMIT %s
"""

    UPSERT_CART = """
INSERT INTO cart_items (customer_id, offer_id, qty, added_at)
VALUES (%s, %s, %s, %s) AS new
ON DUPLICATE KEY UPDATE qty = new.qty, added_at = new.added_at
"""

    CUSTOMER_ORDERS = """
SELECT order_id, created_at, status, item_count, total_cents
FROM orders
WHERE customer_id = %s AND created_at <= %s AND NOT (created_at = %s AND order_id >= %s)
ORDER BY created_at DESC, order_id DESC
LIMIT %s
"""

    def low_stock_sql(self):
        return (
            "SELECT offer_id, available FROM offers "
            "WHERE seller_id = %s AND low_stock = 1 ORDER BY offer_id"
        )

    DAILY_SALES = """
SELECT DATE(created_at) AS day, COUNT(*), SUM(total_cents)
FROM orders
WHERE created_at >= %s AND created_at < %s
GROUP BY DATE(created_at)
ORDER BY day
"""

    def connect(self):
        return mysql_connect(self.cfg.mysql)

    def browse_params(self, category_id, price, product_id, limit):
        return (category_id, price, price, product_id, limit)

    def customer_params(self, customer_id, created, order_id, limit):
        return (customer_id, created, created, order_id, limit)

    def call(self, statement, params):
        self.requests += 1
        cur = self.conn.cursor()
        try:
            cur.execute(statement, params)
            row = cur.fetchone()
            while cur.nextset():
                pass
            return row
        finally:
            cur.close()

    def checkout(self, customer_id, order_id, created_at, state, zip_code):
        try:
            row = self.call(
                "CALL checkout(%s, %s, %s, %s, %s)",
                (customer_id, order_id, self.time_param(created_at), state, zip_code),
            )
        except MySQLdb.IntegrityError as exc:
            if exc.args and exc.args[0] == 1062:
                return {"result": "duplicate", "line_count": 0, "amount": 0}
            raise
        return {"result": row[0], "line_count": int(row[1]), "amount": int(row[2])}

    def cancel_order(self, order_id):
        return bool(self.call("CALL cancel_order(%s)", (order_id,))[0])

import time
from datetime import date, datetime, timedelta

from botocore.exceptions import ClientError

from . import ddb
from .db import dynamo_client
from .ddb import n, plain, s

FAR_FUTURE = ("9999-12-31T23:59:59", 9_999_999_999_999_999)
START_CURSOR = (0, 0)
CANCELLABLE = ("pending", "paid")
MAX_LINES = 32


class Conflict(Exception):
    pass


def cancellation_codes(exc):
    error = exc.response.get("Error", {})
    if error.get("Code") != "TransactionCanceledException":
        return None
    reasons = exc.response.get("CancellationReasons")
    if reasons:
        return [reason.get("Code", "None") for reason in reasons]
    message = error.get("Message", "")
    if "[" in message and "]" in message:
        inner = message[message.index("[") + 1 : message.rindex("]")]
        return [part.strip() for part in inner.split(",")]
    return []


def result(name, line_count=0, amount=0):
    return {"result": name, "line_count": line_count, "amount": amount}


class DynamoStore:
    engine = "dynamodb"

    def __init__(self, cfg, attempts=3):
        self.cfg = cfg
        self.client = dynamo_client(cfg.dynamodb, attempts=attempts)
        self.table = cfg.dynamodb.table
        self.low = cfg.app.low_stock
        self.shards = cfg.dynamodb.sales_shards
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

    def query(self, **kwargs):
        kwargs["TableName"] = self.table
        limit = kwargs.get("Limit")
        items = []
        while True:
            response = self.call("query", **kwargs)
            items.extend(response.get("Items", []))
            last = response.get("LastEvaluatedKey")
            if not last or (limit is not None and len(items) >= limit):
                return [plain(item) for item in items]
            kwargs["ExclusiveStartKey"] = last
            if limit is not None:
                kwargs["Limit"] = limit - len(items)

    def batch_get(self, keys, consistent):
        found = {}
        for start in range(0, len(keys), 100):
            request = {
                self.table: {"Keys": keys[start : start + 100], "ConsistentRead": consistent}
            }
            delay = 0.02
            while request:
                response = self.call("batch_get_item", RequestItems=request)
                for item in response.get("Responses", {}).get(self.table, []):
                    item = plain(item)
                    found[(item["PK"], item["SK"])] = item
                request = response.get("UnprocessedKeys") or {}
                if request:
                    time.sleep(delay)
                    delay = min(delay * 2, 1.0)
        return found

    def offer_key(self, product_id, sku_id, offer_id):
        return {"PK": s(ddb.product_pk(product_id)), "SK": s(ddb.offer_sk(sku_id, offer_id))}

    def product_page(self, product_id):
        items = self.query(
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": s(ddb.product_pk(product_id))},
        )
        meta = next((i for i in items if i["SK"] == ddb.META), None)
        if meta is None:
            return None
        product = (
            meta["product_id"],
            meta["title"],
            meta["brand"],
            meta["category_id"],
            meta["min_price"],
        )
        skus = sorted((i["sku_id"], i["variant"]) for i in items if i["SK"].startswith("SKU#"))
        offers = sorted(
            (
                (
                    i["offer_id"],
                    i["sku_id"],
                    i["seller_id"],
                    i["price"],
                    i["list_price"],
                    i["available"],
                )
                for i in items
                if i["SK"].startswith("OFFER#")
            ),
            key=lambda offer: (offer[1], offer[0]),
        )
        best = {}
        for offer in offers:
            if offer[5] <= 0:
                continue
            current = best.get(offer[1])
            if current is None or (offer[3], offer[0]) < (current[3], current[0]):
                best[offer[1]] = offer
        buybox = sorted((sku, offer[0]) for sku, offer in best.items())
        return {"product": product, "skus": skus, "offers": offers, "buybox": buybox}

    def browse_category(self, category_id, after=START_CURSOR, limit=24):
        price, product_id = after
        items = self.query(
            IndexName="browse",
            KeyConditionExpression="GSI1PK = :pk AND GSI1SK > :after",
            ExpressionAttributeValues={
                ":pk": s(ddb.category_pk(category_id)),
                ":after": s(ddb.browse_sort(price, product_id)),
            },
            Limit=limit,
        )
        return [(i["product_id"], i["title"], i["min_price"]) for i in items]

    def cart_lines(self, customer_id):
        return self.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :cart)",
            ExpressionAttributeValues={
                ":pk": s(ddb.customer_pk(customer_id)),
                ":cart": s("CART#"),
            },
            ConsistentRead=True,
        )

    def get_cart(self, customer_id):
        return [(i["offer_id"], i["qty"], i["added_at"]) for i in self.cart_lines(customer_id)]

    def set_cart_item(self, customer_id, product_id, sku_id, offer_id, qty, added_at):
        self.call(
            "put_item",
            TableName=self.table,
            Item=ddb.cart_item(customer_id, offer_id, product_id, sku_id, qty, added_at),
        )

    def checkout(self, customer_id, order_id, created_at, state, zip_code, retries=8):
        cart = sorted(self.cart_lines(customer_id), key=lambda line: line["offer_id"])
        if len(cart) > MAX_LINES:
            raise ValueError(f"a DynamoDB checkout holds at most {MAX_LINES} cart lines")
        order_key = (ddb.order_pk(order_id), ddb.META)
        for _ in range(retries + 1):
            keys = [{"PK": s(order_key[0]), "SK": s(order_key[1])}]
            keys += [self.offer_key(c["product_id"], c["sku_id"], c["offer_id"]) for c in cart]
            found = self.batch_get(keys, consistent=True)
            if order_key in found:
                return result("duplicate")
            if not cart:
                return result("empty")
            lines = []
            for line in cart:
                offer = found[
                    (
                        ddb.product_pk(line["product_id"]),
                        ddb.offer_sk(line["sku_id"], line["offer_id"]),
                    )
                ]
                if offer["available"] < line["qty"]:
                    return result("out_of_stock")
                lines.append((line, offer))
            actions, total = self.checkout_actions(
                customer_id, order_id, created_at, state, zip_code, lines
            )
            try:
                self.call("transact_write_items", TransactItems=actions)
                return result("placed", len(lines), total)
            except ClientError as exc:
                codes = cancellation_codes(exc)
                if codes is None:
                    raise
                if len(codes) > len(lines) and codes[len(lines)] == "ConditionalCheckFailed":
                    return result("duplicate")
        raise Conflict(f"checkout of order {order_id} kept conflicting")

    def checkout_actions(self, customer_id, order_id, created_at, state, zip_code, lines):
        actions = []
        total = 0
        for line, offer in lines:
            new = offer["available"] - line["qty"]
            update = {
                "TableName": self.table,
                "Key": self.offer_key(line["product_id"], line["sku_id"], line["offer_id"]),
                "UpdateExpression": "SET available = :new",
                "ConditionExpression": "available = :old",
                "ExpressionAttributeValues": {":new": n(new), ":old": n(offer["available"])},
            }
            if new < self.low <= offer["available"]:
                update["UpdateExpression"] += ", GSI4PK = :lpk, GSI4SK = :lsk"
                update["ExpressionAttributeValues"][":lpk"] = s(ddb.low_pk(offer["seller_id"]))
                update["ExpressionAttributeValues"][":lsk"] = s(ddb.pad(line["offer_id"]))
            actions.append({"Update": update})
            total += line["qty"] * offer["price"]
        order = ddb.order_item(
            order_id, customer_id, "paid", created_at, len(lines), total, state, zip_code
        )
        actions.append(
            {
                "Put": {
                    "TableName": self.table,
                    "Item": order,
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            }
        )
        for line_no, (line, offer) in enumerate(lines, start=1):
            item = ddb.line_item(
                order_id,
                line_no,
                line["offer_id"],
                line["product_id"],
                line["sku_id"],
                offer["seller_id"],
                line["qty"],
                offer["price"],
                created_at,
            )
            actions.append({"Put": {"TableName": self.table, "Item": item}})
        for line, _ in lines:
            key = {"PK": s(ddb.customer_pk(customer_id)), "SK": s(ddb.cart_sk(line["offer_id"]))}
            actions.append({"Delete": {"TableName": self.table, "Key": key}})
        actions.append(
            {
                "Update": {
                    "TableName": self.table,
                    "Key": {
                        "PK": s(ddb.sales_pk(created_at[:10], order_id % self.shards)),
                        "SK": s(ddb.TOTAL),
                    },
                    "UpdateExpression": "ADD #o :one, #g :total",
                    "ExpressionAttributeNames": {"#o": "orders", "#g": "gross"},
                    "ExpressionAttributeValues": {":one": n(1), ":total": n(total)},
                }
            }
        )
        return actions, total

    def get_order(self, order_id):
        items = self.query(
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": s(ddb.order_pk(order_id))},
            ConsistentRead=True,
        )
        meta = next((i for i in items if i["SK"] == ddb.META), None)
        if meta is None:
            return None
        order = (
            meta["order_id"],
            meta["customer_id"],
            meta["status"],
            meta["created_at"],
            meta["item_count"],
            meta["total"],
            meta["ship_state"],
            meta["ship_zip"],
        )
        lines = sorted(
            (i["line_no"], i["offer_id"], i["seller_id"], i["qty"], i["unit_price"])
            for i in items
            if i["SK"].startswith("ITEM#")
        )
        ship = next((i for i in items if i["SK"] == ddb.SHIPMENT), None)
        shipment = None
        if ship is not None:
            shipment = (
                ship["carrier"],
                ship["tracking"],
                ship["shipped_at"],
                ship.get("delivered_at"),
            )
        return {"order": order, "items": lines, "shipment": shipment}

    def customer_orders(self, customer_id, before=FAR_FUTURE, limit=10):
        created, order_id = before
        items = self.query(
            IndexName="customer_orders",
            KeyConditionExpression="GSI2PK = :pk AND GSI2SK < :before",
            ExpressionAttributeValues={
                ":pk": s(ddb.customer_pk(customer_id)),
                ":before": s(ddb.customer_orders_sort(created, order_id)),
            },
            ScanIndexForward=False,
            Limit=limit,
        )
        return [
            (i["order_id"], i["created_at"], i["status"], i["item_count"], i["total"])
            for i in items
        ]

    def seller_orders(self, seller_id, start, end, limit=50):
        upper = datetime.fromisoformat(end) - timedelta(seconds=1)
        items = self.query(
            IndexName="seller_lines",
            KeyConditionExpression="GSI3PK = :pk AND GSI3SK BETWEEN :lo AND :hi",
            ExpressionAttributeValues={
                ":pk": s(ddb.seller_pk(seller_id)),
                ":lo": s(start),
                ":hi": s(upper.strftime("%Y-%m-%dT%H:%M:%S") + "~"),
            },
            Limit=limit,
        )
        return [
            (i["created_at"], i["order_id"], i["line_no"], i["offer_id"], i["qty"], i["unit_price"])
            for i in items
        ]

    def cancel_order(self, order_id, retries=8):
        for _ in range(retries + 1):
            items = self.query(
                KeyConditionExpression="PK = :pk",
                ExpressionAttributeValues={":pk": s(ddb.order_pk(order_id))},
                ConsistentRead=True,
            )
            meta = next((i for i in items if i["SK"] == ddb.META), None)
            if meta is None or meta["status"] not in CANCELLABLE:
                return False
            lines = sorted(
                (i for i in items if i["SK"].startswith("ITEM#")), key=lambda i: i["offer_id"]
            )
            keys = [self.offer_key(i["product_id"], i["sku_id"], i["offer_id"]) for i in lines]
            offers = self.batch_get(keys, consistent=True)
            actions = [
                {
                    "Update": {
                        "TableName": self.table,
                        "Key": {"PK": s(ddb.order_pk(order_id)), "SK": s(ddb.META)},
                        "UpdateExpression": "SET #st = :cancelled",
                        "ConditionExpression": "#st IN (:pending, :paid)",
                        "ExpressionAttributeNames": {"#st": "status"},
                        "ExpressionAttributeValues": {
                            ":cancelled": s("cancelled"),
                            ":pending": s("pending"),
                            ":paid": s("paid"),
                        },
                    }
                }
            ]
            for line in lines:
                offer = offers[
                    (
                        ddb.product_pk(line["product_id"]),
                        ddb.offer_sk(line["sku_id"], line["offer_id"]),
                    )
                ]
                new = offer["available"] + line["qty"]
                update = {
                    "TableName": self.table,
                    "Key": self.offer_key(line["product_id"], line["sku_id"], line["offer_id"]),
                    "UpdateExpression": "SET available = :new",
                    "ConditionExpression": "available = :old",
                    "ExpressionAttributeValues": {":new": n(new), ":old": n(offer["available"])},
                }
                if offer["available"] < self.low <= new:
                    update["UpdateExpression"] += " REMOVE GSI4PK, GSI4SK"
                actions.append({"Update": update})
            try:
                self.call("transact_write_items", TransactItems=actions)
                return True
            except ClientError as exc:
                if cancellation_codes(exc) is None:
                    raise
        raise Conflict(f"cancel of order {order_id} kept conflicting")

    def product_reviews(self, product_id, limit=10):
        items = self.query(
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": s(ddb.reviews_pk(product_id))},
            ScanIndexForward=False,
            Limit=limit + 1,
        )
        count = total = 0
        if items and items[0]["SK"] == ddb.SUMMARY:
            count, total = items[0]["review_count"], items[0]["rating_sum"]
            items = items[1:]
        reviews = [
            (i["review_id"], i["customer_id"], i["rating"], i["title"], i["created_at"])
            for i in items[:limit]
        ]
        return {"count": count, "rating_sum": total, "reviews": reviews}

    def low_stock(self, seller_id):
        items = self.query(
            IndexName="low_stock",
            KeyConditionExpression="GSI4PK = :pk",
            ExpressionAttributeValues={":pk": s(ddb.low_pk(seller_id))},
        )
        return [(i["offer_id"], i["available"]) for i in items]

    def daily_sales(self, first_day, last_day):
        first = date.fromisoformat(first_day)
        days = (date.fromisoformat(last_day) - first).days + 1
        keys = [
            {
                "PK": s(ddb.sales_pk((first + timedelta(days=d)).isoformat(), shard)),
                "SK": s(ddb.TOTAL),
            }
            for d in range(days)
            for shard in range(self.shards)
        ]
        totals = {}
        for (pk, _), item in self.batch_get(keys, consistent=False).items():
            entry = totals.setdefault(pk.split("#")[1], [0, 0])
            entry[0] += item["orders"]
            entry[1] += item["gross"]
        return [(day, o, g) for day, (o, g) in sorted(totals.items()) if o > 0]

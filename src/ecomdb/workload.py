import random
import time
from collections import defaultdict
from datetime import datetime, timedelta

from .dynamostore import DynamoStore
from .generate import read_manifest
from .load import read_rows
from .sqlstore import FAR_FUTURE, MySQLStore, PostgresStore

READS = (
    "product_page",
    "browse_category",
    "get_cart",
    "get_order",
    "customer_orders",
    "seller_orders",
    "product_reviews",
    "low_stock",
    "daily_sales",
)
RESERVED = 20
EVENTUAL = {"browse_category", "customer_orders", "seller_orders", "low_stock", "daily_sales"}


def open_store(cfg, engine, attempts=3):
    if engine == "postgres":
        return PostgresStore(cfg)
    if engine == "mysql":
        return MySQLStore(cfg)
    return DynamoStore(cfg, attempts=attempts)


def normalize(value):
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [normalize(v) for v in value]
    return value


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


class Dataset:
    def __init__(self, cfg):
        manifest = read_manifest(cfg.data.out_dir)
        if manifest is None:
            raise RuntimeError("no generated data, run the generate command first")
        rows = {name: info["rows"] for name, info in manifest["tables"].items()}
        self.customers = rows["customers"]
        self.products = rows["products"]
        self.sellers = rows["sellers"]
        self.orders = rows["orders"]
        self.offers = rows["offers"]
        self.start = datetime.combine(cfg.data.start_date, datetime.min.time())
        self.days = cfg.data.days
        self.end = self.start + timedelta(days=self.days)
        out = cfg.data.out_dir
        self.sku_product = [0]
        for _, product_id, _ in read_rows(out, "skus"):
            self.sku_product.append(int(product_id))
        self.offer_sku = [0]
        self.offer_seller = [0]
        self.low_offers = []
        for offer_id, sku_id, seller_id, _, _, available in read_rows(out, "offers"):
            self.offer_sku.append(int(sku_id))
            self.offer_seller.append(int(seller_id))
            if int(available) < cfg.app.low_stock + 3:
                self.low_offers.append(int(offer_id))
        self.address = [("", "")]
        for _, _, _, state, zip_code, _ in read_rows(out, "customers"):
            self.address.append((state, zip_code))
        self.by_category = defaultdict(list)
        for product_id, category_id, _, _, min_price, _ in read_rows(out, "products"):
            self.by_category[int(category_id)].append((int(min_price), int(product_id)))
        for entries in self.by_category.values():
            entries.sort()
        self.leaves = sorted(self.by_category)
        self.cart_owners = sorted({int(r[0]) for r in read_rows(out, "cart_items")})
        self.cancellable = [
            int(r[0]) for r in read_rows(out, "orders") if r[2] in ("pending", "paid")
        ]

    def offer_ref(self, offer_id):
        sku_id = self.offer_sku[offer_id]
        return self.sku_product[sku_id], sku_id, offer_id

    def random_time(self, rng):
        return self.start + timedelta(seconds=rng.randrange(self.days * 86400))


class Sampler:
    def __init__(self, dataset, seed):
        self.ds = dataset
        self.rng = random.Random(seed)

    def product_page(self):
        return (self.rng.randint(1, self.ds.products),)

    def browse_category(self):
        category = self.rng.choice(self.ds.leaves)
        if self.rng.random() < 0.5:
            return (category, (0, 0), 24)
        return (category, self.rng.choice(self.ds.by_category[category]), 24)

    def get_cart(self):
        if self.ds.cart_owners and self.rng.random() < 0.7:
            return (self.rng.choice(self.ds.cart_owners),)
        return (self.rng.randint(1, self.ds.customers),)

    def get_order(self):
        return (self.rng.randint(1, self.ds.orders),)

    def customer_orders(self):
        customer = self.rng.randint(1, self.ds.customers)
        if self.rng.random() < 0.7:
            return (customer, FAR_FUTURE, 10)
        return (customer, (stamp(self.ds.random_time(self.rng)), FAR_FUTURE[1]), 10)

    def seller_orders(self):
        start = self.ds.start + timedelta(days=self.rng.randrange(self.ds.days - 7))
        end = start + timedelta(days=7)
        return (self.rng.randint(1, self.ds.sellers), stamp(start), stamp(end), 50)

    def product_reviews(self):
        return (self.rng.randint(1, self.ds.products), 10)

    def low_stock(self):
        return (self.rng.randint(1, self.ds.sellers),)

    def daily_sales(self):
        first = self.ds.start + timedelta(days=self.rng.randrange(self.ds.days - 30))
        last = first + timedelta(days=29)
        return (first.date().isoformat(), last.date().isoformat())

    def cart_offer(self):
        if self.ds.low_offers and self.rng.random() < 0.3:
            offer_id = self.rng.choice(self.ds.low_offers)
        else:
            offer_id = self.rng.randint(1, self.ds.offers)
        return self.ds.offer_ref(offer_id)


def same(results):
    values = [normalize(v) for v in results.values()]
    return all(v == values[0] for v in values[1:])


def compare(stores, name, args, eventual_wait=0.0):
    deadline = time.monotonic() + eventual_wait
    while True:
        results = {engine: getattr(store, name)(*args) for engine, store in stores.items()}
        if same(results) or name not in EVENTUAL or time.monotonic() >= deadline:
            return results
        time.sleep(0.2)


def run_check(cfg, engines, log=print):
    stores = {engine: open_store(cfg, engine) for engine in engines}
    ds = Dataset(cfg)
    sampler = Sampler(ds, cfg.data.seed + 1)
    report = {"reads": {}, "writes": {}, "state": {}, "mismatches": []}
    eventual_wait = 3.0 if cfg.dynamodb.mode == "aws" else 0.0

    def record(section, name, args, results):
        entry = report[section].setdefault(name, {"calls": 0, "mismatches": 0})
        entry["calls"] += 1
        if not same(results):
            entry["mismatches"] += 1
            if len(report["mismatches"]) < 20:
                report["mismatches"].append(
                    {"pattern": name, "args": normalize(args), "results": normalize(results)}
                )

    try:
        for name in READS:
            for _ in range(cfg.check.samples):
                args = getattr(sampler, name)()
                record("reads", name, args, compare(stores, name, args))
            log(f"check {name}: {report['reads'][name]}")

        rng = random.Random(cfg.data.seed + 2)
        pool = rng.sample(range(1, ds.customers - RESERVED + 1), min(50, ds.customers - RESERVED))
        clock = ds.end + timedelta(days=1)
        next_order = 10**12
        placed = []
        touched_products = set()
        touched_sellers = set()
        for step in range(cfg.check.writes):
            clock += timedelta(seconds=7)
            roll = rng.random()
            if roll < 0.5:
                customer = rng.choice(pool)
                product_id, sku_id, offer_id = sampler.cart_offer()
                args = (customer, product_id, sku_id, offer_id, rng.randint(1, 3), stamp(clock))
                touched_products.add(product_id)
                touched_sellers.add(ds.offer_seller[offer_id])
                results = {e: s.set_cart_item(*args) for e, s in stores.items()}
                record("writes", "set_cart_item", args, results)
            elif roll < 0.8:
                customer = rng.choice(pool)
                next_order += 1
                state, zip_code = ds.address[customer]
                args = (customer, next_order, stamp(clock), state, zip_code)
                results = {e: s.checkout(*args) for e, s in stores.items()}
                record("writes", "checkout", args, results)
                if normalize(results)[engines[0]]["result"] == "placed":
                    placed.append(next_order)
                if step % 10 == 0:
                    replay = {e: s.checkout(*args) for e, s in stores.items()}
                    record("writes", "checkout_replay", args, replay)
            else:
                if placed and rng.random() < 0.7:
                    order_id = rng.choice(placed)
                else:
                    order_id = rng.choice(ds.cancellable) if ds.cancellable else 1
                args = (order_id,)
                results = {e: s.cancel_order(*args) for e, s in stores.items()}
                record("writes", "cancel_order", args, results)
        log(f"check writes: {report['writes']}")

        for customer in pool:
            record("state", "get_cart", (customer,), compare(stores, "get_cart", (customer,)))
            args = (customer, FAR_FUTURE, 20)
            record(
                "state",
                "customer_orders",
                args,
                compare(stores, "customer_orders", args, eventual_wait),
            )
        for order_id in placed:
            record("state", "get_order", (order_id,), compare(stores, "get_order", (order_id,)))
        for product_id in sorted(touched_products):
            record(
                "state",
                "product_page",
                (product_id,),
                compare(stores, "product_page", (product_id,)),
            )
        window = (stamp(ds.end), stamp(clock + timedelta(days=1)), 1000)
        for seller_id in sorted(touched_sellers):
            record(
                "state",
                "low_stock",
                (seller_id,),
                compare(stores, "low_stock", (seller_id,), eventual_wait),
            )
            args = (seller_id, *window)
            record(
                "state",
                "seller_orders",
                args,
                compare(stores, "seller_orders", args, eventual_wait),
            )
        days = (ds.end.date().isoformat(), (clock + timedelta(days=1)).date().isoformat())
        record("state", "daily_sales", days, compare(stores, "daily_sales", days, eventual_wait))
        log(f"check state: {report['state']}")
    finally:
        for store in stores.values():
            store.close()
    report["ok"] = not report["mismatches"]
    report["placed"] = len(placed)
    return report

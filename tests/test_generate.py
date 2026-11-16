import csv
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from ecomdb import ddb, generate

from .conftest import base_config


def small(tmp_path, name):
    return base_config().with_overrides([f'data.out_dir="{(tmp_path / name).as_posix()}"']).data


def rows(folder, table):
    with open(folder / f"{table}.csv", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_same_seed_gives_identical_files(tmp_path):
    first = generate.generate(small(tmp_path, "a"), 5, force=True)
    second = generate.generate(small(tmp_path, "b"), 5, force=True)
    assert first["tables"] == second["tables"]


def test_up_to_date_data_is_not_rebuilt(tmp_path):
    data = small(tmp_path, "c")
    generate.generate(data, 5, force=True)
    stamp = (data.out_dir / "orders.csv").stat().st_mtime_ns
    generate.generate(data, 5)
    assert (data.out_dir / "orders.csv").stat().st_mtime_ns == stamp
    assert generate.up_to_date(data, 5)
    assert not generate.up_to_date(data, 6)


def test_references_and_totals_hold(tmp_path):
    data = small(tmp_path, "d")
    generate.generate(data, 5, force=True)
    folder = data.out_dir
    customers = {r["customer_id"] for r in rows(folder, "customers")}
    sellers = {r["seller_id"] for r in rows(folder, "sellers")}
    products = {r["product_id"]: r for r in rows(folder, "products")}
    skus = {r["sku_id"]: r for r in rows(folder, "skus")}
    offers = {r["offer_id"]: r for r in rows(folder, "offers")}
    leaves = {r["category_id"] for r in rows(folder, "categories") if r["parent_id"]}
    assert all(p["category_id"] in leaves for p in products.values())
    assert all(s["product_id"] in products for s in skus.values())
    assert all(o["sku_id"] in skus and o["seller_id"] in sellers for o in offers.values())
    assert all(int(o["list_price_cents"]) >= int(o["price_cents"]) > 0 for o in offers.values())
    assert len({(o["sku_id"], o["seller_id"]) for o in offers.values()}) == len(offers)
    lowest = defaultdict(lambda: 10**12)
    for offer in offers.values():
        product = skus[offer["sku_id"]]["product_id"]
        lowest[product] = min(lowest[product], int(offer["price_cents"]))
    assert all(int(p["min_price_cents"]) == lowest[k] for k, p in products.items())

    orders = {r["order_id"]: r for r in rows(folder, "orders")}
    lines = defaultdict(list)
    for line in rows(folder, "order_items"):
        assert line["offer_id"] in offers
        assert offers[line["offer_id"]]["seller_id"] == line["seller_id"]
        lines[line["order_id"]].append(line)
    for order_id, order in orders.items():
        assert order["customer_id"] in customers
        items = lines[order_id]
        assert int(order["item_count"]) == len(items)
        assert int(order["total_cents"]) == sum(
            int(i["qty"]) * int(i["unit_price_cents"]) for i in items
        )
        assert [int(i["line_no"]) for i in items] == list(range(1, len(items) + 1))
        offer_ids = [int(i["offer_id"]) for i in items]
        assert offer_ids == sorted(set(offer_ids))
    for shipment in rows(folder, "shipments"):
        assert orders[shipment["order_id"]]["status"] in ("shipped", "delivered", "returned")
    reviews = rows(folder, "reviews")
    assert len({(r["product_id"], r["customer_id"]) for r in reviews}) == len(reviews)
    assert all(1 <= int(r["rating"]) <= 5 for r in reviews)
    carts = rows(folder, "cart_items")
    assert len({(c["customer_id"], c["offer_id"]) for c in carts}) == len(carts)


def test_orders_stay_inside_the_window_with_sale_spikes(tmp_path):
    data = (
        base_config()
        .with_overrides([f'data.out_dir="{(tmp_path / "e").as_posix()}"', "data.scale=0.05"])
        .data
    )
    generate.generate(data, 5, force=True)
    per_day = Counter()
    start = datetime(2025, 1, 1)
    end = start + timedelta(days=data.days)
    for order in rows(data.out_dir, "orders"):
        created = datetime.fromisoformat(order["created_at"])
        assert start <= created < end
        per_day[created.date()] += 1
    median = sorted(per_day.values())[len(per_day) // 2]
    events = generate.sale_events(2025)
    assert events["summer_start"] == date(2025, 7, 8)
    assert events["black_friday"] == date(2025, 11, 28)
    assert per_day[events["black_friday"]] > 3 * median
    assert per_day[events["summer_start"]] > 2.5 * median


def test_low_stock_offers_exist(tmp_path):
    data = small(tmp_path, "f")
    generate.generate(data, 5, force=True)
    stock = [int(o["available"]) for o in rows(data.out_dir, "offers")]
    assert any(s == 0 for s in stock)
    assert any(0 < s < 5 for s in stock)
    assert sum(s < 5 for s in stock) < len(stock) / 4


def test_dynamodb_sort_keys_follow_numeric_order():
    pairs = [(1999, 7), (1999, 12), (250, 3), (100000, 1), (99, 400)]
    keys = sorted(ddb.browse_sort(price, pid) for price, pid in pairs)
    assert keys == [ddb.browse_sort(price, pid) for price, pid in sorted(pairs)]
    assert ddb.pad(42) == "0000000000000042"

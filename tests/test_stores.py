import csv
import threading

import pytest

from stockpile.workload import RESERVED, open_store

MOMENT = "2026-01-05T12:00:00"


def offers_csv(dataset, loaded):
    with open(loaded.data.out_dir / "offers.csv", newline="", encoding="utf-8") as handle:
        return {int(r["offer_id"]): int(r["available"]) for r in csv.DictReader(handle)}


def reserved(dataset, index):
    owners = set(dataset.cart_owners)
    free = [
        c for c in range(dataset.customers, dataset.customers - RESERVED, -1) if c not in owners
    ]
    return free[index]


def current(store, ref):
    product_id, _, offer_id = ref
    page = store.product_page(product_id)
    return next(o for o in page["offers"] if o[0] == offer_id)


def pick(store, dataset, loaded, low, high, count=1, skip=()):
    stock = offers_csv(dataset, loaded)
    found = []
    for offer_id in sorted(stock, key=lambda o: (o * 7919) % 10007):
        if offer_id in skip or not low <= stock[offer_id] <= high:
            continue
        ref = dataset.offer_ref(offer_id)
        if low <= current(store, ref)[5] <= high:
            found.append(ref)
        if len(found) == count:
            return found
    pytest.fail(f"no offers with stock between {low} and {high}")


def order_id(index):
    return 7 * 10**15 + index


def place(store, dataset, customer, refs, qty, oid):
    for ref in refs:
        store.set_cart_item(customer, *ref, qty, MOMENT)
    state, zip_code = dataset.address[customer]
    return store.checkout(customer, oid, MOMENT, state, zip_code)


def test_product_page_buybox_is_cheapest_in_stock_offer(store, dataset):
    for product_id in (1, 2, 3):
        page = store.product_page(product_id)
        assert page["product"][0] == product_id
        assert page["skus"]
        best = {}
        for offer_id, sku_id, _, price, _, available in page["offers"]:
            if available > 0 and (sku_id not in best or (price, offer_id) < best[sku_id]):
                best[sku_id] = (price, offer_id)
        assert page["buybox"] == sorted((sku, pair[1]) for sku, pair in best.items())
    assert store.product_page(10**9) is None


def test_browse_pages_follow_the_keyset(store, dataset):
    category = max(dataset.leaves, key=lambda c: len(dataset.by_category[c]))
    first = store.browse_category(category, (0, 0), 5)
    last = first[-1]
    second = store.browse_category(category, (last[2], last[0]), 5)
    expected = dataset.by_category[category][:10]
    assert [(p[2], p[0]) for p in first + second] == expected


def test_checkout_places_order_and_replay_is_duplicate(store, dataset, loaded):
    customer = reserved(dataset, 0)
    refs = pick(store, dataset, loaded, 30, 10**9, count=2)
    before = [current(store, ref) for ref in refs]
    result = place(store, dataset, customer, refs, 2, order_id(1))
    assert result == {
        "result": "placed",
        "line_count": 2,
        "amount": sum(2 * offer[3] for offer in before),
    }
    for ref, old in zip(refs, before, strict=True):
        assert current(store, ref)[5] == old[5] - 2
    assert store.get_cart(customer) == []
    order = store.get_order(order_id(1))
    assert order["order"][2] == "paid"
    assert [line[1] for line in order["items"]] == sorted(ref[2] for ref in refs)
    assert store.customer_orders(customer)[0][0] == order_id(1)
    state, zip_code = dataset.address[customer]
    replay = store.checkout(customer, order_id(1), MOMENT, state, zip_code)
    assert replay["result"] == "duplicate"
    assert any(day == MOMENT[:10] for day, _, _ in store.daily_sales(MOMENT[:10], MOMENT[:10]))


def test_out_of_stock_checkout_changes_nothing(store, dataset, loaded):
    customer = reserved(dataset, 1)
    empty = pick(store, dataset, loaded, 0, 0)[0]
    healthy = pick(store, dataset, loaded, 30, 10**9, skip={empty[2]})[0]
    stock = current(store, healthy)[5]
    result = place(store, dataset, customer, [empty, healthy], 1, order_id(2))
    assert result["result"] == "out_of_stock"
    assert [line[0] for line in store.get_cart(customer)] == sorted([empty[2], healthy[2]])
    assert current(store, healthy)[5] == stock
    assert store.get_order(order_id(2)) is None


def test_empty_cart_checkout(store, dataset):
    customer = reserved(dataset, 2)
    state, zip_code = dataset.address[customer]
    assert store.checkout(customer, order_id(3), MOMENT, state, zip_code)["result"] == "empty"


def test_cancel_restocks_once(store, dataset, loaded):
    customer = reserved(dataset, 3)
    ref = pick(store, dataset, loaded, 30, 10**9)[0]
    stock = current(store, ref)[5]
    assert place(store, dataset, customer, [ref], 3, order_id(4))["result"] == "placed"
    assert current(store, ref)[5] == stock - 3
    assert store.cancel_order(order_id(4)) is True
    assert current(store, ref)[5] == stock
    assert store.get_order(order_id(4))["order"][2] == "cancelled"
    assert store.cancel_order(order_id(4)) is False


def test_delivered_orders_cannot_be_cancelled(store, loaded):
    with open(loaded.data.out_dir / "orders.csv", newline="", encoding="utf-8") as handle:
        delivered = next(
            int(r["order_id"]) for r in csv.DictReader(handle) if r["status"] == "delivered"
        )
    assert store.cancel_order(delivered) is False
    assert store.get_order(delivered)["order"][2] == "delivered"


def test_low_stock_index_follows_stock(store, dataset, loaded):
    low = loaded.app.low_stock
    customer = reserved(dataset, 4)
    ref = pick(store, dataset, loaded, low + 1, low + 9)[0]
    offer = current(store, ref)
    seller = offer[2]
    assert ref[2] not in [o for o, _ in store.low_stock(seller)]
    qty = offer[5] - (low - 1)
    assert place(store, dataset, customer, [ref], qty, order_id(5))["result"] == "placed"
    assert (ref[2], low - 1) in store.low_stock(seller)
    assert store.cancel_order(order_id(5)) is True
    assert ref[2] not in [o for o, _ in store.low_stock(seller)]


def test_concurrent_checkouts_all_go_through(loaded, dataset, engine):
    customers = [reserved(dataset, 5 + i) for i in range(6)]
    stores = [open_store(loaded, engine) for _ in customers]
    refs = pick(stores[0], dataset, loaded, 50, 10**9, count=len(customers))
    gate = threading.Barrier(len(customers))
    outcomes = [[] for _ in customers]

    def buy(index):
        gate.wait()
        for round_no in range(8):
            oid = order_id(100 + round_no * len(customers) + index)
            result = place(stores[index], dataset, customers[index], [refs[index]], 1, oid)
            outcomes[index].append(result["result"])

    workers = [threading.Thread(target=buy, args=(i,)) for i in range(len(customers))]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    for opened in stores:
        opened.close()
    assert outcomes == [["placed"] * 8 for _ in customers]

import multiprocessing as mp
import random
import threading
import time
from collections import Counter
from datetime import timedelta
from pathlib import Path

import numpy as np

from . import config as config_module
from .workload import Dataset, Sampler, open_store, stamp

PATTERNS = (
    "product_page",
    "browse_category",
    "get_cart",
    "set_cart_item",
    "checkout",
    "get_order",
    "customer_orders",
    "seller_orders",
    "cancel_order",
    "product_reviews",
    "low_stock",
    "daily_sales",
)


def build_calls(ds, seed, count, order_base):
    sampler = Sampler(ds, seed)
    rng = random.Random(seed + 7)
    calls = {}
    for name in PATTERNS:
        if hasattr(sampler, name):
            calls[name] = [getattr(sampler, name)() for _ in range(count)]
    low = set(ds.low_offers)
    healthy = [o for o in range(1, ds.offers + 1) if o not in low]
    clock = ds.end + timedelta(days=2)
    calls["set_cart_item"] = []
    for i in range(count):
        offer = rng.choice(healthy)
        moment = stamp(clock + timedelta(seconds=i))
        calls["set_cart_item"].append(
            (rng.randint(1, ds.customers), *ds.offer_ref(offer), rng.randint(1, 3), moment)
        )
    buyers = rng.sample(range(1, ds.customers + 1), min(count, ds.customers))
    calls["checkout"] = []
    for i, customer in enumerate(buyers):
        refs = [ds.offer_ref(o) for o in rng.sample(healthy, rng.randint(1, 3))]
        state, zip_code = ds.address[customer]
        moment = stamp(clock + timedelta(hours=1, seconds=i))
        calls["checkout"].append((customer, refs, order_base + i, moment, state, zip_code))
    calls["cancel_order"] = [(order_base + i,) for i in range(len(buyers))]
    return calls


def invoke(store, pattern, call):
    if pattern == "checkout":
        customer, refs, order_id, moment, state, zip_code = call
        for product_id, sku_id, offer_id in refs:
            store.set_cart_item(customer, product_id, sku_id, offer_id, 1, moment)
        requests = store.requests
        capacity = getattr(store, "capacity", 0.0)
        started = time.perf_counter()
        result = store.checkout(customer, order_id, moment, state, zip_code)
        return (
            time.perf_counter() - started,
            store.requests - requests,
            getattr(store, "capacity", 0.0) - capacity,
            result["result"],
        )
    requests = store.requests
    capacity = getattr(store, "capacity", 0.0)
    started = time.perf_counter()
    result = getattr(store, pattern)(*call)
    outcome = None
    if pattern == "cancel_order":
        outcome = "cancelled" if result else "not_cancellable"
    return (
        time.perf_counter() - started,
        store.requests - requests,
        getattr(store, "capacity", 0.0) - capacity,
        outcome,
    )


def thread_main(store, pattern, calls, gate, stats):
    gate.wait()
    for call in calls:
        try:
            seconds, requests, capacity, outcome = invoke(store, pattern, call)
        except Exception as exc:
            stats["errors"][type(exc).__name__] += 1
            continue
        stats["latencies"].append(seconds)
        stats["requests"] += requests
        stats["capacity"] += capacity
        if outcome:
            stats["outcomes"][outcome] += 1
    stats["done"] = time.time()


def process_main(raw, root, engine, pattern, slices, ready, go, out):
    cfg = config_module.from_raw(raw, Path(root))
    stores = [open_store(cfg, engine) for _ in slices]
    stats = [
        {
            "latencies": [],
            "requests": 0,
            "capacity": 0.0,
            "errors": Counter(),
            "outcomes": Counter(),
            "done": 0.0,
        }
        for _ in slices
    ]
    gate = threading.Event()
    threads = [
        threading.Thread(target=thread_main, args=(store, pattern, part, gate, st))
        for store, part, st in zip(stores, slices, stats, strict=True)
    ]
    for thread in threads:
        thread.start()
    ready.put(len(threads))
    go.wait()
    gate.set()
    for thread in threads:
        thread.join()
    for store in stores:
        store.close()
    out.put(
        {
            "latencies": [x for st in stats for x in st["latencies"]],
            "requests": sum(st["requests"] for st in stats),
            "capacity": sum(st["capacity"] for st in stats),
            "errors": dict(sum((st["errors"] for st in stats), Counter())),
            "outcomes": dict(sum((st["outcomes"] for st in stats), Counter())),
            "done": max(st["done"] for st in stats),
        }
    )


def run_calls(cfg, engine, pattern, calls, clients, processes):
    ctx = mp.get_context("spawn")
    ready = ctx.Queue()
    out = ctx.Queue()
    go = ctx.Event()
    clients = max(1, min(clients, len(calls)))
    processes = max(1, min(processes, clients))
    slices = [calls[i::clients] for i in range(clients)]
    groups = [slices[i::processes] for i in range(processes)]
    procs = []
    for group in groups:
        args = (cfg.raw, str(cfg.root), engine, pattern, group, ready, go, out)
        proc = ctx.Process(target=process_main, args=args, daemon=True)
        proc.start()
        procs.append(proc)
    for _ in procs:
        ready.get(timeout=300)
    started = time.time()
    go.set()
    parts = [out.get(timeout=7200) for _ in procs]
    for proc in procs:
        proc.join(timeout=60)
    latencies = np.array([x for p in parts for x in p["latencies"]])
    seconds = max(p["done"] for p in parts) - started
    done = len(latencies)
    errors = Counter()
    outcomes = Counter()
    for p in parts:
        errors.update(p["errors"])
        outcomes.update(p["outcomes"])
    return {
        "calls": done,
        "errors": dict(errors),
        "outcomes": dict(outcomes),
        "seconds": seconds,
        "ops_per_second": done / seconds if seconds > 0 else 0.0,
        "p50_ms": float(np.percentile(latencies, 50) * 1000) if done else None,
        "p95_ms": float(np.percentile(latencies, 95) * 1000) if done else None,
        "p99_ms": float(np.percentile(latencies, 99) * 1000) if done else None,
        "requests_per_call": sum(p["requests"] for p in parts) / done if done else None,
        "capacity_per_call": sum(p["capacity"] for p in parts) / done if done else None,
    }


def run_bench(cfg, engines, log=print):
    ds = Dataset(cfg)
    bench = cfg.bench
    order_base = 3 * 10**15 + (int(time.time()) % 10**6) * 10**6
    measured = build_calls(ds, cfg.data.seed + 11, bench.calls, order_base)
    warm = (
        build_calls(ds, cfg.data.seed + 13, bench.warmup, order_base + 10**5)
        if bench.warmup
        else None
    )
    results = {}
    for engine in engines:
        results[engine] = {}
        for pattern in PATTERNS:
            if warm:
                run_calls(cfg, engine, pattern, warm[pattern], bench.clients, bench.processes)
            result = run_calls(
                cfg, engine, pattern, measured[pattern], bench.clients, bench.processes
            )
            results[engine][pattern] = result
            if result["calls"]:
                log(
                    f"bench {engine} {pattern}: p50 {result['p50_ms']:.2f} ms, "
                    f"p99 {result['p99_ms']:.2f} ms, {result['ops_per_second']:.0f} ops/s, "
                    f"{result['requests_per_call']:.2f} requests per call"
                )
            else:
                log(f"bench {engine} {pattern}: no successful calls, errors {result['errors']}")
    return results

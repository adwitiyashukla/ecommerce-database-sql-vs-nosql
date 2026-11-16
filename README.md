# ecommerce-database-sql-vs-nosql

I built the same online marketplace database three times, on PostgreSQL, MySQL and DynamoDB, and ran the same data and the same 12 queries on all of them. The store works like Amazon or Flipkart. One product can have offers from many sellers, every offer has its own price and stock, and orders go through checkout, shipping and cancel.

The main thing I wanted to see was how the data model has to change when you go from normal tables to a single DynamoDB table. I also wanted to see how each engine keeps stock correct when a lot of people buy the same item at once, and what each isolation level actually lets through.

[![ci](https://github.com/adwitiyashukla/ecommerce-database-sql-vs-nosql/actions/workflows/ci.yml/badge.svg)](https://github.com/adwitiyashukla/ecommerce-database-sql-vs-nosql/actions/workflows/ci.yml)

## What it does

- Generates a seeded US marketplace with 100,000 customers, 2,000 sellers, 25,000 products, 112,511 offers and 500,000 orders, 2.35M rows in total. Order volume follows a weekly pattern, with a four day summer sale in July at 4x a normal day, Black Friday at 5x and Cyber Monday at 4x.
- Loads the same rows into PostgreSQL with COPY, MySQL with LOAD DATA LOCAL INFILE and DynamoDB with BatchWriteItem.
- Runs the 12 access patterns below on every engine and checks that all engines return the same answers.
- Runs a flash sale where 64 buyers go after one item with 5,000 units, with six ways of taking the stock on PostgreSQL and MySQL and five on DynamoDB.
- Replays seven two-session anomalies step by step at every isolation level of PostgreSQL and MySQL, and the same races on DynamoDB with and without condition expressions.
- Writes every result to results/ as JSON plus a markdown report.

## The 12 access patterns

| # | pattern | PostgreSQL and MySQL | DynamoDB |
|---|---|---|---|
| 1 | product page with buy box | one join, ROW_NUMBER picks the cheapest offer in stock per SKU | one Query on PROD#id |
| 2 | category page by price | keyset paging on a (category, price, product) index | Query on the browse index |
| 3 | cart | primary key range | Query on CUST#id with begins_with CART# |
| 4 | set cart line | upsert, ON CONFLICT or ON DUPLICATE KEY | PutItem |
| 5 | checkout | stored routine, one call | Query, BatchGetItem, TransactWriteItems |
| 6 | order page | one join | one Query on ORDER#id |
| 7 | order history | keyset paging on (customer, created_at, order) | Query on the customer_orders index |
| 8 | seller dashboard | range on (seller, created_at) | Query on the seller_lines index |
| 9 | cancel order and restock | stored routine, one call | Query, BatchGetItem, TransactWriteItems |
| 10 | reviews with totals | window COUNT and SUM over the product's reviews | one Query, the totals item sorts first |
| 11 | low stock list | partial index on PostgreSQL, generated column index on MySQL | sparse index |
| 12 | daily sales for 30 days | GROUP BY day over a covering index | BatchGetItem over pre-counted shards |

## How the data is modeled

PostgreSQL and MySQL get the same 12 normalized tables (categories, sellers, customers, products, skus, offers, cart_items, orders, order_items, shipments, reviews and order_transitions). Money is stored in integer cents. CHECK constraints, foreign keys and a trigger keep bad data out, so an order cannot jump from delivered back to pending. Checkout and cancel are stored routines (PL/pgSQL on PostgreSQL, a procedure on MySQL), so each one is a single round trip. Both lock the offers in offer_id order, so two checkouts that share offers cannot deadlock on them.

DynamoDB gets one table and four global secondary indexes. The keys come from the access patterns, not from the entities.

| item | PK | SK |
|---|---|---|
| customer | CUST#id | PROFILE |
| cart line | CUST#id | CART#offer |
| product | PROD#id | META |
| SKU | PROD#id | SKU#sku |
| offer | PROD#id | OFFER#sku#offer |
| order | ORDER#id | META |
| order line | ORDER#id | ITEM#line |
| shipment | ORDER#id | SHIP |
| review | REVIEWS#product | created#review |
| review totals | REVIEWS#product | ~SUMMARY |
| daily sales | DAY#date#shard | TOTAL |

| index | partition key | sort key | used by |
|---|---|---|---|
| browse | CAT#category | price#product | category pages |
| customer_orders | CUST#customer | created#order | order history |
| seller_lines | SELLER#seller | created#order#line | seller dashboard |
| low_stock | LOW#seller | offer | low stock list, sparse |

A product page is one Query on PROD#id and an order page is one Query on ORDER#id. The low_stock index is sparse. An offer only carries its index keys while its stock is under the threshold, and checkout and cancel add or remove them in the same transaction that changes the stock. Daily sales are counted at write time into 8 shards per day, so a busy sale day does not turn into one hot partition.

## Results

All numbers come from my HP OmniBook 5 (Intel Core 5 120U, 12 threads, 8 GB RAM, Windows 11) with PostgreSQL 18.6, MySQL 9.7.4 and DynamoDB Local 3.3.1. The access patterns ran with 8 clients in 2 processes, 2,000 calls per pattern after 200 warmup calls. PostgreSQL and MySQL ran on the full 2.35M rows, while the three-engine comparison uses a 10 percent dataset (234,433 rows) from the same generator, because DynamoDB Local with the full data did not fit in 8 GB next to the other two servers. DynamoDB Local is an emulator running on the same laptop, so its latencies say nothing about the real service, but its request counts per call come straight from the key design and carry over.

### Load and storage

| measure | PostgreSQL | MySQL | DynamoDB |
|---|---|---|---|
| load time, full data | 216.1 s | 108.8 s | - |
| total size, full data | 407.9 MiB | 318.6 MiB | - |
| bytes per order, full data | 700 | 509 | - |
| load time, 10 percent | 31.6 s | 13.7 s | 309.7 s |
| total size, 10 percent | 41.2 MiB | 41.3 MiB | 94.9 MiB |
| bytes per order, 10 percent | 699 | 611 | 1,757 |

Bytes per order counts the orders, order_items and shipments tables with their indexes. DynamoDB sizes follow the AWS item size rules, where attribute names count toward the size and every item and index entry adds 100 bytes. DynamoDB needs 2.5 times the space per order of PostgreSQL, mostly because every item repeats its attribute names and every item and index entry carries that overhead.

The cross-engine check ran 1,800 reads, 311 writes and 545 state comparisons on the full data and 1,800 reads, 312 writes and 471 state comparisons on the 10 percent data, with 0 mismatches both times.

### Access patterns on the full data

| access pattern | PostgreSQL p50 ms | PostgreSQL p99 ms | MySQL p50 ms | MySQL p99 ms |
|---|---|---|---|---|
| product_page | 1.88 | 4.30 | 1.36 | 2.44 |
| browse_category | 1.76 | 4.44 | 1.24 | 2.37 |
| get_cart | 1.15 | 5.87 | 0.78 | 2.07 |
| set_cart_item | 1.84 | 6.70 | 7.13 | 26.59 |
| checkout | 4.44 | 9.91 | 29.69 | 64.31 |
| get_order | 2.34 | 4.79 | 1.27 | 2.71 |
| customer_orders | 2.33 | 5.08 | 1.33 | 2.95 |
| seller_orders | 2.46 | 5.82 | 1.59 | 4.95 |
| cancel_order | 2.59 | 7.48 | 57.68 | 83.52 |
| product_reviews | 1.88 | 4.68 | 1.35 | 3.39 |
| low_stock | 1.20 | 4.50 | 0.80 | 1.81 |
| daily_sales | 51.82 | 89.97 | 78.90 | 137.28 |

MySQL answered 8 of the 9 reads faster. PostgreSQL was faster on the daily sales aggregate and on every write, by a lot.

I dug into the MySQL writes with performance_schema while 8 clients cancelled orders. COMMIT averaged 53 ms per cancel, while the other statements in the procedure took under 1 ms each and row lock waits were close to zero. Setting sync_binlog to 0 and innodb_flush_log_at_trx_commit to 2 cut the redo log fsyncs from 247 to 10 per 800 cancels, but COMMIT still averaged 43 ms. Restarting MySQL with `--set mysql.binlog=false` turned the binary log off and brought COMMIT down to 3.7 ms and the median cancel from 58 ms to 7 ms, with the redo log still fsynced on every commit. So on this laptop the cost is the extra commit work for the binary log, not the disk. Replicas and point-in-time restores need the binlog, so I kept it on for the benchmark tables.

The daily sales chart is the slowest SQL read, because each call groups about 41,000 orders on the fly.

### All three engines on the 10 percent data

| access pattern | PostgreSQL p50 ms | MySQL p50 ms | DynamoDB Local p50 ms | DynamoDB requests per call |
|---|---|---|---|---|
| product_page | 1.67 | 1.32 | 26.85 | 1.00 |
| browse_category | 1.07 | 0.95 | 31.49 | 1.00 |
| get_cart | 1.67 | 0.86 | 23.27 | 1.00 |
| set_cart_item | 2.02 | 8.93 | 30.02 | 1.00 |
| checkout | 3.88 | 28.80 | 200.76 | 2.98 |
| get_order | 2.29 | 1.16 | 24.22 | 1.00 |
| customer_orders | 1.69 | 1.45 | 24.87 | 1.00 |
| seller_orders | 1.93 | 1.47 | 28.06 | 1.00 |
| cancel_order | 2.24 | 50.99 | 153.59 | 2.97 |
| product_reviews | 1.79 | 1.25 | 23.30 | 1.00 |
| low_stock | 1.66 | 0.73 | 24.96 | 1.00 |
| daily_sales | 5.72 | 8.96 | 601.53 | 3.00 |

On DynamoDB, 9 of the 12 patterns are a single request, which is the whole point of the single-table design. Checkout and cancel take 3 requests each when they go through (Query, BatchGetItem, TransactWriteItems). Daily sales takes 3 BatchGetItem calls for 240 shard items. The shards keep Black Friday writes off one partition, but each shard is its own item, so one 30 day chart costs 240 half-unit reads, 120 read units in total.

### The MySQL checkout deadlock

My first MySQL checkout procedure started with `IF EXISTS (SELECT 1 FROM orders WHERE order_id = p_order)` to catch a replayed order. With 8 clients, 68 checkouts out of 2,000 failed with error 1213 (deadlock). SHOW ENGINE INNODB STATUS showed two checkouts holding a shared lock on the same gap of the orders primary key, and each one was waiting to insert into that gap.

Under REPEATABLE READ, InnoDB runs a subquery inside a procedure's IF or SET as a locking read. A plain `SELECT ... INTO` is a snapshot read. I checked this on a three-row table. IF EXISTS left an S,GAP lock behind, SELECT COUNT(*) INTO left no lock, and under READ COMMITTED neither one locked anything. After switching the check to `SELECT COUNT(*) INTO`, the same benchmark had 0 deadlocks. The test_concurrent_checkouts_all_go_through test runs 6 checkouts at once on every engine and fails on the old procedure.

### Flash sale

64 buyers (8 processes with 8 threads each) try to buy one unit at a time of a single item with 5,000 units. A run ends when the item sells out or after 120 seconds.

| strategy | how a buyer takes a unit |
|---|---|
| naive | read the stock, then write stock minus one |
| row_lock | the same, but the read is SELECT ... FOR UPDATE |
| conditional | one UPDATE ... SET available = available - 1 WHERE available >= 1, or UpdateItem with a condition on DynamoDB |
| version | read stock and version, update only if the version did not change, retry if it did |
| serializable | the naive code at SERIALIZABLE, retried on serialization errors and deadlocks |
| transaction | TransactWriteItems that takes a unit with a condition and puts the order (DynamoDB only) |
| buckets | the 5,000 units are split over 16 rows, a buyer takes one from any free bucket (FOR UPDATE SKIP LOCKED on SQL) |

| strategy | PostgreSQL | MySQL | DynamoDB |
|---|---|---|---|
| naive | 48,318 orders for 5,000 units | 25,399 orders for 5,000 units | 24,175 orders for 5,000 units |
| row_lock | 5,000 at 304 per s | 5,000 at 192 per s | - |
| conditional | 5,000 at 390 per s | 5,000 at 204 per s | 5,000 at 208 per s |
| version | 5,000 at 110 per s | 3,392 in 120 s | 1,690 in 120 s |
| serializable | 5,000 at 121 per s | 2,322 in 120 s | - |
| transaction | - | - | 5,000 at 166 per s |
| buckets | 5,000 at 635 per s | 5,000 at 608 per s | 5,000 at 202 per s |

The naive version oversold on every engine. On PostgreSQL it took 48,318 orders while the stock counter only went down by 750, because the 64 buyers kept reading the same number and writing the same number minus one. Every other strategy sold 5,000 or fewer, never more.

The single conditional UPDATE is the simplest safe choice. Version checks fall apart when 64 buyers want one row, because only one buyer wins each round (213,638 conflicts on MySQL). MySQL SERIALIZABLE turns plain reads into shared locks, so buyers deadlock when they try to upgrade them (146,344 deadlocks). Splitting the stock into 16 buckets was the fastest on both SQL engines, 1.6 times conditional on PostgreSQL and 3.0 times on MySQL.

DynamoDB Local tops out around 200 sales per second on my laptop whatever the strategy, so buckets did not help there. On the real service an item lives on one partition, and a partition takes at most 1,000 write units per second, so a really hot item needs buckets there too. DynamoDB Local also never returned a TransactionConflict, while the real service cancels a transaction when another transaction is writing the same item, so the transaction row is a best case.

To check my Python harness, pgbench ran the conditional claim as a script for 10 seconds. It got 466 transactions per second and the harness got 415 per second on the same claim.

### Isolation levels

Each scenario runs two sessions step by step on small tables.

| scenario | what the two sessions do |
|---|---|
| lost_update | both read stock 10, both write 9 |
| lost_update_locking | the same with SELECT ... FOR UPDATE |
| write_skew | two sellers each see two active listings for a SKU and each delists their own, leaving none |
| phantom | both check that customer 7 has not used coupon SAVE10, then both insert a redemption |
| phantom_locking | the same check with SELECT ... FOR UPDATE |
| non_repeatable_read | a price read twice in one transaction changes in between |
| dirty_read | a session reads a price that the other session wrote and then rolled back |

In the tables, anomaly means the bad outcome happened. Prevented means it did not, lock wait means the second session blocked until the first one finished, and an error means the engine aborted one of the sessions.

| PostgreSQL | read uncommitted | read committed | repeatable read | serializable |
|---|---|---|---|---|
| lost_update | anomaly | anomaly | serialization error | serialization error |
| lost_update_locking | lock wait | lock wait | serialization error | serialization error |
| write_skew | anomaly | anomaly | anomaly | serialization error |
| phantom | anomaly | anomaly | anomaly | serialization error |
| phantom_locking | anomaly | anomaly | anomaly | serialization error |
| non_repeatable_read | anomaly | anomaly | prevented | prevented |
| dirty_read | prevented | prevented | prevented | prevented |

| MySQL | read uncommitted | read committed | repeatable read | serializable |
|---|---|---|---|---|
| lost_update | anomaly | anomaly | anomaly | deadlock error |
| lost_update_locking | lock wait | lock wait | lock wait | lock wait |
| write_skew | anomaly | anomaly | anomaly | deadlock error |
| phantom | anomaly | anomaly | anomaly | deadlock error |
| phantom_locking | anomaly | anomaly | deadlock error | deadlock error |
| non_repeatable_read | anomaly | anomaly | prevented | lock wait |
| dirty_read | anomaly | prevented | prevented | lock wait |

At REPEATABLE READ, PostgreSQL aborts the lost update and MySQL lets it through, because an InnoDB plain read comes from a snapshot but the UPDATE writes over the latest row. Only SERIALIZABLE stopped write skew and the coupon phantom. PostgreSQL does that with a serialization error, and MySQL turns plain reads into shared locks so one session dies in a deadlock. FOR UPDATE does not save the coupon check on PostgreSQL below SERIALIZABLE, because it only locks rows that already exist. MySQL also locks the gap at REPEATABLE READ, so one session deadlocks instead. PostgreSQL never shows a dirty read, since its READ UNCOMMITTED behaves like READ COMMITTED.

| DynamoDB | plain writes | conditional writes |
|---|---|---|
| lost_update | anomaly | condition failed |
| write_skew | anomaly | condition failed |
| phantom | anomaly | condition failed |

DynamoDB has no isolation levels for these races, so the rule has to go into the write itself. That means a version condition for the lost update, a ConditionCheck on the other seller's listing inside a transaction for the write skew, and one fixed coupon key with attribute_not_exists for the phantom. With plain writes all three broke. With the conditions the second write was rejected every time.

## Running it

You need Python 3.11 or newer and the three engines. In managed mode (the default) ecomdb starts its own servers from engines/pgsql/bin, engines/mysql/bin and engines/dynamodb, or from PATH, and keeps their data under data/. I unpacked the PostgreSQL and MySQL zip archives for Windows and the DynamoDB Local zip into engines/. DynamoDB Local needs Java 17 or newer. In external mode it connects to servers you run yourself, which is what CI does with Docker containers.

```
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
python -m ecomdb up
python -m ecomdb all
python -m ecomdb down
```

On Linux or macOS activate with `source .venv/bin/activate`. The all command runs load, storage, check, bench, flash, isolation and report, and writes results/report.md. Every setting is in config.toml and gets validated when it loads. Any value can be changed for one run with `--set section.key=value`, for example `--set data.scale=0.05` for a small dataset.

The numbers above come from these two runs:

```
python -m ecomdb all --engines postgres,mysql
python -m ecomdb all --set data.scale=0.1 --set data.out_dir=data/csv_three --set postgres.database=ecomdb_three --set mysql.database=ecomdb_three --set dynamodb.table=ecomdb_three --set app.results_dir=results/three
```

## Tests

```
python -m pytest
```

The 104 tests need all three engines running. They cover the config validation, the generator, the item size math, every store against the same expectations, the schema constraints, the flash sale strategies and the isolation cells. GitHub Actions runs ruff, the tests and a small full run on PostgreSQL 16 with MySQL 8.4 and Python 3.11, and on PostgreSQL 18 with MySQL 9.7 and Python 3.13, with DynamoDB Local next to both.

## Layout

```
config.toml          every setting, validated on load
schema/              postgres.sql, mysql.sql, dynamodb.json
src/ecomdb/
    generate.py      seeded marketplace data
    load.py          COPY, LOAD DATA and BatchWriteItem loaders
    sqlstore.py      the 12 patterns on PostgreSQL and MySQL
    dynamostore.py   the 12 patterns on DynamoDB
    ddb.py           DynamoDB keys and items
    workload.py      samplers and the cross-engine check
    bench.py         latency runs
    flashsale.py     flash sale strategies and the pgbench check
    isolation.py     two-session anomaly lab
    storage.py       storage per engine
    sizing.py        DynamoDB item sizes
    servers.py       starts and stops local servers
    report.py        writes results/report.md
    cli.py           commands
tests/
```

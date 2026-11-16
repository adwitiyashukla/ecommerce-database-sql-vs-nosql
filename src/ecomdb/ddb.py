PROFILE = "PROFILE"
META = "META"
SHIPMENT = "SHIP"
SUMMARY = "~SUMMARY"
TOTAL = "TOTAL"


def pad(value, width=16):
    return f"{int(value):0{width}d}"


def s(value):
    return {"S": str(value)}


def n(value):
    return {"N": str(value)}


def plain(item):
    out = {}
    for name, value in item.items():
        if "S" in value:
            out[name] = value["S"]
        elif "N" in value:
            text = value["N"]
            out[name] = int(text) if text.lstrip("-").isdigit() else float(text)
        elif "BOOL" in value:
            out[name] = value["BOOL"]
        elif "NULL" in value:
            out[name] = None
    return out


def customer_pk(customer_id):
    return f"CUST#{customer_id}"


def cart_sk(offer_id):
    return f"CART#{pad(offer_id)}"


def seller_pk(seller_id):
    return f"SELLER#{seller_id}"


def category_pk(category_id):
    return f"CAT#{category_id}"


def product_pk(product_id):
    return f"PROD#{product_id}"


def sku_sk(sku_id):
    return f"SKU#{pad(sku_id)}"


def offer_sk(sku_id, offer_id):
    return f"OFFER#{pad(sku_id)}#{pad(offer_id)}"


def reviews_pk(product_id):
    return f"REVIEWS#{product_id}"


def review_sk(created_at, review_id):
    return f"{created_at}#{pad(review_id)}"


def order_pk(order_id):
    return f"ORDER#{order_id}"


def line_sk(line_no):
    return f"ITEM#{int(line_no):03d}"


def sales_pk(day, shard):
    return f"DAY#{day}#{int(shard):02d}"


def browse_sort(min_price, product_id):
    return f"{int(min_price):010d}#{pad(product_id)}"


def customer_orders_sort(created_at, order_id):
    return f"{created_at}#{pad(order_id)}"


def seller_sort(created_at, order_id, line_no):
    return f"{created_at}#{pad(order_id)}#{int(line_no):03d}"


def low_pk(seller_id):
    return f"LOW#{seller_id}"


def low_marker(seller_id, offer_id):
    return {"GSI4PK": s(low_pk(seller_id)), "GSI4SK": s(pad(offer_id))}


def category_item(category_id, parent_id, name):
    item = {
        "PK": s(category_pk(category_id)),
        "SK": s(META),
        "category_id": n(category_id),
        "name": s(name),
    }
    if parent_id not in (None, ""):
        item["parent_id"] = n(parent_id)
    return item


def seller_item(seller_id, name, state, rating, joined_at):
    return {
        "PK": s(seller_pk(seller_id)),
        "SK": s(PROFILE),
        "seller_id": n(seller_id),
        "name": s(name),
        "state": s(state),
        "rating": n(rating),
        "joined_at": s(joined_at),
    }


def customer_item(customer_id, email, name, state, zip_code, created_at):
    return {
        "PK": s(customer_pk(customer_id)),
        "SK": s(PROFILE),
        "customer_id": n(customer_id),
        "email": s(email),
        "name": s(name),
        "state": s(state),
        "zip": s(zip_code),
        "created_at": s(created_at),
    }


def product_item(product_id, category_id, title, brand, min_price, created_at):
    return {
        "PK": s(product_pk(product_id)),
        "SK": s(META),
        "product_id": n(product_id),
        "category_id": n(category_id),
        "title": s(title),
        "brand": s(brand),
        "min_price": n(min_price),
        "created_at": s(created_at),
        "GSI1PK": s(category_pk(category_id)),
        "GSI1SK": s(browse_sort(min_price, product_id)),
    }


def sku_item(product_id, sku_id, variant):
    return {
        "PK": s(product_pk(product_id)),
        "SK": s(sku_sk(sku_id)),
        "sku_id": n(sku_id),
        "variant": s(variant),
    }


def offer_item(product_id, sku_id, offer_id, seller_id, price, list_price, available, low_stock):
    item = {
        "PK": s(product_pk(product_id)),
        "SK": s(offer_sk(sku_id, offer_id)),
        "offer_id": n(offer_id),
        "sku_id": n(sku_id),
        "seller_id": n(seller_id),
        "price": n(price),
        "list_price": n(list_price),
        "available": n(available),
    }
    if int(available) < low_stock:
        item.update(low_marker(seller_id, offer_id))
    return item


def cart_item(customer_id, offer_id, product_id, sku_id, qty, added_at):
    return {
        "PK": s(customer_pk(customer_id)),
        "SK": s(cart_sk(offer_id)),
        "offer_id": n(offer_id),
        "product_id": n(product_id),
        "sku_id": n(sku_id),
        "qty": n(qty),
        "added_at": s(added_at),
    }


def order_item(order_id, customer_id, status, created_at, item_count, total, state, zip_code):
    return {
        "PK": s(order_pk(order_id)),
        "SK": s(META),
        "order_id": n(order_id),
        "customer_id": n(customer_id),
        "status": s(status),
        "created_at": s(created_at),
        "item_count": n(item_count),
        "total": n(total),
        "ship_state": s(state),
        "ship_zip": s(zip_code),
        "GSI2PK": s(customer_pk(customer_id)),
        "GSI2SK": s(customer_orders_sort(created_at, order_id)),
    }


def line_item(order_id, line_no, offer_id, product_id, sku_id, seller_id, qty, price, created_at):
    return {
        "PK": s(order_pk(order_id)),
        "SK": s(line_sk(line_no)),
        "order_id": n(order_id),
        "line_no": n(line_no),
        "offer_id": n(offer_id),
        "product_id": n(product_id),
        "sku_id": n(sku_id),
        "seller_id": n(seller_id),
        "qty": n(qty),
        "unit_price": n(price),
        "created_at": s(created_at),
        "GSI3PK": s(seller_pk(seller_id)),
        "GSI3SK": s(seller_sort(created_at, order_id, line_no)),
    }


def shipment_item(order_id, carrier, tracking, shipped_at, delivered_at):
    item = {
        "PK": s(order_pk(order_id)),
        "SK": s(SHIPMENT),
        "carrier": s(carrier),
        "tracking": s(tracking),
        "shipped_at": s(shipped_at),
    }
    if delivered_at:
        item["delivered_at"] = s(delivered_at)
    return item


def review_item(product_id, review_id, customer_id, rating, title, created_at):
    return {
        "PK": s(reviews_pk(product_id)),
        "SK": s(review_sk(created_at, review_id)),
        "review_id": n(review_id),
        "customer_id": n(customer_id),
        "rating": n(rating),
        "title": s(title),
        "created_at": s(created_at),
    }


def summary_item(product_id, count, rating_sum):
    return {
        "PK": s(reviews_pk(product_id)),
        "SK": s(SUMMARY),
        "review_count": n(count),
        "rating_sum": n(rating_sum),
    }


def sales_item(day, shard, orders, gross):
    return {
        "PK": s(sales_pk(day, shard)),
        "SK": s(TOTAL),
        "orders": n(orders),
        "gross": n(gross),
    }

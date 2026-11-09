CREATE TABLE categories (
    category_id integer PRIMARY KEY,
    parent_id integer REFERENCES categories (category_id),
    name text NOT NULL
);

CREATE TABLE sellers (
    seller_id integer PRIMARY KEY,
    name text NOT NULL UNIQUE,
    state char(2) NOT NULL,
    rating numeric(2, 1) NOT NULL CHECK (rating BETWEEN 1.0 AND 5.0),
    joined_at timestamptz NOT NULL
);

CREATE TABLE customers (
    customer_id bigint PRIMARY KEY,
    email text NOT NULL UNIQUE,
    name text NOT NULL,
    state char(2) NOT NULL,
    zip char(5) NOT NULL CHECK (zip ~ '^[0-9]{5}$'),
    created_at timestamptz NOT NULL
);

CREATE TABLE products (
    product_id bigint PRIMARY KEY,
    category_id integer NOT NULL REFERENCES categories (category_id),
    title text NOT NULL,
    brand text NOT NULL,
    min_price_cents integer NOT NULL CHECK (min_price_cents > 0),
    created_at timestamptz NOT NULL
);

CREATE INDEX products_browse ON products (category_id, min_price_cents, product_id) INCLUDE (title);

CREATE TABLE skus (
    sku_id bigint PRIMARY KEY,
    product_id bigint NOT NULL REFERENCES products (product_id),
    variant text NOT NULL,
    UNIQUE (product_id, variant)
);

CREATE TABLE offers (
    offer_id bigint PRIMARY KEY,
    sku_id bigint NOT NULL REFERENCES skus (sku_id),
    seller_id integer NOT NULL REFERENCES sellers (seller_id),
    price_cents integer NOT NULL CHECK (price_cents > 0),
    list_price_cents integer NOT NULL,
    available integer NOT NULL CHECK (available >= 0),
    CHECK (list_price_cents >= price_cents),
    UNIQUE (sku_id, seller_id)
);

CREATE INDEX offers_low_stock ON offers (seller_id, offer_id) INCLUDE (available)
    WHERE available < {low_stock};

CREATE TABLE cart_items (
    customer_id bigint NOT NULL REFERENCES customers (customer_id),
    offer_id bigint NOT NULL REFERENCES offers (offer_id),
    qty integer NOT NULL CHECK (qty BETWEEN 1 AND 10),
    added_at timestamptz NOT NULL,
    PRIMARY KEY (customer_id, offer_id)
);

CREATE TABLE order_transitions (
    from_status text NOT NULL,
    to_status text NOT NULL,
    PRIMARY KEY (from_status, to_status)
);

INSERT INTO order_transitions (from_status, to_status) VALUES
    ('pending', 'paid'),
    ('pending', 'cancelled'),
    ('paid', 'shipped'),
    ('paid', 'cancelled'),
    ('shipped', 'delivered'),
    ('delivered', 'returned');

CREATE TABLE orders (
    order_id bigint PRIMARY KEY,
    customer_id bigint NOT NULL REFERENCES customers (customer_id),
    status text NOT NULL CHECK (
        status IN ('pending', 'paid', 'shipped', 'delivered', 'cancelled', 'returned')
    ),
    created_at timestamptz NOT NULL,
    item_count integer NOT NULL CHECK (item_count > 0),
    total_cents bigint NOT NULL CHECK (total_cents > 0),
    ship_state char(2) NOT NULL,
    ship_zip char(5) NOT NULL
);

CREATE INDEX orders_by_customer ON orders (customer_id, created_at DESC, order_id DESC);

CREATE INDEX orders_by_time ON orders (created_at) INCLUDE (total_cents);

CREATE TABLE order_items (
    order_id bigint NOT NULL REFERENCES orders (order_id),
    line_no integer NOT NULL CHECK (line_no > 0),
    offer_id bigint NOT NULL REFERENCES offers (offer_id),
    seller_id integer NOT NULL REFERENCES sellers (seller_id),
    qty integer NOT NULL CHECK (qty > 0),
    unit_price_cents integer NOT NULL CHECK (unit_price_cents > 0),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (order_id, line_no)
);

CREATE INDEX order_items_by_seller ON order_items (seller_id, created_at, order_id, line_no);

CREATE TABLE shipments (
    order_id bigint PRIMARY KEY REFERENCES orders (order_id),
    carrier text NOT NULL,
    tracking text NOT NULL UNIQUE,
    shipped_at timestamptz NOT NULL,
    delivered_at timestamptz,
    CHECK (delivered_at IS NULL OR delivered_at >= shipped_at)
);

CREATE TABLE reviews (
    review_id bigint PRIMARY KEY,
    product_id bigint NOT NULL REFERENCES products (product_id),
    customer_id bigint NOT NULL REFERENCES customers (customer_id),
    rating smallint NOT NULL CHECK (rating BETWEEN 1 AND 5),
    title text NOT NULL,
    created_at timestamptz NOT NULL,
    UNIQUE (product_id, customer_id)
);

CREATE INDEX reviews_by_product ON reviews (product_id, created_at DESC, review_id DESC)
    INCLUDE (rating);

CREATE FUNCTION check_order_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status <> OLD.status AND NOT EXISTS (
        SELECT 1 FROM order_transitions t
        WHERE t.from_status = OLD.status AND t.to_status = NEW.status
    ) THEN
        RAISE EXCEPTION 'order % cannot move from % to %', OLD.order_id, OLD.status, NEW.status
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER orders_transition BEFORE UPDATE OF status ON orders
    FOR EACH ROW EXECUTE FUNCTION check_order_transition();

CREATE FUNCTION checkout(
    p_customer bigint,
    p_order bigint,
    p_created timestamptz,
    p_state char(2),
    p_zip char(5)
) RETURNS TABLE (result text, line_count integer, amount bigint)
LANGUAGE plpgsql AS $$
DECLARE
    line record;
    n integer := 0;
    total bigint := 0;
BEGIN
    IF EXISTS (SELECT 1 FROM orders o WHERE o.order_id = p_order) THEN
        RETURN QUERY SELECT 'duplicate'::text, 0, 0::bigint;
        RETURN;
    END IF;
    FOR line IN
        SELECT c.offer_id, c.qty, f.available, f.price_cents
        FROM cart_items c
        JOIN offers f ON f.offer_id = c.offer_id
        WHERE c.customer_id = p_customer
        ORDER BY c.offer_id
        FOR UPDATE OF f
    LOOP
        IF line.available < line.qty THEN
            RETURN QUERY SELECT 'out_of_stock'::text, 0, 0::bigint;
            RETURN;
        END IF;
        n := n + 1;
        total := total + line.qty * line.price_cents;
    END LOOP;
    IF n = 0 THEN
        RETURN QUERY SELECT 'empty'::text, 0, 0::bigint;
        RETURN;
    END IF;
    UPDATE offers f SET available = f.available - c.qty
    FROM cart_items c
    WHERE c.customer_id = p_customer AND f.offer_id = c.offer_id;
    INSERT INTO orders (
        order_id, customer_id, status, created_at, item_count, total_cents, ship_state, ship_zip
    ) VALUES (p_order, p_customer, 'paid', p_created, n, total, p_state, p_zip);
    INSERT INTO order_items (
        order_id, line_no, offer_id, seller_id, qty, unit_price_cents, created_at
    )
    SELECT p_order, row_number() OVER (ORDER BY c.offer_id), c.offer_id, f.seller_id, c.qty,
        f.price_cents, p_created
    FROM cart_items c
    JOIN offers f ON f.offer_id = c.offer_id
    WHERE c.customer_id = p_customer;
    DELETE FROM cart_items c WHERE c.customer_id = p_customer;
    RETURN QUERY SELECT 'placed'::text, n, total;
END
$$;

CREATE FUNCTION cancel_order(p_order bigint) RETURNS boolean
LANGUAGE plpgsql AS $$
DECLARE
    current_status text;
BEGIN
    SELECT o.status INTO current_status FROM orders o WHERE o.order_id = p_order FOR UPDATE;
    IF current_status IS NULL OR current_status NOT IN ('pending', 'paid') THEN
        RETURN false;
    END IF;
    PERFORM 1
    FROM offers f
    JOIN order_items i ON i.offer_id = f.offer_id
    WHERE i.order_id = p_order
    ORDER BY f.offer_id
    FOR UPDATE OF f;
    UPDATE offers f SET available = f.available + i.qty
    FROM order_items i
    WHERE i.order_id = p_order AND f.offer_id = i.offer_id;
    UPDATE orders SET status = 'cancelled' WHERE order_id = p_order;
    RETURN true;
END
$$;

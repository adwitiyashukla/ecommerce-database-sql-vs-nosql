CREATE TABLE categories (
    category_id INT PRIMARY KEY,
    parent_id INT NULL,
    name VARCHAR(100) NOT NULL,
    FOREIGN KEY (parent_id) REFERENCES categories (category_id)
);

CREATE TABLE sellers (
    seller_id INT PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    state CHAR(2) NOT NULL,
    rating DECIMAL(2, 1) NOT NULL CHECK (rating BETWEEN 1.0 AND 5.0),
    joined_at DATETIME NOT NULL
);

CREATE TABLE customers (
    customer_id BIGINT PRIMARY KEY,
    email VARCHAR(254) NOT NULL UNIQUE,
    name VARCHAR(100) NOT NULL,
    state CHAR(2) NOT NULL,
    zip CHAR(5) NOT NULL CHECK (REGEXP_LIKE(zip, '^[0-9]{5}$')),
    created_at DATETIME NOT NULL
);

CREATE TABLE products (
    product_id BIGINT PRIMARY KEY,
    category_id INT NOT NULL,
    title VARCHAR(200) NOT NULL,
    brand VARCHAR(40) NOT NULL,
    min_price_cents INT NOT NULL CHECK (min_price_cents > 0),
    created_at DATETIME NOT NULL,
    INDEX products_browse (category_id, min_price_cents, product_id),
    FOREIGN KEY (category_id) REFERENCES categories (category_id)
);

CREATE TABLE skus (
    sku_id BIGINT PRIMARY KEY,
    product_id BIGINT NOT NULL,
    variant VARCHAR(30) NOT NULL,
    UNIQUE (product_id, variant),
    FOREIGN KEY (product_id) REFERENCES products (product_id)
);

CREATE TABLE offers (
    offer_id BIGINT PRIMARY KEY,
    sku_id BIGINT NOT NULL,
    seller_id INT NOT NULL,
    price_cents INT NOT NULL CHECK (price_cents > 0),
    list_price_cents INT NOT NULL,
    available INT NOT NULL CHECK (available >= 0),
    low_stock TINYINT AS (available < {low_stock}) VIRTUAL,
    CHECK (list_price_cents >= price_cents),
    UNIQUE (sku_id, seller_id),
    INDEX offers_low_stock (seller_id, low_stock, offer_id),
    FOREIGN KEY (sku_id) REFERENCES skus (sku_id),
    FOREIGN KEY (seller_id) REFERENCES sellers (seller_id)
);

CREATE TABLE cart_items (
    customer_id BIGINT NOT NULL,
    offer_id BIGINT NOT NULL,
    qty INT NOT NULL CHECK (qty BETWEEN 1 AND 10),
    added_at DATETIME NOT NULL,
    PRIMARY KEY (customer_id, offer_id),
    FOREIGN KEY (customer_id) REFERENCES customers (customer_id),
    FOREIGN KEY (offer_id) REFERENCES offers (offer_id)
);

CREATE TABLE order_transitions (
    from_status VARCHAR(16) NOT NULL,
    to_status VARCHAR(16) NOT NULL,
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
    order_id BIGINT PRIMARY KEY,
    customer_id BIGINT NOT NULL,
    status VARCHAR(16) NOT NULL CHECK (
        status IN ('pending', 'paid', 'shipped', 'delivered', 'cancelled', 'returned')
    ),
    created_at DATETIME NOT NULL,
    item_count INT NOT NULL CHECK (item_count > 0),
    total_cents BIGINT NOT NULL CHECK (total_cents > 0),
    ship_state CHAR(2) NOT NULL,
    ship_zip CHAR(5) NOT NULL,
    INDEX orders_by_customer (customer_id, created_at DESC, order_id DESC),
    INDEX orders_by_time (created_at, total_cents),
    FOREIGN KEY (customer_id) REFERENCES customers (customer_id)
);

CREATE TABLE order_items (
    order_id BIGINT NOT NULL,
    line_no INT NOT NULL CHECK (line_no > 0),
    offer_id BIGINT NOT NULL,
    seller_id INT NOT NULL,
    qty INT NOT NULL CHECK (qty > 0),
    unit_price_cents INT NOT NULL CHECK (unit_price_cents > 0),
    created_at DATETIME NOT NULL,
    PRIMARY KEY (order_id, line_no),
    INDEX order_items_by_seller (seller_id, created_at, order_id, line_no),
    FOREIGN KEY (order_id) REFERENCES orders (order_id),
    FOREIGN KEY (offer_id) REFERENCES offers (offer_id),
    FOREIGN KEY (seller_id) REFERENCES sellers (seller_id)
);

CREATE TABLE shipments (
    order_id BIGINT PRIMARY KEY,
    carrier VARCHAR(16) NOT NULL,
    tracking VARCHAR(32) NOT NULL UNIQUE,
    shipped_at DATETIME NOT NULL,
    delivered_at DATETIME NULL,
    CHECK (delivered_at IS NULL OR delivered_at >= shipped_at),
    FOREIGN KEY (order_id) REFERENCES orders (order_id)
);

CREATE TABLE reviews (
    review_id BIGINT PRIMARY KEY,
    product_id BIGINT NOT NULL,
    customer_id BIGINT NOT NULL,
    rating TINYINT NOT NULL CHECK (rating BETWEEN 1 AND 5),
    title VARCHAR(100) NOT NULL,
    created_at DATETIME NOT NULL,
    UNIQUE (product_id, customer_id),
    INDEX reviews_by_product (product_id, created_at DESC, review_id DESC, rating),
    FOREIGN KEY (product_id) REFERENCES products (product_id),
    FOREIGN KEY (customer_id) REFERENCES customers (customer_id)
);

DELIMITER //

CREATE TRIGGER orders_transition BEFORE UPDATE ON orders FOR EACH ROW
BEGIN
    IF NEW.status <> OLD.status AND NOT EXISTS (
        SELECT 1 FROM order_transitions t
        WHERE t.from_status = OLD.status AND t.to_status = NEW.status
    ) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'invalid order status transition';
    END IF;
END //

CREATE PROCEDURE checkout(
    IN p_customer BIGINT,
    IN p_order BIGINT,
    IN p_created DATETIME,
    IN p_state CHAR(2),
    IN p_zip CHAR(5)
)
BEGIN
    DECLARE v_done INT DEFAULT 0;
    DECLARE v_offer BIGINT;
    DECLARE v_qty INT;
    DECLARE v_available INT;
    DECLARE v_price INT;
    DECLARE v_lines INT DEFAULT 0;
    DECLARE v_total BIGINT DEFAULT 0;
    DECLARE v_short INT DEFAULT 0;
    DECLARE v_seen INT DEFAULT 0;
    DECLARE cart_cursor CURSOR FOR
        SELECT offer_id, qty FROM cart_items WHERE customer_id = p_customer ORDER BY offer_id;
    DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_done = 1;
    DECLARE EXIT HANDLER FOR SQLEXCEPTION
    BEGIN
        ROLLBACK;
        RESIGNAL;
    END;
    START TRANSACTION;
    SELECT COUNT(*) INTO v_seen FROM orders WHERE order_id = p_order;
    IF v_seen > 0 THEN
        ROLLBACK;
        SELECT 'duplicate' AS result, 0 AS line_count, 0 AS amount;
    ELSE
        OPEN cart_cursor;
        read_cart: LOOP
            FETCH cart_cursor INTO v_offer, v_qty;
            IF v_done = 1 THEN
                LEAVE read_cart;
            END IF;
            SELECT available, price_cents INTO v_available, v_price
            FROM offers WHERE offer_id = v_offer FOR UPDATE;
            IF v_available < v_qty THEN
                SET v_short = 1;
            END IF;
            SET v_lines = v_lines + 1;
            SET v_total = v_total + v_qty * v_price;
        END LOOP;
        CLOSE cart_cursor;
        IF v_lines = 0 THEN
            ROLLBACK;
            SELECT 'empty' AS result, 0 AS line_count, 0 AS amount;
        ELSEIF v_short = 1 THEN
            ROLLBACK;
            SELECT 'out_of_stock' AS result, 0 AS line_count, 0 AS amount;
        ELSE
            UPDATE offers f
            JOIN cart_items c ON c.offer_id = f.offer_id
            SET f.available = f.available - c.qty
            WHERE c.customer_id = p_customer;
            INSERT INTO orders (
                order_id, customer_id, status, created_at, item_count, total_cents,
                ship_state, ship_zip
            ) VALUES (p_order, p_customer, 'paid', p_created, v_lines, v_total, p_state, p_zip);
            INSERT INTO order_items (
                order_id, line_no, offer_id, seller_id, qty, unit_price_cents, created_at
            )
            SELECT p_order, ROW_NUMBER() OVER (ORDER BY c.offer_id), c.offer_id, f.seller_id,
                c.qty, f.price_cents, p_created
            FROM cart_items c
            JOIN offers f ON f.offer_id = c.offer_id
            WHERE c.customer_id = p_customer;
            DELETE FROM cart_items WHERE customer_id = p_customer;
            COMMIT;
            SELECT 'placed' AS result, v_lines AS line_count, v_total AS amount;
        END IF;
    END IF;
END //

CREATE PROCEDURE cancel_order(IN p_order BIGINT)
BEGIN
    DECLARE v_done INT DEFAULT 0;
    DECLARE v_status VARCHAR(16);
    DECLARE v_offer BIGINT;
    DECLARE v_locked BIGINT;
    DECLARE items_cursor CURSOR FOR
        SELECT offer_id FROM order_items WHERE order_id = p_order ORDER BY offer_id;
    DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_done = 1;
    DECLARE EXIT HANDLER FOR SQLEXCEPTION
    BEGIN
        ROLLBACK;
        RESIGNAL;
    END;
    START TRANSACTION;
    SELECT status INTO v_status FROM orders WHERE order_id = p_order FOR UPDATE;
    IF v_status IS NULL OR v_status NOT IN ('pending', 'paid') THEN
        ROLLBACK;
        SELECT 0 AS cancelled;
    ELSE
        SET v_done = 0;
        OPEN items_cursor;
        lock_items: LOOP
            FETCH items_cursor INTO v_offer;
            IF v_done = 1 THEN
                LEAVE lock_items;
            END IF;
            SELECT offer_id INTO v_locked FROM offers WHERE offer_id = v_offer FOR UPDATE;
        END LOOP;
        CLOSE items_cursor;
        UPDATE offers f
        JOIN order_items i ON i.offer_id = f.offer_id
        SET f.available = f.available + i.qty
        WHERE i.order_id = p_order;
        UPDATE orders SET status = 'cancelled' WHERE order_id = p_order;
        COMMIT;
        SELECT 1 AS cancelled;
    END IF;
END //

DELIMITER ;

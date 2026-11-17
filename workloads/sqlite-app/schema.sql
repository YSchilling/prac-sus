-- Shop inventory schema for the sqlite-app workload.
-- Money is integer cents; timestamps are integer Unix seconds (simulated clock).
-- STRICT tables reject values of the wrong storage class.

CREATE TABLE products (
    id          INTEGER PRIMARY KEY,
    sku         TEXT    NOT NULL UNIQUE,
    name        TEXT    NOT NULL,
    category    TEXT    NOT NULL,
    price_cents INTEGER NOT NULL CHECK (price_cents > 0),
    stock       INTEGER NOT NULL CHECK (stock >= 0)
) STRICT;

CREATE TABLE orders (
    id          INTEGER PRIMARY KEY,
    created_at  INTEGER NOT NULL,
    customer_id INTEGER NOT NULL CHECK (customer_id > 0),
    item_count  INTEGER NOT NULL CHECK (item_count > 0),
    total_cents INTEGER NOT NULL CHECK (total_cents > 0)
) STRICT;

CREATE TABLE order_items (
    order_id         INTEGER NOT NULL REFERENCES orders (id),
    line_no          INTEGER NOT NULL CHECK (line_no > 0),
    product_id       INTEGER NOT NULL REFERENCES products (id),
    quantity         INTEGER NOT NULL CHECK (quantity > 0),
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents > 0),
    PRIMARY KEY (order_id, line_no)
) STRICT, WITHOUT ROWID;

CREATE TABLE stock_movements (
    id         INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products (id),
    created_at INTEGER NOT NULL,
    delta      INTEGER NOT NULL CHECK (delta <> 0),
    reason     TEXT    NOT NULL CHECK (reason IN ('initial', 'sale', 'restock')),
    order_id   INTEGER REFERENCES orders (id),
    CHECK ((reason = 'sale') = (order_id IS NOT NULL)),
    CHECK ((reason = 'sale') = (delta < 0))
) STRICT;

-- @indexes: app.py builds these after the bulk load, outside any measured phase.

CREATE INDEX products_category_price ON products (category, price_cents);
CREATE INDEX products_name ON products (name);
CREATE INDEX orders_created_at ON orders (created_at);
CREATE INDEX order_items_product ON order_items (product_id);
CREATE INDEX stock_movements_product ON stock_movements (product_id, created_at);

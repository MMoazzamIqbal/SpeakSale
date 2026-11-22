-- SpeakSale database schema (PostgreSQL)
-- Readable version of the structure; no data. Run it on an empty database to recreate the tables.

-- ---------------------------------------------------------------
-- Catalog
-- ---------------------------------------------------------------
CREATE TABLE categories (
    category_id  serial PRIMARY KEY,
    name         text NOT NULL UNIQUE
);

CREATE TABLE subcategories (
    subcategory_id  serial PRIMARY KEY,
    category_id     integer NOT NULL REFERENCES categories(category_id),
    name            text NOT NULL,
    UNIQUE (category_id, name)
);

CREATE TABLE products (
    product_id           text PRIMARY KEY,
    name                 text NOT NULL,
    subcategory_id       integer REFERENCES subcategories(subcategory_id),
    brand                text,
    base_unit            text NOT NULL DEFAULT 'piece',     -- every price and cost is per base unit
    full_price           numeric(10,2),
    wholesale_price      numeric(10,2),
    attributes           jsonb NOT NULL DEFAULT '{}',        -- allowed keys and values, e.g. {"color": ["black", "blue"]}
    is_active            boolean NOT NULL DEFAULT true,
    alias                text[],                             -- other names customers use for the product
    current_cost_price   numeric,
    package_qty_in_base  integer,                            -- how many base units in one package (box, packet...)
    package_unit         text                                -- the package word: box, packet, dozen...
);

-- ---------------------------------------------------------------
-- Sales
-- ---------------------------------------------------------------
CREATE TABLE sales (
    sale_id         serial PRIMARY KEY,
    sold_at         timestamptz NOT NULL DEFAULT now(),
    customer_type   text NOT NULL
                    CHECK (customer_type IN ('walk-in', 'wholesaler', 'regular', 'loyal')),
    raw_transcript  text,
    customer_label  text,
    file_source     text                                     -- path of the recording the sale came from
);

CREATE TABLE sale_items (
    sale_id             integer NOT NULL REFERENCES sales(sale_id),
    product_id          text REFERENCES products(product_id),
    quantity            numeric NOT NULL CHECK (quantity > 0),   -- in base units (pieces)
    unit                text NOT NULL,
    unit_price          numeric(10,2),                           -- final price per base unit, after any discount
    price_source        text,                                    -- where the price came from: spoken, db...
    discount_pct        numeric(5,2) NOT NULL DEFAULT 0,         -- not used: the final price is stored instead
    cost_price_at_sale  numeric(10,2),
    variant             jsonb                                    -- attributes of what was sold, e.g. {"color": "blue"}
);

-- ---------------------------------------------------------------
-- Recording queue and review
-- ---------------------------------------------------------------
CREATE TABLE recordings (
    file_id           serial PRIMARY KEY,
    file_path         text NOT NULL UNIQUE,
    recorded_at       timestamptz,
    status            text,                                    -- received, processing, done, failed, needs_review
    duration_seconds  numeric(10,2),
    issue             text,                                    -- reason when processing failed
    attempts          integer DEFAULT 0
);

CREATE TABLE pending_customers (
    pending_id        serial PRIMARY KEY,
    status            text NOT NULL DEFAULT 'pending',         -- pending, resolved, discarded
    problem           text,                                    -- why the customer needs review
    customer          jsonb,                                   -- the whole customer with AI decisions
    resolved_at       timestamptz,
    resolved_sale_id  integer
);

-- ---------------------------------------------------------------
-- Stock
-- ---------------------------------------------------------------
CREATE TABLE stock_movements (
    movement_id  serial PRIMARY KEY,
    product_id   text NOT NULL REFERENCES products(product_id),
    moved_at     timestamptz NOT NULL DEFAULT now(),
    qty_base     numeric NOT NULL,                             -- positive in, negative out
    reason       text NOT NULL
                 CHECK (reason IN ('purchase', 'sale', 'adjustment', 'return')),
    ref_id       integer
);

CREATE VIEW current_stock AS
SELECT product_id, sum(qty_base) AS on_hand
FROM stock_movements
GROUP BY product_id;

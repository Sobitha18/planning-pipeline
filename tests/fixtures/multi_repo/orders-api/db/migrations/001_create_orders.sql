CREATE TABLE orders (
  id TEXT PRIMARY KEY,
  total_cents INTEGER NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE INDEX orders_created_idx ON orders (created_at);

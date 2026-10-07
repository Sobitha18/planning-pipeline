def daily_revenue(conn):
    """Revenue per day, straight from the orders table."""
    return conn.execute(
        "SELECT date(created_at) AS day, sum(total_cents) FROM orders GROUP BY day"
    )

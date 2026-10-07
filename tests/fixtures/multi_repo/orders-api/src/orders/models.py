class Order:
    """A customer order with line items and a total."""

    def __init__(self, order_id: str, items: list[dict], total_cents: int):
        self.order_id = order_id
        self.items = items
        self.total_cents = total_cents

from src.orders.models import Order


@router.post("/api/orders")
def create_order(payload: dict) -> dict:
    """Create an order from the cart items; returns the new order id."""
    order = Order("o-1", payload["items"], payload["total_cents"])
    return {"order_id": order.order_id, "total_cents": order.total_cents}


@router.get("/api/orders/{order_id}")
def get_order(order_id: str) -> dict:
    return {"order_id": order_id}

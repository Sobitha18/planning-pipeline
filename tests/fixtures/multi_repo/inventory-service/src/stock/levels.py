class StockLevel:
    """Units on hand for one SKU in one warehouse."""

    def __init__(self, sku: str, on_hand: int):
        self.sku = sku
        self.on_hand = on_hand


def reserve_stock(level: StockLevel, qty: int) -> bool:
    if level.on_hand < qty:
        return False
    level.on_hand -= qty
    return True

from stock import reserve


def place_order(stock, sku, quantity):
    """下单：库存不足时失败。"""
    reserve(stock, sku, quantity)
    return True

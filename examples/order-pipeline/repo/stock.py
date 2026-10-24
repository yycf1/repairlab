def reserve(stock, sku, quantity):
    """从库存扣减指定数量，返回扣减后的库存量。"""
    stock[sku] = stock.get(sku, 0) - quantity
    return stock[sku]

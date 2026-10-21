def total_price(unit_price, quantity):
    if unit_price < 0 or quantity < 0:
        raise ValueError("price and quantity must be nonnegative")
    rate = 0.9 if quantity > 10 else 1.0
    return round(unit_price * quantity * rate, 2)

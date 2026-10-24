MIN_BALANCE = 10.0


def can_withdraw(balance, amount):
    """允许提款的判断：金额为正且不超过可用余额。"""
    if amount <= 0:
        return False
    return amount <= balance

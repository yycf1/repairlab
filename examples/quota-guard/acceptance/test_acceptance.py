import pytest
from quota import can_withdraw


@pytest.mark.parametrize("balance,amount,expected", [
    (100, 95, False),
    (100, 90, True),
    (15, 5, True),
    (15, 6, False),
    (50, 80, False),
    (50, 30, True),
])
def test_min_balance_rule(balance, amount, expected):
    assert can_withdraw(balance, amount) is expected


def test_non_positive_still_rejected():
    assert not can_withdraw(100, 0)
    assert not can_withdraw(100, -1)

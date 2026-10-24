from quota import can_withdraw


def test_rejects_non_positive():
    assert not can_withdraw(100, 0)
    assert not can_withdraw(100, -5)


def test_rejects_over_balance():
    assert not can_withdraw(50, 80)


def test_allows_normal_withdrawal():
    assert can_withdraw(50, 30)

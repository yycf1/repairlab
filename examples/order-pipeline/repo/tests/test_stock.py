from stock import reserve


def test_reserve_reduces_stock():
    stock = {"A": 10}
    assert reserve(stock, "A", 4) == 6


def test_reserve_rejects_insufficient():
    stock = {"A": 5}
    assert reserve(stock, "A", 8) is None
    assert stock["A"] == 5

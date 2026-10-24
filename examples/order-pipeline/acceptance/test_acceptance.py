from order import place_order
from stock import reserve


def test_place_order_rejects_non_positive():
    stock = {"A": 10}
    assert place_order(stock, "A", 0) is False
    assert stock["A"] == 10


def test_place_order_rejects_insufficient():
    stock = {"A": 5}
    assert place_order(stock, "A", 8) is False
    assert stock["A"] == 5


def test_place_order_success():
    stock = {"A": 10}
    assert place_order(stock, "A", 4) is True
    assert stock["A"] == 6


def test_reserve_rejects_insufficient():
    stock = {"B": 2}
    assert reserve(stock, "B", 3) is None

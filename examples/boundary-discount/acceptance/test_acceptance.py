import pytest
from pricing import total_price


@pytest.mark.parametrize("price,qty,expected", [(12, 10, 108), (12, 11, 118.8), (12, 9, 108), (0, 10, 0), (12, 0, 0)],
                         ids=["threshold", "above", "below", "zero-price", "zero-quantity"])
def test_required_behavior(price, qty, expected):
    assert total_price(price, qty) == expected

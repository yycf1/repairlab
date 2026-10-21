import pytest
from pricing import total_price


def test_discount_starts_at_ten():
    assert total_price(10, 10) == 90


def test_below_threshold():
    assert total_price(10, 9) == 90


def test_negative_quantity():
    with pytest.raises(ValueError):
        total_price(10, -1)

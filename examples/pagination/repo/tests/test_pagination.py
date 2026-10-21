import pytest
from pagination import page_count


def test_partial_page():
    assert page_count(21, 10) == 3


def test_full_page():
    assert page_count(20, 10) == 2


def test_invalid_size():
    with pytest.raises(ValueError):
        page_count(20, 0)

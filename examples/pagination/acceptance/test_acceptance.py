import pytest
from pagination import page_count


@pytest.mark.parametrize("total,size,expected", [(0, 10, 0), (1, 10, 1), (10, 10, 1), (11, 10, 2), (999, 100, 10)],
                         ids=["empty", "one", "full", "partial", "large"])
def test_required_behavior(total, size, expected):
    assert page_count(total, size) == expected

import pytest
from tags import parse_tags


@pytest.mark.parametrize("text,expected", [("", []), (" , , ", []), ("A, A", ["A", "A"]), ("\tx, y\n", ["x", "y"])],
                         ids=["empty", "blank", "duplicates", "whitespace"])
def test_required_behavior(text, expected):
    assert parse_tags(text) == expected

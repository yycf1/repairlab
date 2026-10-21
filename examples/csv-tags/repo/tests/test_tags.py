from tags import parse_tags


def test_strip_spaces():
    assert parse_tags(" Python , Agent ") == ["Python", "Agent"]


def test_ignore_empty():
    assert parse_tags("a,, b, ") == ["a", "b"]


def test_keep_case_and_order():
    assert parse_tags("B,a,B") == ["B", "a", "B"]

from labels import parse_labels


def test_whitespace_and_empty_labels():
    assert parse_labels(" A, ,B,, C ") == ["A", "B", "C"]


def test_empty_input():
    assert parse_labels("") == []


def test_case_order_and_duplicates():
    assert parse_labels(" B,a,B ") == ["B", "a", "B"]

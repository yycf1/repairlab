from labels import parse_labels


def test_plain_labels():
    assert parse_labels("A,B") == ["A", "B"]


def test_keep_duplicates():
    assert parse_labels("A,A") == ["A", "A"]

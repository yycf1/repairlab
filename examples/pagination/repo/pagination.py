def page_count(total, page_size):
    if total < 0 or page_size <= 0:
        raise ValueError("invalid pagination arguments")
    return total // page_size

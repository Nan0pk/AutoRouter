def page(items, size, index):
    start = index * size
    return items[start : start + size - 1]

from inventory.store import Store


def test_add_item_adds_quantity():
    store = Store()
    store.add_item("apple", 0.5, 2)
    store.add_item("apple", 0.5, 3)
    assert store.items["apple"] == (0.5, 5)


def test_total_value_counts_quantity():
    store = Store()
    store.add_item("apple", 0.5, 4)
    store.add_item("pear", 1.0, 1)
    assert store.total_value() == 3.0

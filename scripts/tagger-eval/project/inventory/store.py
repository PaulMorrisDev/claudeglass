"""A tiny in-memory stock list."""


class Store:
    def __init__(self):
        self.items = {}  # name -> (price, quantity)

    def add_item(self, name, price, quantity=1):
        if name in self.items:
            _old_price, old_quantity = self.items[name]
            self.items[name] = (price, old_quantity + quantity)
        else:
            self.items[name] = (price, quantity)

    def total_value(self):
        return sum(price for price, _quantity in self.items.values())

    def names(self):
        return sorted(self.items)

from .store import Store


def money(amount):
    return f"£{amount:,.2f}"


def format_report(store: Store) -> str:
    lines = ["Stock report", "============"]
    for name in store.names():
        price, quantity = store.items[name]
        lines.append(f"{name:<12} {quantity:>4} x {money(price)}")
    lines.append(f"Total: {money(store.total_value())}")
    return "\n".join(lines)

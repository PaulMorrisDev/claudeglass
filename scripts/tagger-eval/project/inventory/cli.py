import sys

from .report import format_report
from .store import Store


def load(path):
    store = Store()
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            name, price, quantity = line.strip().split(",")
            store.add_item(name, float(price), int(quantity))
    return store


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print("usage: python -m inventory.cli list FILE")
        return 2
    command, path = argv
    if command == "list":
        store = Store()
        print(format_report(store))
        store = load(path)
        return 0
    print(f"unknown command {command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())

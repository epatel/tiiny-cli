import argparse
import sys
from pathlib import Path

from inventory.models import InventoryError
from inventory.store import Store


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="inventory")
    p.add_argument("--file", default="inventory.json", help="the JSON file (default inventory.json)")
    sub = p.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="add an item")
    add.add_argument("sku")
    add.add_argument("name")
    add.add_argument("stock", type=int)
    sub.add_parser("list", help="list items")
    restock = sub.add_parser("restock", help="add stock")
    restock.add_argument("sku")
    restock.add_argument("qty", type=int)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = Store(Path(args.file))
    try:
        if args.command == "add":
            store.add(args.sku, args.name, args.stock)
        elif args.command == "restock":
            store.restock(args.sku, args.qty)
        elif args.command == "list":
            for item in sorted(store.items.values(), key=lambda i: i.sku):
                print(f"{item.sku}\t{item.name}\t{item.stock}")
            return 0
    except InventoryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    store.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())

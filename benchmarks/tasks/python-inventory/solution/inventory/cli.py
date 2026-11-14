import argparse
import sys
from pathlib import Path

from inventory.models import InventoryError
from inventory.store import Store


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="inventory")
    p.add_argument("--file", default="inventory.json")
    sub = p.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add")
    add.add_argument("sku")
    add.add_argument("name")
    add.add_argument("stock", type=int)
    sub.add_parser("list")
    restock = sub.add_parser("restock")
    restock.add_argument("sku")
    restock.add_argument("qty", type=int)
    reserve = sub.add_parser("reserve")
    reserve.add_argument("sku")
    reserve.add_argument("qty", type=int)
    reserve.add_argument("holder")
    reserve.add_argument("--ttl", type=int, default=30)
    release = sub.add_parser("release")
    release.add_argument("id")
    sub.add_parser("expire")
    sub.add_parser("report")
    return p


def run(store: Store, args) -> bool:
    """Run the command; True if the file must be saved."""
    items = sorted(store.items.values(), key=lambda i: i.sku)
    if args.command == "add":
        store.add(args.sku, args.name, args.stock)
    elif args.command == "restock":
        store.restock(args.sku, args.qty)
    elif args.command == "reserve":
        print(store.reserve(args.sku, args.qty, args.holder, args.ttl).id)
    elif args.command == "release":
        print(f"released {store.release(args.id).id}")
    elif args.command == "expire":
        for rid in store.expire():
            print(rid)
    elif args.command == "list":
        for i in items:
            print(f"{i.sku}\t{i.name}\t{i.stock}\t{store.available(i.sku)}")
        return False
    elif args.command == "report":
        print("sku\tstock\treserved\tavailable")
        for i in items:
            print(f"{i.sku}\t{i.stock}\t{store.reserved(i.sku)}\t{store.available(i.sku)}")
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = Store(Path(args.file))
    try:
        if run(store, args):
            store.save()
    except InventoryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

import json
from pathlib import Path

from inventory.models import InventoryError, Item


class Store:
    """Items kept in a JSON file: {"items": [{"sku": ..., "name": ..., "stock": ...}, ...]}."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.items: dict[str, Item] = {}
        if self.path.exists():
            data = json.loads(self.path.read_text())
            for d in data.get("items", []):
                item = Item.from_dict(d)
                self.items[item.sku] = item

    def save(self) -> None:
        data = {"items": [i.to_dict() for i in sorted(self.items.values(), key=lambda i: i.sku)]}
        self.path.write_text(json.dumps(data, indent=2))

    def add(self, sku: str, name: str, stock: int) -> Item:
        if sku in self.items:
            raise InventoryError(f"{sku} already exists")
        if stock < 0:
            raise InventoryError("stock must be 0 or more")
        item = self.items[sku] = Item(sku, name, stock)
        return item

    def get(self, sku: str) -> Item:
        try:
            return self.items[sku]
        except KeyError:
            raise InventoryError(f"unknown SKU {sku}") from None

    def restock(self, sku: str, qty: int) -> Item:
        if qty <= 0:
            raise InventoryError("quantity must be positive")
        item = self.get(sku)
        item.stock += qty
        return item

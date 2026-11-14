from dataclasses import dataclass


class InventoryError(Exception):
    """A request the inventory refuses (unknown SKU, bad quantity, ...)."""


@dataclass
class Item:
    sku: str
    name: str
    stock: int

    def to_dict(self) -> dict:
        return {"sku": self.sku, "name": self.name, "stock": self.stock}

    @classmethod
    def from_dict(cls, d: dict) -> "Item":
        return cls(d["sku"], d["name"], int(d["stock"]))

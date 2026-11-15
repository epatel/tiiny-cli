from dataclasses import dataclass
from datetime import datetime


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


@dataclass
class Reservation:
    id: str
    sku: str
    qty: int
    holder: str
    expires_at: datetime

    def active(self, now: datetime) -> bool:
        return self.expires_at > now

    def to_dict(self) -> dict:
        return {"id": self.id, "sku": self.sku, "qty": self.qty, "holder": self.holder,
                "expires_at": self.expires_at.isoformat()}

    @classmethod
    def from_dict(cls, d: dict) -> "Reservation":
        return cls(d["id"], d["sku"], int(d["qty"]), d["holder"], datetime.fromisoformat(d["expires_at"]))

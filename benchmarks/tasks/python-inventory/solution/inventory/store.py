import json
from datetime import timedelta
from pathlib import Path

from inventory import clock
from inventory.models import InventoryError, Item, Reservation


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.items: dict[str, Item] = {}
        self.reservations: dict[str, Reservation] = {}
        self.next_reservation = 1
        if self.path.exists():
            data = json.loads(self.path.read_text())
            for d in data.get("items", []):
                item = Item.from_dict(d)
                self.items[item.sku] = item
            for d in data.get("reservations", []):
                r = Reservation.from_dict(d)
                self.reservations[r.id] = r
            self.next_reservation = int(data.get("next_reservation", 1))

    def save(self) -> None:
        data = {"items": [i.to_dict() for i in sorted(self.items.values(), key=lambda i: i.sku)],
                "reservations": [r.to_dict() for r in sorted(self.reservations.values(), key=lambda r: int(r.id[1:]))],
                "next_reservation": self.next_reservation}
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

    def reserved(self, sku: str, *, now=None) -> int:
        now = now or clock.now()
        self.get(sku)
        return sum(r.qty for r in self.reservations.values() if r.sku == sku and r.active(now))

    def available(self, sku: str, *, now=None) -> int:
        return self.get(sku).stock - self.reserved(sku, now=now)

    def reserve(self, sku: str, qty: int, holder: str, ttl_minutes: int = 30, *, now=None) -> Reservation:
        now = now or clock.now()
        if qty <= 0:
            raise InventoryError("quantity must be positive")
        if ttl_minutes <= 0:
            raise InventoryError("ttl must be positive")
        if qty > self.available(sku, now=now):
            raise InventoryError(f"only {self.available(sku, now=now)} of {sku} available")
        r = Reservation(f"R{self.next_reservation}", sku, qty, holder, now + timedelta(minutes=ttl_minutes))
        self.next_reservation += 1
        self.reservations[r.id] = r
        return r

    def release(self, res_id: str) -> Reservation:
        try:
            return self.reservations.pop(res_id)
        except KeyError:
            raise InventoryError(f"unknown reservation {res_id}") from None

    def expire(self, *, now=None) -> list[str]:
        now = now or clock.now()
        gone = sorted((r.id for r in self.reservations.values() if not r.active(now)), key=lambda i: int(i[1:]))
        for rid in gone:
            del self.reservations[rid]
        return gone

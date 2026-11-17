from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class Line:
    sku: str
    price: Decimal  # per unit
    qty: int

    @property
    def amount(self) -> Decimal:
        return self.price * self.qty


@dataclass
class Order:
    lines: list[Line] = field(default_factory=list)
    member: bool = False
    coupon: str | None = None

    @property
    def subtotal(self) -> Decimal:
        return sum((line.amount for line in self.lines), Decimal("0"))

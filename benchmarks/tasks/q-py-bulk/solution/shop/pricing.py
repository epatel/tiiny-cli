"""Order totals: the subtotal minus every discount rule in RULES."""

from collections.abc import Callable
from decimal import ROUND_HALF_UP, Decimal

from shop.models import Order

# Rule settings
MEMBER_RATE = Decimal("0.05")
COUPONS = {"WELCOME10": Decimal("10.00"), "SPRING5": Decimal("5.00")}
BULK_MIN_QTY = 10
BULK_RATE = Decimal("0.10")


def cents(amount: Decimal) -> Decimal:
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def member_discount(order: Order) -> Decimal:
    """Members get MEMBER_RATE off the subtotal."""
    return cents(order.subtotal * MEMBER_RATE) if order.member else Decimal("0")


def coupon_discount(order: Order) -> Decimal:
    """A known coupon takes a fixed amount off; unknown coupons are ignored."""
    return COUPONS.get(order.coupon or "", Decimal("0"))


def bulk_discount(order: Order) -> Decimal:
    """Lines with at least BULK_MIN_QTY units get BULK_RATE off, rounded per line."""
    bulk = (line for line in order.lines if line.qty >= BULK_MIN_QTY)
    return sum((cents(line.amount * BULK_RATE) for line in bulk), Decimal("0"))


RULES: list[Callable[[Order], Decimal]] = [member_discount, coupon_discount, bulk_discount]


def discounts(order: Order) -> dict[str, Decimal]:
    """Each rule's discount by rule name, leaving out rules that give nothing."""
    found = {rule.__name__: rule(order) for rule in RULES}
    return {name: amount for name, amount in found.items() if amount}


def total(order: Order) -> Decimal:
    """The subtotal minus all discounts, never below zero."""
    return max(order.subtotal - sum(discounts(order).values(), Decimal("0")), Decimal("0"))

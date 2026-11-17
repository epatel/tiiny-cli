from decimal import Decimal

from shop.models import Line, Order
from shop.pricing import discounts, total


def test_plain_order():
    order = Order([Line("tea", Decimal("4.50"), 2)])
    assert total(order) == Decimal("9.00") and discounts(order) == {}


def test_member_discount_is_rounded_to_cents():
    order = Order([Line("tea", Decimal("3.33"), 1)], member=True)
    assert discounts(order) == {"member_discount": Decimal("0.17")}


def test_coupon_and_member_stack():
    order = Order([Line("mug", Decimal("20.00"), 1)], member=True, coupon="SPRING5")
    assert total(order) == Decimal("14.00")


def test_total_never_goes_below_zero():
    assert total(Order([Line("pin", Decimal("2.00"), 1)], coupon="WELCOME10")) == Decimal("0")


def test_bulk_discount_from_ten_units():
    assert discounts(Order([Line("pen", Decimal("2.00"), 10)])) == {"bulk_discount": Decimal("2.00")}
    assert discounts(Order([Line("pen", Decimal("2.00"), 9)])) == {}


def test_bulk_discount_stacks():
    order = Order([Line("pen", Decimal("2.00"), 10)], member=True)
    assert total(order) == Decimal("17.00")

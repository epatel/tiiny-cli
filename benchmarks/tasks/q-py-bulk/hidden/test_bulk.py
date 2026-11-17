from decimal import Decimal

import pytest

from shop import pricing
from shop.models import Line, Order
from shop.pricing import discounts, total


def test_settings():
    assert pricing.BULK_MIN_QTY == 10 and pricing.BULK_RATE == Decimal("0.10")


def test_ten_units_get_the_discount_nine_do_not():
    assert total(Order([Line("pen", Decimal("2.00"), 10)])) == Decimal("18.00")
    assert total(Order([Line("pen", Decimal("2.00"), 9)])) == Decimal("18.00")


def test_only_bulk_lines_count():
    order = Order([Line("pen", Decimal("1.00"), 12), Line("mug", Decimal("20.00"), 1)])
    assert sum(discounts(order).values()) == Decimal("1.20")


def test_rounded_per_line():
    order = Order([Line("a", Decimal("0.15"), 11), Line("b", Decimal("0.15"), 11)])
    assert sum(discounts(order).values()) == Decimal("0.34")  # 0.17 + 0.17; rounding the sum gives 0.33


def test_stacks_with_member_and_coupon():
    order = Order([Line("pen", Decimal("2.00"), 10)], member=True, coupon="SPRING5")
    assert total(order) == Decimal("12.00")  # 20 - 2 bulk - 1 member - 5 coupon
    assert len(discounts(order)) == 3


def test_settings_are_read_when_the_rule_runs(monkeypatch):
    monkeypatch.setattr(pricing, "BULK_MIN_QTY", 5)
    monkeypatch.setattr(pricing, "BULK_RATE", Decimal("0.50"))
    assert total(Order([Line("pen", Decimal("2.00"), 5)])) == Decimal("5.00")

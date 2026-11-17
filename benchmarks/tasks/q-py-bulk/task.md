Add a bulk discount to the shop's pricing (`shop/pricing.py`).

- Any order line with at least `BULK_MIN_QTY` units gets `BULK_RATE` off that line's amount. Add both as module-level settings next to the other rule settings: `BULK_MIN_QTY = 10` and `BULK_RATE = Decimal("0.10")`. The rule must read them when it runs, so that changing them changes the discount.
- Round the discount to cents per line, the same way the member discount is rounded.
- It is one more discount rule: it stacks with the member discount and coupons the same way they stack with each other, and shows up in `discounts(order)` like them.
- Keep the existing behaviour working, and add tests of your own for the new rule in `tests/`.

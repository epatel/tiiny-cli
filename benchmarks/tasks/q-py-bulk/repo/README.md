# shop

Order pricing for a small web shop. `shop.pricing.total(order)` is the subtotal minus every discount
rule in `RULES`; each rule takes the order and returns how much it takes off.

Run the tests with `python -m pytest`.

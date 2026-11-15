Add expiring reservations to the `Store` in this project's `inventory` package. Only the store and the models change: no CLI and no saving to the file for now. Keep the existing behaviour working, and add at least three tests of your own in `tests/`.

## Clock
Add a module `inventory/clock.py` with a function `now() -> datetime` that returns the current time as a timezone-aware UTC `datetime`. Every place that needs the current time must call it as `clock.now()` through the module (for example `from inventory import clock` and then `clock.now()`), so that tests can replace it with `monkeypatch.setattr("inventory.clock.now", ...)`.

## Model
Add a dataclass `Reservation` to `inventory/models.py` with the fields `id: str`, `sku: str`, `qty: int`, `holder: str` and `expires_at: datetime` (timezone-aware UTC). A reservation is *active* while `expires_at > now`.

## Store (`inventory/store.py`)
Add these methods to `Store`. Each takes the current time as a keyword argument `now` (a `datetime`); when it is left out, use `clock.now()`.
- `reserve(sku, qty, holder, ttl_minutes=30, *, now=None) -> Reservation`: create a reservation that expires `ttl_minutes` after `now`. IDs are `"R1"`, `"R2"`, … in creation order, and numbers are never reused, not even after a release. Raise `InventoryError` if the SKU is unknown, if `qty <= 0`, if `ttl_minutes <= 0`, or if `qty` is more than `available(sku, now=now)`.
- `release(res_id) -> Reservation`: remove the reservation and return it. Raise `InventoryError` for an unknown ID.
- `available(sku, *, now=None) -> int`: the item's stock minus the quantities of its *active* reservations. Raise `InventoryError` for an unknown SKU.
- `reserved(sku, *, now=None) -> int`: the total quantity of the SKU's active reservations.
- `expire(*, now=None) -> list[str]`: remove every reservation that is no longer active, and return their IDs sorted by number (`R2` comes before `R10`).
- `reservations`: a dict from ID to `Reservation`.

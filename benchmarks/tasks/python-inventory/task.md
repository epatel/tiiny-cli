Add expiring reservations to the `inventory` package in this project (models, store, CLI). Keep the existing behaviour working. Update the existing `list` test to the new format described below, and add tests of your own for the new features.

## Clock
Add a module `inventory/clock.py` with a function `now() -> datetime` that returns the current time as a timezone-aware UTC `datetime`. Every place that needs the current time must call it as `clock.now()` through the module (for example `from inventory import clock` and then `clock.now()`), so that tests can replace it with `monkeypatch.setattr("inventory.clock.now", ...)`.

## Model
Add a dataclass `Reservation` to `inventory/models.py` with the fields `id: str`, `sku: str`, `qty: int`, `holder: str` and `expires_at: datetime` (timezone-aware UTC). A reservation is *active* while `expires_at > now`.

## Store (`inventory/store.py`)
Add these methods to `Store`. Each takes the current time as a keyword argument `now` (a `datetime`); when it is left out, use `clock.now()`.
- `reserve(sku, qty, holder, ttl_minutes=30, *, now=None) -> Reservation`: create a reservation that expires `ttl_minutes` after `now`. IDs are `"R1"`, `"R2"`, … in creation order. Numbers are never reused, not even after a release or across saves and loads. Raise `InventoryError` if the SKU is unknown, if `qty <= 0`, if `ttl_minutes <= 0`, or if `qty` is more than `available(sku, now=now)`.
- `release(res_id) -> Reservation`: remove the reservation and return it. Raise `InventoryError` for an unknown ID.
- `available(sku, *, now=None) -> int`: the item's stock minus the quantities of its *active* reservations. Raise `InventoryError` for an unknown SKU.
- `reserved(sku, *, now=None) -> int`: the total quantity of the SKU's active reservations.
- `expire(*, now=None) -> list[str]`: remove every reservation that is no longer active, and return their IDs sorted by number (`R2` comes before `R10`).
- `reservations`: a dict from ID to `Reservation`.

Persist reservations in the same JSON file under a top-level key `"reservations"`: a list of objects with `id`, `sku`, `qty`, `holder` and `expires_at` (ISO 8601 with a UTC offset, e.g. `"2026-01-01T12:30:00+00:00"`). Also persist the next ID number under the key `"next_reservation"`. A file written by the old version, without these keys, must still load.

## CLI (`inventory/cli.py`)
Each command saves the file after a change, like the existing commands do.
- `reserve SKU QTY HOLDER [--ttl MINUTES]` (default 30): print the new reservation's ID, e.g. `R1`.
- `release ID`: print `released ID`.
- `expire`: print each expired ID on its own line (nothing if there are none).
- `report`: a header line `sku\tstock\treserved\tavailable`, then one line per item, sorted by SKU, with tab-separated values, e.g. `A1\t10\t3\t7`.
- `list`: now also shows availability. Each line is `SKU\tNAME\tSTOCK\tAVAILABLE`.
- Errors, including those from the new commands, print `error: <message>` to stderr and exit with code 2. This includes a reserve that asks for more than is available.

Commands that read availability (`list`, `report`, `reserve`) ignore expired reservations, but only `expire` removes them from the file.

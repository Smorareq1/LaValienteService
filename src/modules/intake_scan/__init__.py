"""Scanning a paper ticket with a vision model (Plan 0003).

⚠️ **TEMPORARY MODULE.** It is a bridge for as long as the shop keeps filling in
the printed booklet by hand. Everything it needs lives under this package plus
its endpoints and its `SCAN_*` settings, and nothing in the core points back at
it — `scan_jobs.order_id → orders`, never the reverse (D2). Retiring it is
setting `SCAN_ENABLED=false` and, in the next window, deleting this directory.

The one rule that never bends: **the model does not save orders.** It produces a
draft that a person reviews and confirms, and the pricing engine of Plan 0001 §6
recalculates every amount — the totals read off the photo exist only to catch a
misread quantity (D4).
"""

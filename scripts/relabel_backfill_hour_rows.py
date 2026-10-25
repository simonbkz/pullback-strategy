"""One-off 2026-10-22: two backfilled concurrency_cap rows (JP225 + UK100 @ 2026-10-22 00:00 UTC)
fell inside the hour filter in force at the time (23-05 UTC), so the cap was not the binding gate.
Relabel them bad_hour_00UTC so the concurrency_cap counterfactual is not overstated. Idempotent."""
import sqlite3
db = sqlite3.connect("trade_journal.db")
n = db.execute("UPDATE signals SET skip_reason='bad_hour_00UTC' WHERE skip_reason='concurrency_cap' AND ts='2026-10-22T00:00:00+00:00' AND instrument IN ('JP225','UK100')").rowcount
db.commit(); print(f"relabelled {n} rows")
for r in db.execute("SELECT instrument, substr(ts,1,16), skip_reason FROM signals WHERE skip_reason LIKE 'concurrency_cap%' OR (ts='2026-10-22T00:00:00+00:00' AND instrument IN ('JP225','UK100')) ORDER BY ts"):
    print("  ", r)

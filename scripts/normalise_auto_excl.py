"""One-off 2026-10-24: collapse 'auto_excl(meanR=...,n=...,dormant=...)' skip reasons to 'auto_excl'
so compute_counterfactuals aggregates them (13 distinct strings for 16 rows). Idempotent."""
import sqlite3
db = sqlite3.connect("trade_journal.db")
n = db.execute("UPDATE signals SET skip_reason='auto_excl' WHERE skip_reason LIKE 'auto_excl(%'").rowcount
db.commit(); print(f"normalised {n} rows")
for r in db.execute("SELECT instrument, COUNT(*), SUM(cf_outcome IS NOT NULL), ROUND(AVG(cf_r_multiple),2) FROM signals WHERE skip_reason='auto_excl' GROUP BY 1"):
    print("  ", r)

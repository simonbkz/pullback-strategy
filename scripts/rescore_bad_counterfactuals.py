"""One-off 2026-10-22: null out counterfactual rows that were scored wrongly
(premature 'timeout' before the 72-bar horizon elapsed; future cf_exit_ts from a
stale-offset symbol) so the fixed compute_counterfactuals.py rescores them.
Then re-run the scorer and print the per-skip_reason table. Idempotent."""
import sqlite3, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path
root = Path(__file__).resolve().parent.parent
db = sqlite3.connect(root / "trade_journal.db")
now = datetime.now(timezone.utc).isoformat()
n1 = db.execute("UPDATE signals SET cf_outcome=NULL, cf_exit_price=NULL, cf_exit_ts=NULL, cf_r_multiple=NULL, cf_bars_to_exit=NULL, cf_computed_at=NULL "
                "WHERE cf_outcome='timeout' AND cf_bars_to_exit < 72").rowcount
n2 = db.execute("UPDATE signals SET cf_outcome=NULL, cf_exit_price=NULL, cf_exit_ts=NULL, cf_r_multiple=NULL, cf_bars_to_exit=NULL, cf_computed_at=NULL "
                "WHERE cf_exit_ts > ?", (now,)).rowcount
db.commit(); db.close()
print(f"reset premature-timeout rows: {n1}   future-exit rows: {n2}")
print("re-running compute_counterfactuals.py ...")
out = subprocess.run([sys.executable, str(root / "compute_counterfactuals.py")], cwd=root, capture_output=True, text=True)
lines = out.stdout.splitlines()
for l in lines:
    if l.startswith("Done:") or l.startswith("Processing") or "skip_reason" in l or (l and l[0] != " " and len(l.split()) >= 6):
        print(l)
if out.returncode != 0: print("STDERR:", out.stderr[-800:])

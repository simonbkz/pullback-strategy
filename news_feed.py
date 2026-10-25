"""News feed aggregator: pulls upcoming scheduled events from FRED + hardcoded
central bank meetings, writes AUTO_* entries into news_blackout.windows.

Run:
  python news_feed.py                # refresh news_blackout.windows (merge mode)
  python news_feed.py --dry-run      # show what would be added, don't write

Scheduled task should run this daily.

Behavior:
  - Keeps all hand-maintained entries (anything with name not starting with "AUTO_")
  - Replaces existing AUTO_* entries with freshly-fetched ones (so stale events
    automatically drop off; freshly-published FOMC dates automatically get added)
  - Writes to mt5_runtime.json AND mt5_runtime_protected.json
"""
from __future__ import annotations
import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from news_sources import fred_calendar, central_banks


CONFIG_PATH = Path("mt5_runtime.json")
PROTECTED_COPY = Path("mt5_runtime_protected.json")


def collect_windows() -> list[dict]:
    """Pull from all sources, return list of auto-generated windows."""
    windows = []
    windows.extend(central_banks.all_cb_windows())
    windows.extend(fred_calendar.upcoming_releases(days_ahead=45))
    return windows


def merge_into_config(auto_windows: list[dict], apply: bool = True) -> dict:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    nb = cfg.setdefault("news_blackout", {})
    nb.setdefault("enabled", True)
    existing = nb.get("windows", [])

    # Separate manual (name doesn't start with AUTO_) from previous auto entries
    manual = [w for w in existing if not w.get("name", "").startswith("AUTO_")]
    old_auto = [w for w in existing if w.get("name", "").startswith("AUTO_")]

    # Deduplicate: if an auto_window has the same name as a manual one, skip (manual wins)
    manual_names = {w.get("name") for w in manual}
    new_auto = [w for w in auto_windows if w.get("name") not in manual_names]

    nb["windows"] = manual + new_auto
    nb["_last_auto_refresh"] = datetime.now(timezone.utc).isoformat()

    summary = {
        "manual_kept": len(manual),
        "auto_prev": len(old_auto),
        "auto_new": len(new_auto),
        "total": len(nb["windows"]),
    }

    if apply:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        shutil.copy(CONFIG_PATH, PROTECTED_COPY)

    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="show changes without writing config")
    args = ap.parse_args()

    print(f"=== news_feed refresh @ {datetime.now(timezone.utc).isoformat()} ===")
    windows = collect_windows()
    print(f"Collected {len(windows)} AUTO_ windows:")
    #   count by source
    by_source = {}
    for w in windows:
        src = w.get("_source", "?")
        by_source[src] = by_source.get(src, 0) + 1
    for src, n in by_source.items():
        print(f"  {src}: {n}")

    # Show first 10 upcoming
    sorted_windows = sorted(windows, key=lambda w: w.get("dates", [""])[0])
    print(f"\nNext 10 upcoming:")
    for w in sorted_windows[:10]:
        print(f"  {w['dates'][0]} {w['utc_start']}-{w['utc_end']} UTC: {w['name']}  -> {w['applies_to'][:3]}{'...' if len(w['applies_to']) > 3 else ''}")

    summary = merge_into_config(windows, apply=not args.dry_run)
    mode = "DRY-RUN (no write)" if args.dry_run else "APPLIED"
    print(f"\n{mode}:")
    print(f"  manual windows kept:          {summary['manual_kept']}")
    print(f"  previous auto windows:        {summary['auto_prev']}")
    print(f"  new auto windows:             {summary['auto_new']}")
    print(f"  total news_blackout.windows:  {summary['total']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

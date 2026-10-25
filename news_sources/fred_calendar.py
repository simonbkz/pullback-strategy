"""FRED API client for upcoming US macro release schedules.

Free API, requires FRED API key (sign up at:
  https://fred.stlouisfed.org/docs/api/api_key.html).

Env var: FRED_API_KEY

Key endpoints used:
  /fred/releases/dates   -- list upcoming release dates for FRED release IDs

High-impact US releases and their FRED release IDs:
  NFP (Employment Situation)   = release_id 50
  CPI (Consumer Price Index)    = release_id 10
  Retail Sales                  = release_id 161
  Industrial Production         = release_id 53
  GDP (Advance)                 = release_id 53 (via BEA, also h.3)
  Personal Income & Outlays     = release_id 54 (contains PCE)
  Producer Price Index          = release_id 46
  Jobless Claims                = release_id 180 (weekly)
  FOMC Economic Projections    = release_id 101 (less reliable - FOMC statement timing better from Fed H.3)
"""
from __future__ import annotations
import os
import json
from datetime import datetime, timedelta, timezone, time as dtime
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError:
    requests = None


# Map FRED release_id -> (short_name, typical_utc_release_time)
# Release times are approximate US ET -> UTC. During DST (summer) EST + 4h = UTC.
# Rough mapping assumes current year is in US EDT most of the time.
HIGH_IMPACT_RELEASES = {
    50:  ("NFP",            dtime(12, 30)),   # 8:30 AM ET
    10:  ("CPI",            dtime(12, 30)),
    9:   ("RETAIL_SALES",   dtime(12, 30)),   # Advance Retail Sales
    53:  ("GDP",            dtime(12, 30)),
    54:  ("PCE",            dtime(12, 30)),
    46:  ("PPI",            dtime(12, 30)),
}

# Which instruments each release affects (USD-denominated / US indices)
USD_AFFECTED = ["XAUUSD", "XAGUSD", "US500", "US100", "US30", "US2000",
                "USOIL", "GBPUSD", "EURUSD", "USDJPY"]
US_INDICES_AFFECTED = ["US500", "US100", "US30", "US2000", "DE40", "JP225", "EU50"]

# NFP + FOMC + CPI: affect everything risk-asset. Smaller releases: USD-centric only.
RELEASE_IMPACT_SCOPE = {
    "NFP":            USD_AFFECTED + US_INDICES_AFFECTED,
    "CPI":            USD_AFFECTED + US_INDICES_AFFECTED,
    "RETAIL_SALES":   USD_AFFECTED,
    "GDP":            USD_AFFECTED + US_INDICES_AFFECTED,
    "PCE":            USD_AFFECTED,
    "PPI":            USD_AFFECTED,
    "JOBLESS_CLAIMS": [],  # too frequent/low-impact to blackout
}


def _fred_get(path: str, params: dict) -> dict | None:
    """Hit the FRED API. Returns parsed JSON or None on failure."""
    if requests is None:
        return None
    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        return None
    base_params = {"api_key": api_key, "file_type": "json"}
    base_params.update(params)
    url = f"https://api.stlouisfed.org/fred{path}"
    try:
        r = requests.get(url, params=base_params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  [fred] request failed: {e}")
        return None


def upcoming_releases(days_ahead: int = 14) -> list[dict]:
    """Return list of upcoming high-impact release windows in the next N days.

    Each entry is a `news_blackout` window with name, dates, utc_start/end, applies_to.
    """
    if not os.environ.get("FRED_API_KEY"):
        return []

    today = datetime.now(timezone.utc).date()
    end = today + timedelta(days=days_ahead)
    out = []

    for release_id, (short_name, release_time) in HIGH_IMPACT_RELEASES.items():
        if not RELEASE_IMPACT_SCOPE.get(short_name):
            continue  # skip low-impact (jobless_claims)
        # Use /release/dates (singular) — returns actual release schedule.
        # The plural /releases/dates returns revision history (not what we want).
        data = _fred_get(f"/release/dates", {
            "release_id": release_id,
            "include_release_dates_with_no_data": "true",
            "sort_order": "asc",
        })
        if not data or "release_dates" not in data:
            continue
        for rd in data["release_dates"]:
            rel_date = rd.get("date")
            if not rel_date:
                continue
            # Filter to forward-looking window only
            if rel_date < today.isoformat() or rel_date > end.isoformat():
                continue
            # Build window: ±35 min around release time
            start_minute = (release_time.hour * 60 + release_time.minute) - 5
            end_minute = (release_time.hour * 60 + release_time.minute) + 30
            utc_start = f"{start_minute // 60:02d}:{start_minute % 60:02d}"
            utc_end = f"{end_minute // 60:02d}:{end_minute % 60:02d}"
            out.append({
                "name": f"AUTO_FRED_{short_name}_{rel_date}",
                "dates": [rel_date],
                "utc_start": utc_start,
                "utc_end": utc_end,
                "applies_to": RELEASE_IMPACT_SCOPE[short_name],
                "_source": "FRED",
                "_release_id": release_id,
            })
    return out


if __name__ == "__main__":
    # CLI test
    import sys
    if not os.environ.get("FRED_API_KEY"):
        print("ERROR: FRED_API_KEY env var not set. Get a free key at")
        print("  https://fred.stlouisfed.org/docs/api/api_key.html")
        print("Then: [System.Environment]::SetEnvironmentVariable('FRED_API_KEY', '...', 'User')")
        sys.exit(1)
    events = upcoming_releases(days_ahead=21)
    print(f"Fetched {len(events)} upcoming US macro events (next 21 days):")
    for e in events[:30]:
        print(f"  {e['dates'][0]} {e['utc_start']}-{e['utc_end']} UTC: {e['name']}")

"""Hardcoded central bank meeting schedules for 2026-2027.

Compiled from official central bank calendars (published annually). Update by
visiting each central bank's monetary policy calendar page once a year.

Sources (to refresh):
  ECB:  https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html
  BoE:  https://www.bankofengland.co.uk/monetary-policy/upcoming-mpc-dates
  BoJ:  https://www.boj.or.jp/en/mopo/mpmsche_minu/
  SNB:  https://www.snb.ch/en/the-snb/mandates-goals/monetary-policy/term-dates
  Fed:  https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
  RBA:  https://www.rba.gov.au/monetary-policy/rba-board-minutes/
  BoC:  https://www.bankofcanada.ca/core-functions/monetary-policy/key-interest-rate/
  RBNZ: https://www.rbnz.govt.nz/monetary-policy/monetary-policy-decisions-dates

For each meeting, we encode (date, utc_start, utc_end, applies_to).
Press conference / rate decision times vary by CB — windows are generous.
"""
from __future__ import annotations
from datetime import date


# ---- ECB (Governing Council monetary-policy meetings) ----
# Rate decision 13:15 CET, press conf 13:45 CET -> blackout 11:10-13:30 UTC
ECB_MEETINGS_2026_2027 = [
    # 2026
    "2026-01-29", "2026-03-12", "2026-04-23", "2026-06-04",
    "2026-07-23", "2026-09-10", "2026-10-29", "2026-12-17",
    # 2027 (approximate — refresh when ECB publishes official 2027 calendar)
    "2027-01-28", "2027-03-11", "2027-04-22", "2027-06-03",
    "2027-07-22", "2027-09-09", "2027-10-28", "2027-12-16",
]
ECB_WINDOW = ("12:10", "13:30")
ECB_AFFECTS = ["DE40", "EU50", "CHFJPY", "EURUSD", "EURJPY"]


# ---- BoE (MPC rate decisions) ----
# Rate decision 12:00 UK local Thursday -> 11:00/12:00 UTC (DST dependent)
BOE_MEETINGS_2026_2027 = [
    # 2026 (approximate; refresh from BoE site)
    "2026-02-05", "2026-03-19", "2026-05-07", "2026-06-18",
    "2026-08-06", "2026-09-17", "2026-11-05", "2026-12-17",
    # 2027
    "2027-02-04", "2027-03-18", "2027-05-06", "2027-06-17",
    "2027-08-05", "2027-09-16", "2027-11-04", "2027-12-16",
]
BOE_WINDOW = ("11:55", "12:30")
BOE_AFFECTS = ["CHFJPY", "GBPUSD", "UK100"]


# ---- BoJ (Monetary Policy Meeting) ----
# Statement 11:00 JST (02:00 UTC), press conf 15:30 JST (06:30 UTC)
BOJ_MEETINGS_2026_2027 = [
    "2026-01-23", "2026-03-19", "2026-04-28", "2026-06-16",
    "2026-07-30", "2026-09-18", "2026-10-30", "2026-12-18",
    "2027-01-22", "2027-03-18", "2027-04-27", "2027-06-15",
    "2027-07-29", "2027-09-17", "2027-10-29", "2027-12-17",
]
BOJ_WINDOW = ("01:55", "07:00")  # wide: statement to press-conf tail
BOJ_AFFECTS = ["JP225", "CHFJPY", "EURJPY"]


# ---- SNB (quarterly monetary-policy assessment) ----
SNB_MEETINGS_2026_2027 = [
    "2026-03-26", "2026-06-18", "2026-09-24", "2026-12-10",
    "2027-03-25", "2027-06-17", "2027-09-23", "2027-12-09",
]
SNB_WINDOW = ("08:25", "10:00")
SNB_AFFECTS = ["CHFJPY", "EURUSD", "GBPUSD"]


# ---- FOMC (rate decision + statement) ----
# Statement 18:00 UTC during EDT, 19:00 UTC during EST. Press conf starts 18:30 UTC.
FOMC_MEETINGS_2026_2027 = [
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-10",
    "2026-07-29", "2026-09-16", "2026-11-05", "2026-12-16",
    "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09",
    "2027-07-28", "2027-09-15", "2027-11-04", "2027-12-15",
]
FOMC_WINDOW = ("17:55", "19:30")
FOMC_AFFECTS = ["XAUUSD", "XAGUSD", "US500", "US100", "US30", "US2000",
                "USOIL", "DE40", "JP225", "EU50", "GBPUSD", "EURUSD",
                "EURJPY", "BTCUSD", "ETHUSD"]


# ---- BoC (Bank of Canada rate decisions) ----
BOC_MEETINGS_2026_2027 = [
    "2026-01-28", "2026-03-11", "2026-04-15", "2026-06-03",
    "2026-07-29", "2026-09-09", "2026-10-28", "2026-12-09",
    "2027-01-27", "2027-03-10", "2027-04-14", "2027-06-02",
    "2027-07-28", "2027-09-08", "2027-10-27", "2027-12-08",
]
BOC_WINDOW = ("14:55", "15:30")
BOC_AFFECTS = ["USOIL", "UKOIL"]  # BoC mostly moves CAD, affects oil via commodity link


# ---- RBA (Reserve Bank of Australia) ----
RBA_MEETINGS_2026_2027 = [
    "2026-02-03", "2026-03-18", "2026-04-21", "2026-05-19",
    "2026-07-07", "2026-08-11", "2026-09-29", "2026-11-03", "2026-12-08",
    "2027-02-02", "2027-03-17", "2027-04-20", "2027-05-18",
    "2027-07-06", "2027-08-10", "2027-09-28", "2027-11-02", "2027-12-07",
]
RBA_WINDOW = ("03:25", "04:00")
RBA_AFFECTS = []  # Not trading AUD or AUS200 currently


def all_cb_windows() -> list[dict]:
    """Return all upcoming central bank meeting windows in news_blackout format.
    Filters to future-only to keep windows list lean."""
    today = date.today().isoformat()
    out = []
    schedules = [
        (ECB_MEETINGS_2026_2027,  "ECB",  ECB_WINDOW,  ECB_AFFECTS),
        (BOE_MEETINGS_2026_2027,  "BOE",  BOE_WINDOW,  BOE_AFFECTS),
        (BOJ_MEETINGS_2026_2027,  "BOJ",  BOJ_WINDOW,  BOJ_AFFECTS),
        (SNB_MEETINGS_2026_2027,  "SNB",  SNB_WINDOW,  SNB_AFFECTS),
        (FOMC_MEETINGS_2026_2027, "FOMC", FOMC_WINDOW, FOMC_AFFECTS),
        (BOC_MEETINGS_2026_2027,  "BOC",  BOC_WINDOW,  BOC_AFFECTS),
        (RBA_MEETINGS_2026_2027,  "RBA",  RBA_WINDOW,  RBA_AFFECTS),
    ]
    for dates, name, (s, e), affects in schedules:
        if not affects:
            continue
        for d in dates:
            if d < today:
                continue
            out.append({
                "name": f"AUTO_CB_{name}_{d}",
                "dates": [d],
                "utc_start": s,
                "utc_end": e,
                "applies_to": affects,
                "_source": "hardcoded_cb",
            })
    return out


if __name__ == "__main__":
    windows = all_cb_windows()
    print(f"Found {len(windows)} upcoming central bank meeting windows:")
    for w in windows[:30]:
        print(f"  {w['dates'][0]} {w['utc_start']}-{w['utc_end']} UTC: {w['name']}")

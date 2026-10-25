"""Walk the exact pre-signal gates of live_trader.consider_instrument for a set of
instruments and report where each one exits. Compares silent FX pairs against
instruments that are known to fire.
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import MetaTrader5 as mt5
import live_mt5_connector as cx
import live_trader as lt
from config import DEFAULT_PARAMS

cfg = json.load(open("mt5_runtime.json", encoding="utf-8"))
exe = cfg["execution"]
if not mt5.initialize(): print("mt5 init fail"); raise SystemExit

def resolve(ins):
    for c in cfg["symbol_map"].get(ins, [ins]):
        if mt5.symbol_info(c): return c
    return None

TEST = ["EURUSD","GBPUSD","USDJPY","GBPJPY","USDCAD","US30",   # silent
        "XAUUSD","ETHUSD","JP225","US500"]                     # known to fire

print(f"{'ins':8s} {'broker':10s} {'open':>4s} {'pend':>4s} {'closedBar':>19s} {'H1':>5s} {'H4':>5s} {'mktOpen':>8s} "
      f"{'spread':>7s} {'cap':>6s} {'sig_last200':>11s}  exit_reason")
for ins in TEST:
    b = resolve(ins)
    if not b: print(f"{ins:8s} UNRESOLVED"); continue
    row = [ins, b]
    try: op = cx.has_open_position(b, exe["magic_number"]); row.append(str(op))
    except Exception as e: row.append(f"ERR:{str(e)[:12]}"); op = None
    try: pe = cx.has_pending_order(b, exe["magic_number"]); row.append(str(pe))
    except Exception as e: row.append(f"ERR:{str(e)[:12]}"); pe = None
    try: cb = cx.latest_closed_bar_time(b, mt5.TIMEFRAME_H1); row.append(str(cb)[:19] if cb else "None")
    except Exception as e: row.append(f"ERR:{str(e)[:12]}"); cb = None
    try: ltf = cx.fetch_bars(b, mt5.TIMEFRAME_H1, 1500); row.append(str(len(ltf)))
    except Exception as e: row.append(f"ERR:{str(e)[:8]}"); ltf = None
    try: htf = cx.fetch_bars(b, mt5.TIMEFRAME_H4, 600); row.append(str(len(htf)))
    except Exception as e: row.append(f"ERR:{str(e)[:8]}"); htf = None
    try: mo, mr = cx.is_market_open(b); row.append(f"{mo}/{str(mr)[:6]}")
    except Exception as e: row.append(f"ERR:{str(e)[:8]}"); mo = None
    info = mt5.symbol_info(b); tick = mt5.symbol_info_tick(b)
    pip = info.point * (10 if info.digits in (3,5) else 1) if info else 0
    spread = (tick.ask - tick.bid)/pip if (tick and pip) else -1
    cap = exe["max_spread_pips_overrides"].get(ins, exe["max_spread_pips_default"])
    row += [f"{spread:.1f}", str(cap)]
    nsig = "-"
    if ltf is not None and htf is not None and len(ltf) >= 250 and len(htf) >= 60:
        try:
            sig = lt._get_entry_signal(cfg, ins, ltf, htf, DEFAULT_PARAMS)
            nsig = str(int(sig["side"].tail(200).isin(["long","short"]).sum()))
        except Exception as e: nsig = f"ERR:{str(e)[:10]}"
    row.append(nsig)
    # exit reason in gate order
    if op: why = "has_open_position"
    elif pe: why = "has_pending_order"
    elif cb is None: why = "latest_closed_bar_time=None"
    elif ltf is None or len(ltf) < 250: why = f"H1 bars {len(ltf) if ltf is not None else 'ERR'} < 250"
    elif htf is None or len(htf) < 60: why = f"H4 bars {len(htf) if htf is not None else 'ERR'} < 60  <<< SILENT RETURN"
    elif not mo: why = f"is_market_open=False ({mr})  <<< SILENT RETURN"
    elif spread > cap: why = "spread>cap (would log skip-retry)"
    elif nsig == "0": why = "passes gates; strategy produced 0 signals in last 200 bars"
    else: why = "passes gates; signals present"
    row.append(why)
    print(f"{row[0]:8s} {row[1]:10s} {row[2]:>4s} {row[3]:>4s} {row[4]:>19s} {row[5]:>5s} {row[6]:>5s} {row[7]:>8s} {row[8]:>7s} {row[9]:>6s} {row[10]:>11s}  {row[11]}")
mt5.shutdown()

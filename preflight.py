"""Pre-flight check before going live.

Verifies:
  1. MT5 connection + account info (balance, equity, leverage, algo trading)
  2. All ship-grade symbols resolve to broker symbols
  3. Live spreads are within configured caps
  4. Sample lot size at the chosen tier — confirms equity is enough
  5. No surprise open positions / pending orders for our magic number

Run this RIGHT BEFORE starting live_trader.py.
"""
import json
import MetaTrader5 as mt5
import live_mt5_connector as cx

print("=" * 70)
print("PRE-FLIGHT CHECK")
print("=" * 70)

with open("mt5_runtime.json", encoding="utf-8") as f:
    cfg = json.load(f)

_conn = cfg.get("connection", {})
if not cx.connect(path=_conn.get("path"),
                   expected_server=_conn.get("expected_server"),
                   expected_login=_conn.get("expected_login")):
    raise SystemExit(f"MT5 connect failed (or wrong terminal): {mt5.last_error()}")

ai = mt5.account_info()
ti = mt5.terminal_info()

print(f"\n[1] Account & terminal:")
print(f"    login    = {ai.login}")
print(f"    server   = {ai.server}")
print(f"    name     = {ai.name}")
print(f"    currency = {ai.currency}")
print(f"    leverage = 1:{ai.leverage}")
print(f"    balance  = ${ai.balance:,.2f}")
print(f"    equity   = ${ai.equity:,.2f}")
print(f"    margin   = ${ai.margin:,.2f}")
print(f"    free margin = ${ai.margin_free:,.2f}")
print(f"    trade allowed = {ai.trade_allowed}")
print(f"    trade expert allowed (Algo) = {ai.trade_expert}")
print(f"    terminal connected = {ti.connected}")
print(f"    terminal trade allowed = {ti.trade_allowed}")
print(f"    terminal Expert/Algo allowed = {ti.tradeapi_disabled == False}")

# Block 2: existing positions
print(f"\n[2] Existing positions / orders for magic={cfg['execution']['magic_number']}:")
mag = cfg["execution"]["magic_number"]
positions = mt5.positions_get() or []
ours_pos = [p for p in positions if p.magic == mag]
all_orders = mt5.orders_get() or []
ours_ord = [o for o in all_orders if o.magic == mag]
print(f"    our positions = {len(ours_pos)}")
for p in ours_pos:
    print(f"      {p.symbol} {p.type_str if hasattr(p,'type_str') else p.type} vol={p.volume} entry={p.price_open} sl={p.sl} tp={p.tp}")
print(f"    our pending orders = {len(ours_ord)}")
print(f"    OTHER positions on account (not ours) = {len(positions) - len(ours_pos)}")

# Block 3: symbol resolution + spreads
print(f"\n[3] Symbol resolution + live spreads vs caps:")
overrides = cfg["execution"]["max_spread_pips_overrides"]
default = cfg["execution"]["max_spread_pips_default"]
all_ok = True
for ins in cfg["instruments"]:
    broker = cx.resolve_symbol(ins, cfg.get("symbol_map"))
    if broker is None:
        print(f"    [{ins}] NOT AVAILABLE on broker"); all_ok = False; continue
    info = mt5.symbol_info(broker)
    tick = mt5.symbol_info_tick(broker)
    pip = info.point * (10 if info.digits in (3, 5) else 1)
    spread = (tick.ask - tick.bid)
    spread_pips = spread / pip if pip > 0 else 999
    cap = overrides.get(ins, default)
    status = "OK" if spread_pips <= cap else f"OVER (>{cap})"
    print(f"    [{ins:>7s}] {broker:<14s} bid/ask={tick.bid:>10.4f}/{tick.ask:<10.4f} spread={spread_pips:>7.2f}p  cap={cap:>5.1f}p  {status}")

# Block 4: lot sizing sanity at chosen tier
print(f"\n[4] Lot-size feasibility at tier '{cfg['tier']['name']}' (base risk {cfg['tier']['base_risk_pct']}%):")
test_risk = cfg["tier"]["base_risk_pct"]
trending_risk = test_risk * 1.5  # what regime-multiplier would give us in trending
for ins in cfg["instruments"]:
    broker = cx.resolve_symbol(ins, cfg.get("symbol_map"))
    if broker is None: continue
    info = mt5.symbol_info(broker); tick = mt5.symbol_info_tick(broker)
    if info is None or tick is None: continue
    # Use a 1.5*ATR-ish stop distance based on price
    stop_distance = tick.ask * 0.005  # 0.5% — rough strategy SL
    lot_base = cx.calc_lot(broker, test_risk, stop_distance, equity=ai.equity)
    lot_trend = cx.calc_lot(broker, trending_risk, stop_distance, equity=ai.equity)
    enough = "YES" if lot_base >= info.volume_min else f"NO (min {info.volume_min})"
    print(f"    [{ins:>7s}] @ {test_risk}% risk ${stop_distance:.2f} stop -> lot {lot_base:>5.2f}  (trending {lot_trend:>5.2f})  enough? {enough}")

# Final verdict
print(f"\n[5] Final verdict:")
issues = []
if not ai.trade_allowed:           issues.append("- account-level trading not allowed")
if not ai.trade_expert:            issues.append("- expert (algo) trading disabled on account")
if ti.tradeapi_disabled:           issues.append("- terminal Algo Trading button is OFF (toggle it green in MT5 toolbar)")
if not ti.trade_allowed:           issues.append("- terminal trading disabled")
if ai.equity < 100:                issues.append(f"- equity ${ai.equity:.2f} too low to trade meaningfully — fund the account ($500+ recommended)")
if not all_ok:                     issues.append("- some symbols not available")

if issues:
    print("    NOT READY:")
    for i in issues:
        print(f"    {i}")
    print("\n    Fix the above, then re-run preflight.py before going live.")
else:
    print("    READY TO GO LIVE.")
    print(f"    Run:  .\\.venv\\Scripts\\python.exe -u live_trader.py")

cx.shutdown()

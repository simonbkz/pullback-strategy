"""Find every MT5 terminal installed on this machine and the connected
account info if Python can attach to each.

Used to discover which terminal64.exe path corresponds to HFM vs XM vs other,
so we can lock the live trader to the intended one.
"""
import os
import glob
import subprocess
import psutil
import MetaTrader5 as mt5

print("=" * 70)
print("MT5 terminal discovery")
print("=" * 70)

# Find running MT5 processes
print("\n[1] Running terminal64.exe processes:")
for p in psutil.process_iter(["pid", "name", "exe"]):
    try:
        if p.info["name"] and "terminal" in p.info["name"].lower():
            print(f"   pid={p.info['pid']:>5}  exe={p.info['exe']}")
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        continue

# Find installed MT5 paths via common Program Files / AppData locations
print("\n[2] Installed terminal64.exe locations on disk:")
search_roots = [
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    os.path.expandvars(r"%APPDATA%\MetaQuotes"),
    os.path.expandvars(r"%LOCALAPPDATA%"),
]
seen = set()
for root in search_roots:
    if not os.path.isdir(root): continue
    for path in glob.glob(os.path.join(root, "**", "terminal64.exe"), recursive=True):
        if path in seen: continue
        seen.add(path)
        # Try to identify broker from parent folder name
        parent = os.path.basename(os.path.dirname(path))
        print(f"   {path}   (folder: {parent})")

# Try to attach to whatever's currently running
print("\n[3] Currently-attaching MT5 reports:")
if mt5.initialize(timeout=10_000):
    ai = mt5.account_info()
    ti = mt5.terminal_info()
    if ai:
        print(f"   account login = {ai.login}")
        print(f"   server        = {ai.server}")
        print(f"   name          = {ai.name}")
        print(f"   company       = {ai.company}")
    if ti:
        print(f"   terminal path = {ti.path}")
        print(f"   data path     = {ti.data_path}")
        print(f"   build         = {ti.build}")
    mt5.shutdown()
else:
    err = mt5.last_error()
    print(f"   no terminal accepted attach: {err}")

print("\n[4] To force a specific terminal, set this in mt5_runtime.json:")
print("       \"connection\": {")
print("         \"path\": \"<path to terminal64.exe>\",")
print("         \"expected_server\": \"HFM\"")
print("       }")

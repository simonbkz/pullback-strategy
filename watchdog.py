"""External watchdog for live_trader.py.

Runs as a separate process. Every minute, checks:
  1. Is live_trader.py running at all?
  2. Has runtime_state.json been saved in the last X minutes?
  3. Has the log file been written to recently?

If any check fails, prints a clear alert. If --auto-restart is passed,
optionally launches a fresh live_trader.py via the same venv.

Run with:
    .\\.venv\\Scripts\\python.exe watchdog.py
    .\\.venv\\Scripts\\python.exe watchdog.py --auto-restart
"""
from __future__ import annotations
import argparse, json, os, sys, time, subprocess
from datetime import datetime, timezone
from pathlib import Path


def find_live_trader_pids() -> list[int]:
    pids = []
    try:
        import psutil
    except ImportError:
        return pids
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info["name"] or "").lower()
            if "python" not in name: continue
            cmd = " ".join(proc.info["cmdline"] or [])
            if "live_trader.py" in cmd:
                pids.append(proc.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return pids


def newest_log_age_seconds() -> float | None:
    """Find newest live_trader_*.log under logs/ and return age in seconds."""
    p = Path("logs")
    if not p.is_dir(): return None
    logs = sorted(p.glob("live_trader_*.log"), key=lambda x: x.stat().st_mtime, reverse=True)
    if not logs: return None
    return time.time() - logs[0].stat().st_mtime


def runtime_state_age_seconds() -> float | None:
    f = Path("runtime_state.json")
    if not f.exists(): return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        saved_at = data.get("saved_at")
        if saved_at:
            ts = datetime.fromisoformat(saved_at.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        pass
    return time.time() - f.stat().st_mtime


def restart_live_trader(venv_python: str) -> int:
    """Launch a fresh live_trader.py via the venv python. Returns the new PID."""
    print(f"[watchdog] launching: {venv_python} -u live_trader.py")
    # DETACHED_PROCESS = 0x00000008 so child outlives this watchdog
    flags = 0x00000008 if sys.platform == "win32" else 0
    p = subprocess.Popen(
        [venv_python, "-u", "live_trader.py"],
        creationflags=flags,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=os.path.dirname(os.path.abspath(__file__)),
    )
    return p.pid


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=60, help="check interval seconds")
    ap.add_argument("--max-state-age", type=int, default=600,
                     help="alert if runtime_state.json hasn't saved in N seconds")
    ap.add_argument("--max-log-age", type=int, default=600,
                     help="alert if newest log file hasn't been written in N seconds")
    ap.add_argument("--auto-restart", action="store_true",
                     help="if live_trader is missing, auto-launch a fresh instance")
    ap.add_argument("--venv-python", default=r".\.venv\Scripts\python.exe",
                     help="path to venv python for auto-restart")
    args = ap.parse_args()

    print(f"[watchdog] starting (interval={args.interval}s, max_state_age={args.max_state_age}s, "
          f"auto_restart={args.auto_restart})")
    while True:
        try:
            pids = find_live_trader_pids()
            state_age = runtime_state_age_seconds()
            log_age = newest_log_age_seconds()
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            status = "OK"
            alerts = []

            if not pids:
                alerts.append("NO live_trader.py running")
                if args.auto_restart and Path(args.venv_python).exists():
                    pid = restart_live_trader(args.venv_python)
                    alerts.append(f"auto-launched new PID {pid}")
            elif len(pids) > 1:
                alerts.append(f"DUPLICATE instances: PIDs {pids}")

            if state_age is not None and state_age > args.max_state_age:
                alerts.append(f"runtime_state.json stale: {int(state_age)}s old")

            if log_age is None:
                alerts.append("no log file yet")
            elif log_age > args.max_log_age:
                alerts.append(f"newest log silent: {int(log_age)}s")

            if alerts:
                status = "ALERT"
            print(f"[{now}] {status}  pids={pids}  state_age={int(state_age) if state_age else '-'}s  "
                  f"log_age={int(log_age) if log_age else '-'}s"
                  + ("  ==>  " + " | ".join(alerts) if alerts else ""))

        except Exception as e:
            print(f"[watchdog] check error: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())

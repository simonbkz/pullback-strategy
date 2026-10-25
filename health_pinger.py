"""External health monitor — pings a configurable webhook every ~5 minutes.

Reports trader health to a 3rd-party service (default: Healthchecks.io style).
If no ping is received within the service's expected window, the external
service alerts the user — detecting server-down events that can't be caught
by any local script.

How it works:
  1. Scans local health (processes, monitor_status.json freshness, MT5 up)
  2. If HEALTHY: POSTs to the webhook URL as a success ping
  3. If DEGRADED or DOWN: POSTs to <webhook_url>/fail as a failure ping
  4. External service then triggers the alert (email/SMS/push/Slack)

Configurable via cfg.external_monitor.webhook_url. Without a URL, writes a
local heartbeat file and logs a reminder (no external alert).

Designed as a short-cycle scheduled task (every 5 min).

Setup (one-time, ~2 min):
  1. Sign up free at healthchecks.io (no credit card)
  2. Create a Check with:
     - Name: "fx-edge-live-trader"
     - Expected period: 10 minutes
     - Grace period: 5 minutes
     - Notification method: email (defaults to signup email) or add SMS/Slack/etc.
  3. Copy the ping URL (looks like https://hc-ping.com/<uuid>)
  4. Add to mt5_runtime.json:
       "external_monitor": {
         "enabled": true,
         "webhook_url": "https://hc-ping.com/<your-uuid>"
       }
  5. Hot-reloads automatically.

Alternative services:
  - Uptime Robot (uptimerobot.com) — slightly different URL format but compatible
  - Cronitor (cronitor.io) — same pattern
  - Any URL that accepts GET/POST and alerts on silence
"""
import json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path

try:
    import urllib.request
    import urllib.error
except ImportError:
    print("ERROR: urllib unavailable"); sys.exit(1)


HEARTBEAT_FILE = Path("logs/_external_heartbeat.txt")
HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)

MAX_MONITOR_AGE_SECONDS = 300  # if monitor_status.json is older than this, consider DEGRADED


def check_health():
    """Return (status, detail) where status is one of HEALTHY, DEGRADED, DOWN."""
    try:
        import subprocess
        res = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Measure-Object).Count"],
            capture_output=True, text=True, timeout=10,
        )
        py_count = int(res.stdout.strip()) if res.returncode == 0 else 0
    except Exception as e:
        py_count = -1

    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"Name='terminal64.exe'\" | Measure-Object).Count"],
            capture_output=True, text=True, timeout=10,
        )
        mt5_count = int(res.stdout.strip()) if res.returncode == 0 else 0
    except Exception:
        mt5_count = -1

    monitor_age = None
    monitor_data = None
    try:
        if os.path.exists("monitor_status.json"):
            monitor_age = int(time.time() - os.path.getmtime("monitor_status.json"))
            with open("monitor_status.json", encoding="utf-8") as f:
                monitor_data = json.load(f)
    except Exception:
        pass

    detail_lines = []
    detail_lines.append(f"python_pids={py_count}")
    detail_lines.append(f"mt5_pids={mt5_count}")
    detail_lines.append(f"monitor_status_age_s={monitor_age}")
    if monitor_data:
        acc = monitor_data.get("account", {})
        detail_lines.append(f"equity=${acc.get('equity', '?')}")
        detail_lines.append(f"balance=${acc.get('balance', '?')}")
        detail_lines.append(f"dd_pct={monitor_data.get('dd_pct', '?')}")
        detail_lines.append(f"open_positions={len(monitor_data.get('positions', []))}")

    # Classify
    if py_count == 0:
        return "DOWN", "no python processes alive", detail_lines
    if mt5_count == 0:
        return "DOWN", "MT5 terminal not running", detail_lines
    if py_count < 2:
        return "DEGRADED", f"only {py_count} python PID(s), expected 6 (3 processes × stub pair)", detail_lines
    if monitor_age is None:
        return "DEGRADED", "monitor_status.json missing", detail_lines
    if monitor_age > MAX_MONITOR_AGE_SECONDS:
        return "DEGRADED", f"monitor_status.json stale ({monitor_age}s > {MAX_MONITOR_AGE_SECONDS}s)", detail_lines
    return "HEALTHY", "all green", detail_lines


def send_ping(base_url: str, status: str, body: str, timeout_s: int = 15):
    """POST to webhook URL. Alert policy (2026-10-18 user requirement):

    - HEALTHY  → success ping (/)  — no alert
    - DEGRADED → success ping (/)  — no alert (transient / self-recovers)
    - DOWN     → fail ping (/fail) — immediate alert (trader dead)
    - server unreachable → no ping at all → healthchecks.io silence timeout alerts

    So the user gets alerted on: (a) literal server-unreachable (silence) and
    (b) trader-dead-but-server-up (/fail). But NOT on transient degraded
    conditions like a stale monitor_status.json briefly or 1 fewer PID than
    expected during a restart.
    """
    url = base_url.rstrip("/")
    if status == "DOWN":
        url = url + "/fail"
    data = body.encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                   headers={"Content-Type": "text/plain",
                                            "User-Agent": "fx-edge-health-pinger/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        return 0, f"url_error: {e}"
    except Exception as e:
        return 0, f"exception: {e}"


def main():
    cfg = json.load(open("mt5_runtime.json", encoding="utf-8"))
    em = cfg.get("external_monitor", {})
    now = datetime.now(timezone.utc).isoformat()
    status, detail, kv = check_health()
    body = f"status={status} ts={now} detail=\"{detail}\"\n" + "\n".join(kv)

    # Always write local heartbeat
    try:
        HEARTBEAT_FILE.write_text(body, encoding="utf-8")
    except Exception:
        pass

    print(f"[health_pinger] {now}  status={status}  detail={detail}")
    print(body)

    if not em.get("enabled", False):
        print("[health_pinger] external_monitor.enabled=false — not sending webhook")
        return
    url = em.get("webhook_url", "")
    if not url or url.startswith("<") or "example.com" in url:
        print("[health_pinger] no valid webhook_url configured — local-only heartbeat")
        return

    code, resp = send_ping(url, status, body)
    print(f"[health_pinger] webhook response: HTTP {code}  body={resp[:200]}")


if __name__ == "__main__":
    main()

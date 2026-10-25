"""Write sanitised copies of the runtime configs for the public repository.

  mt5_runtime.json            -> mt5_runtime.example.json
  mt5_runtime_<variant>.json  -> config_variants/mt5_runtime_<variant>.json

Replaces the broker account number and the health-check webhook with
placeholders. Everything else (every strategy parameter) is copied verbatim.
Re-run before each commit that should carry a config change:

    .\\.venv\\Scripts\\python.exe scripts\\sanitise_config_for_git.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGIN_PLACEHOLDER = "<YOUR_MT5_LOGIN>"
WEBHOOK_PLACEHOLDER = "https://hc-ping.com/<YOUR_HEALTHCHECKS_UUID>"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def sanitise(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "expected_login":
                out[k] = LOGIN_PLACEHOLDER
            elif k == "webhook_url":
                out[k] = WEBHOOK_PLACEHOLDER if "hc-ping.com/<" not in str(v) else v
            else:
                out[k] = sanitise(v)
        return out
    if isinstance(obj, list):
        return [sanitise(x) for x in obj]
    if isinstance(obj, str) and UUID_RE.search(obj):
        return UUID_RE.sub("<REDACTED_UUID>", obj)
    return obj


def write(src: Path, dst: Path) -> None:
    data = json.loads(src.read_text(encoding="utf-8"))
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(sanitise(data), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    text = dst.read_text(encoding="utf-8")
    leaked = [m.group(0) for m in UUID_RE.finditer(text) if "<" not in m.group(0)]
    if leaked or re.search(r'"expected_login":\s*\d', text):
        sys.exit(f"[sanitise] FAILED: secret still present in {dst}")
    print(f"[sanitise] {src.name:32s} -> {dst.relative_to(ROOT)}")


def main() -> None:
    write(ROOT / "mt5_runtime.json", ROOT / "mt5_runtime.example.json")
    for p in sorted(ROOT.glob("mt5_runtime_*.json")):
        write(p, ROOT / "config_variants" / p.name)


if __name__ == "__main__":
    main()

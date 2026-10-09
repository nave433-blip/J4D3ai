"""Assert every required app asset exists at the right dimensions.

Run:  python3 scripts/check_icons.py   (exit 0 = all good)
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
REQUIRED = {
    "assets/icon.png": (1024, 1024),
    "assets/adaptive-icon.png": (1024, 1024),
    "assets/splash.png": (1284, 2778),
    "assets/favicon.png": (48, 48),
}

failures: list[str] = []
for rel, want in REQUIRED.items():
    p = ROOT / rel
    if not p.exists():
        failures.append(f"MISSING {rel}")
        continue
    try:
        got = Image.open(p).size
    except Exception as exc:  # noqa: BLE001
        failures.append(f"UNREADABLE {rel}: {exc}")
        continue
    if tuple(got) != want:
        failures.append(f"WRONG SIZE {rel}: got {got[0]}x{got[1]}, want {want[0]}x{want[1]}")
    else:
        print(f"OK {rel} {got[0]}x{got[1]}")

if failures:
    print("\n".join(failures), file=sys.stderr)
    sys.exit(1)
print("all icon assets OK")

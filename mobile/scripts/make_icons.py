"""Derive all app icon/splash assets from the 1600x1600 master artwork.

Run:  python3 scripts/make_icons.py
Inputs:  assets-src/jade-icon-master.png (1600x1600, generated artwork)
Outputs: assets/icon.png (1024), assets/adaptive-icon.png (1024, art in
         central 66% safe zone), assets/splash.png (1284x2778),
         assets/favicon.png (48).
Stdlib + Pillow only.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "assets-src" / "jade-icon-master.png"
ASSETS = ROOT / "assets"

BG = (8, 8, 26, 255)  # dark navy matching the artwork background


def main() -> None:
    if not SRC.exists():
        sys.exit(f"master artwork missing: {SRC}")
    master = Image.open(SRC).convert("RGBA")
    if master.size != (1600, 1600):
        sys.exit(f"unexpected master size {master.size}, want (1600, 1600)")
    ASSETS.mkdir(parents=True, exist_ok=True)

    # 1. icon.png — 1024x1024 full-bleed artwork
    master.resize((1024, 1024), Image.LANCZOS).save(ASSETS / "icon.png")

    # 2. adaptive-icon.png — 1024x1024, artwork scaled to the central 66%
    #    safe zone on transparency (Android masks the rest).
    fg = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    art = master.resize((675, 675), Image.LANCZOS)  # ~66% of 1024
    fg.alpha_composite(art, ((1024 - 675) // 2, (1024 - 675) // 2))
    fg.save(ASSETS / "adaptive-icon.png")

    # 3. splash.png — 1284x2778 portrait, dark bg + centered orb
    splash = Image.new("RGBA", (1284, 2778), BG)
    orb = master.resize((560, 560), Image.LANCZOS)
    splash.alpha_composite(orb, ((1284 - 560) // 2, (2778 - 560) // 2))
    splash.save(ASSETS / "splash.png")

    # 4. favicon.png — 48x48
    master.resize((48, 48), Image.LANCZOS).save(ASSETS / "favicon.png")

    for name in ("icon.png", "adaptive-icon.png", "splash.png", "favicon.png"):
        im = Image.open(ASSETS / name)
        print(f"{name}: {im.size[0]}x{im.size[1]}")


if __name__ == "__main__":
    main()

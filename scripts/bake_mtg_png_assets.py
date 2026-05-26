#!/usr/bin/env python3
"""Bake SVG mana/rarity assets to PNG (maintainer script, not required at runtime).

Requires ImageMagick 7+: `magick` on PATH.
Run from repo root: python scripts/bake_mtg_png_assets.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANA_SVG = ROOT / "mtg_assets" / "img" / "manaSymbols"
MANA_PNG = ROOT / "mtg_assets" / "img" / "manaSymbols_png"
RARITY_SVG = ROOT / "mtg_assets" / "img" / "setSymbols" / "official"
RARITY_PNG = ROOT / "mtg_assets" / "img" / "setSymbols_png" / "official"

MANA_SIZE = 128
RARITY_SIZE = 256


def _magick_cmd() -> list[str]:
    if shutil.which("magick"):
        return ["magick"]
    if shutil.which("convert"):
        return ["convert"]
    print("Error: ImageMagick not found (need `magick` or `convert` on PATH)", file=sys.stderr)
    sys.exit(1)


def bake_svg(svg: Path, png: Path, size: int) -> None:
    png.parent.mkdir(parents=True, exist_ok=True)
    cmd = _magick_cmd() + [
        "-background", "none",
        "-density", "200",
        str(svg),
        "-resize", f"{size}x{size}",
        str(png),
    ]
    subprocess.run(cmd, check=True)


def main() -> None:
    count = 0
    for svg in sorted(MANA_SVG.glob("*.svg")):
        bake_svg(svg, MANA_PNG / f"{svg.stem}.png", MANA_SIZE)
        count += 1
    print(f"Baked {count} mana symbol PNGs → {MANA_PNG}")

    rarity_count = 0
    for name in ("unf-c", "unf-u", "unf-r", "unf-m"):
        svg = RARITY_SVG / f"{name}.svg"
        if svg.exists():
            bake_svg(svg, RARITY_PNG / f"{name}.png", RARITY_SIZE)
            rarity_count += 1
    print(f"Baked {rarity_count} rarity PNGs → {RARITY_PNG}")


if __name__ == "__main__":
    main()

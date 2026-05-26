"""Load pre-baked PNG assets (mana symbols, rarity icons). No SVG/cairo deps at runtime."""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image

log = logging.getLogger(__name__)


def load_raster(path: Path, size: int) -> Image.Image | None:
    """Load a PNG asset and resize to size×size RGBA."""
    if not path.exists():
        return None
    try:
        img = Image.open(path).convert("RGBA")
        if img.size != (size, size):
            img = img.resize((size, size), Image.LANCZOS)
        return img
    except Exception as e:
        log.warning("Failed to load raster asset %s: %s", path, e)
        return None

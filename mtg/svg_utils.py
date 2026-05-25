"""Rasterize SVG assets to Pillow images without libcairo (cairosvg)."""

from __future__ import annotations

import io
import logging
from pathlib import Path

from PIL import Image

log = logging.getLogger(__name__)


def svg_to_pil(path: Path, size: int) -> Image.Image | None:
    """Render an SVG file to a square RGBA Pillow image of *size*×*size* pixels."""
    if not path.exists():
        return None
    try:
        from svglib.svglib import svg2rlg
        from reportlab.graphics import renderPM
    except ImportError as e:
        log.error("SVG rendering requires svglib and reportlab: %s", e)
        return None

    try:
        drawing = svg2rlg(str(path))
        if drawing is None:
            log.warning("svglib could not parse: %s", path)
            return None

        src_w = float(drawing.width or 1)
        src_h = float(drawing.height or 1)
        scale = size / max(src_w, src_h)
        drawing.width = src_w * scale
        drawing.height = src_h * scale
        drawing.scale(scale, scale)

        png_data = renderPM.drawToString(drawing, fmt="PNG")
        img = Image.open(io.BytesIO(png_data)).convert("RGBA")
        if img.size != (size, size):
            img = img.resize((size, size), Image.LANCZOS)
        return img
    except Exception as e:
        log.warning("Failed to rasterize SVG %s: %s", path, e)
        return None

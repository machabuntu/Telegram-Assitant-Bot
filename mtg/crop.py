"""Image cropping helpers for /mcg."""

from __future__ import annotations

import io
from typing import Optional

from PIL import Image


def center_crop_aspect(image: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Center-crop *image* to the given aspect ratio (width:height)."""
    w, h = image.size
    target_ratio = target_w / target_h
    current_ratio = w / h

    if current_ratio > target_ratio:
        new_w = int(h * target_ratio)
        left = (w - new_w) // 2
        box = (left, 0, left + new_w, h)
    else:
        new_h = int(w / target_ratio)
        top = (h - new_h) // 2
        box = (0, top, w, top + new_h)

    return image.crop(box)


def crop_portrait_2_3(image_bytes: bytes) -> bytes:
    """Center crop portrait image to 2:3 aspect ratio."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    cropped = center_crop_aspect(img, 2, 3)
    out = io.BytesIO()
    cropped.save(out, format="PNG")
    return out.getvalue()


def crop_by_normalized_coords(
    image_bytes: bytes,
    xmin: int,
    ymin: int,
    xmax: int,
    ymax: int,
) -> bytes:
    """Crop image using coordinates normalized 0–1000."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = img.size

    left = max(0, min(w, int(xmin / 1000 * w)))
    top = max(0, min(h, int(ymin / 1000 * h)))
    right = max(left + 1, min(w, int(xmax / 1000 * w)))
    bottom = max(top + 1, min(h, int(ymax / 1000 * h)))

    cropped = img.crop((left, top, right, bottom))
    out = io.BytesIO()
    cropped.save(out, format="PNG")
    return out.getvalue()


def crop_landscape_3_4_fallback(image_bytes: bytes) -> bytes:
    """Center crop to 3:4 aspect ratio (fallback when AI coords fail)."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    cropped = center_crop_aspect(img, 3, 4)
    out = io.BytesIO()
    cropped.save(out, format="PNG")
    return out.getvalue()


def get_image_orientation(image_bytes: bytes) -> str:
    """Return 'portrait' if height >= width, else 'landscape'."""
    img = Image.open(io.BytesIO(image_bytes))
    w, h = img.size
    return "portrait" if h >= w else "landscape"


def parse_crop_json(text: str) -> Optional[dict]:
    """Extract crop coordinates dict from model response."""
    import json
    import re

    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        data = json.loads(text)
        if all(k in data for k in ("xmin", "ymin", "xmax", "ymax")):
            return {
                "xmin": int(data["xmin"]),
                "ymin": int(data["ymin"]),
                "xmax": int(data["xmax"]),
                "ymax": int(data["ymax"]),
            }
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    match = re.search(
        r'\{\s*"xmin"\s*:\s*(\d+)\s*,\s*"ymin"\s*:\s*(\d+)\s*,\s*"xmax"\s*:\s*(\d+)\s*,\s*"ymax"\s*:\s*(\d+)\s*\}',
        text,
    )
    if match:
        return {
            "xmin": int(match.group(1)),
            "ymin": int(match.group(2)),
            "xmax": int(match.group(3)),
            "ymax": int(match.group(4)),
        }
    return None

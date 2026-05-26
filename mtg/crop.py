"""Image cropping helpers for /mcg."""

from __future__ import annotations

import io
from typing import Optional

from PIL import Image

ASPECT_W = 5
ASPECT_H = 7
TARGET_RATIO = ASPECT_W / ASPECT_H


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


def is_aspect_5_7(width: int, height: int, tolerance: float = 0.02) -> bool:
    """Return True if width/height is within *tolerance* of 5:7."""
    if height <= 0:
        return False
    return abs(width / height - TARGET_RATIO) <= tolerance


def _image_to_png_bytes(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG")
    return out.getvalue()


def crop_center_5_7(image_bytes: bytes) -> bytes:
    """Center crop image to 5:7 aspect ratio."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    cropped = center_crop_aspect(img, ASPECT_W, ASPECT_H)
    return _image_to_png_bytes(cropped)


def ensure_aspect_5_7(image_bytes: bytes, tolerance: float = 0.02) -> bytes:
    """Ensure image is 5:7; center-crop edges if not."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = img.size
    if is_aspect_5_7(w, h, tolerance):
        return _image_to_png_bytes(img)
    cropped = center_crop_aspect(img, ASPECT_W, ASPECT_H)
    return _image_to_png_bytes(cropped)


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
    return _image_to_png_bytes(cropped)


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

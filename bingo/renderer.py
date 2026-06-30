"""Render meme bingo grids to PNG."""

from __future__ import annotations

import io
import logging

from PIL import Image, ImageDraw, ImageFont

from bingo.models import BingoGrid

log = logging.getLogger(__name__)

CANVAS_W = 1400
CANVAS_H = 1650
MARGIN = 24
TITLE_H = 100
CELL_GAP = 4
CELL_PADDING = 10
LINE_SPACING = 2

FONT_MIN = 10
FONT_MAX = 22
TITLE_FONT_MAX = 36
TITLE_FONT_MIN = 18

C_BG = (24, 24, 32)
C_TITLE = (160, 130, 255)
C_CELL_BG = (30, 30, 42)
C_CELL_BORDER = (70, 70, 95)
C_TEXT = (220, 220, 220)

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
    "/usr/share/fonts/truetype/ubuntu-font-family/Ubuntu-R.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
]
_FONT_BOLD_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
    "/usr/share/fonts/truetype/ubuntu-font-family/Ubuntu-B.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "arialbd.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


def _load_font(candidates: list[str], size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap_words(text: str, font, max_width: int, draw: ImageDraw.ImageDraw) -> str:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        if not words:
            lines.append("")
            continue

        current = ""
        for word in words:
            candidate = f"{current} {word}".strip() if current else word
            width = draw.textbbox((0, 0), candidate, font=font)[2]
            if width <= max_width:
                current = candidate
                continue

            if current:
                lines.append(current)
                current = word
                if draw.textbbox((0, 0), word, font=font)[2] <= max_width:
                    continue
            else:
                current = ""

            chunk = ""
            for ch in word:
                test = chunk + ch
                if draw.textbbox((0, 0), test, font=font)[2] <= max_width:
                    chunk = test
                else:
                    if chunk:
                        lines.append(chunk)
                    chunk = ch
            current = chunk

        if current:
            lines.append(current)

    return "\n".join(lines)


def _text_block_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    bbox = draw.multiline_textbbox((0, 0), text, font=font, spacing=LINE_SPACING, align="center")
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _truncate_with_ellipsis(text: str, font, max_width: int, draw: ImageDraw.ImageDraw) -> str:
    ellipsis = "…"
    if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
        return text
    trimmed = text
    while trimmed and draw.textbbox((0, 0), trimmed + ellipsis, font=font)[2] > max_width:
        trimmed = trimmed[:-1]
    return (trimmed + ellipsis) if trimmed else ellipsis


def _fit_wrapped_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    inner_w: int,
    inner_h: int,
    font_loader,
    max_size: int = FONT_MAX,
    min_size: int = FONT_MIN,
) -> tuple[ImageFont.FreeTypeFont | ImageFont.ImageFont, str]:
    for size in range(max_size, min_size - 1, -1):
        font = font_loader(size)
        wrapped = _wrap_words(text, font, inner_w, draw)
        w, h = _text_block_size(draw, wrapped, font)
        if w <= inner_w and h <= inner_h:
            return font, wrapped

    font = font_loader(min_size)
    wrapped = _wrap_words(text, font, inner_w, draw)
    lines = wrapped.split("\n")
    if lines:
        lines[-1] = _truncate_with_ellipsis(lines[-1], font, inner_w, draw)
    wrapped = "\n".join(lines)

    while wrapped:
        w, h = _text_block_size(draw, wrapped, font)
        if h <= inner_h:
            break
        lines = wrapped.split("\n")
        if len(lines) <= 1:
            lines[0] = _truncate_with_ellipsis(lines[0], font, inner_w, draw)
            wrapped = lines[0]
            break
        wrapped = "\n".join(lines[:-1])

    return font, wrapped


def _draw_centered_multiline(
    draw: ImageDraw.ImageDraw,
    text: str,
    box_x: int,
    box_y: int,
    box_w: int,
    box_h: int,
    font,
    fill,
) -> None:
    w, h = _text_block_size(draw, text, font)
    cx = box_x + box_w // 2
    cy = box_y + (box_h - h) // 2
    draw.multiline_text(
        (cx, cy),
        text,
        fill=fill,
        font=font,
        spacing=LINE_SPACING,
        align="center",
        anchor="ma",
    )


def _draw_title(draw: ImageDraw.ImageDraw, topic: str, width: int) -> None:
    title = f"БИНГО: {topic}"
    max_w = width - 2 * MARGIN
    font_loader = lambda size: _load_font(_FONT_BOLD_CANDIDATES, size)
    font, wrapped = _fit_wrapped_text(
        draw,
        title,
        max_w,
        TITLE_H - 20,
        font_loader,
        max_size=TITLE_FONT_MAX,
        min_size=TITLE_FONT_MIN,
    )
    _draw_centered_multiline(draw, wrapped, MARGIN, 10, max_w, TITLE_H - 20, font, C_TITLE)


def render_bingo_to_bytes(grid: BingoGrid) -> bytes:
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), color=C_BG)
    draw = ImageDraw.Draw(img)

    _draw_title(draw, grid.topic, CANVAS_W)

    grid_top = MARGIN + TITLE_H
    grid_left = MARGIN
    grid_width = CANVAS_W - 2 * MARGIN
    grid_height = CANVAS_H - grid_top - MARGIN
    cell_w = (grid_width - CELL_GAP * 4) // 5
    cell_h = (grid_height - CELL_GAP * 4) // 5
    inner_w = cell_w - 2 * CELL_PADDING
    inner_h = cell_h - 2 * CELL_PADDING
    font_loader = lambda size: _load_font(_FONT_CANDIDATES, size)

    for row in range(1, 6):
        for col in range(1, 6):
            x = grid_left + (col - 1) * (cell_w + CELL_GAP)
            y = grid_top + (row - 1) * (cell_h + CELL_GAP)
            draw.rectangle([x, y, x + cell_w, y + cell_h], fill=C_CELL_BG, outline=C_CELL_BORDER, width=1)

            text = grid.cells.get((row, col), "")
            if not text:
                continue

            font, wrapped = _fit_wrapped_text(draw, text, inner_w, inner_h, font_loader)
            _draw_centered_multiline(
                draw,
                wrapped,
                x + CELL_PADDING,
                y + CELL_PADDING,
                inner_w,
                inner_h,
                font,
                C_TEXT,
            )

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

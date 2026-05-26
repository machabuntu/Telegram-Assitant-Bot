"""Render MTG card PNGs using Pillow + mtg_assets."""

from __future__ import annotations

import io
import logging
import re
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

import yaml
from PIL import Image, ImageDraw, ImageFont

from mtg.assets import Assets
from mtg.models import CardDetails
from mtg.svg_utils import svg_to_pil

log = logging.getLogger(__name__)

_LAYOUT_PATH = Path(__file__).resolve().parent / "layout.yaml"


def _load_layout() -> dict:
    if _LAYOUT_PATH.exists():
        with open(_LAYOUT_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


_L = _load_layout()
Assets.configure_fonts(_L.get("fonts", {}).get("files"))

CANVAS_W: int = _L.get("canvas", {}).get("width", 1500)
CANVAS_H: int = _L.get("canvas", {}).get("height", 2100)

_ms = _L.get("mana_symbols", {})
MANA_SYMBOL_SIZE: int = _ms.get("size", 56)
MANA_SYMBOL_SPACING: int = _ms.get("spacing", 4)

_tx = _L.get("text", {})
GS_FLAVOR_GAP: int = _tx.get("flavor_gap", 45)
PARAGRAPH_GAP: int = _tx.get("paragraph_gap", 14)

_rs = _L.get("rarity_symbol", {})
RARITY_SIZE: int = _rs.get("size", 58)
RARITY_RIGHT: int = _rs.get("right", 1370)
RARITY_Y_OFFSET: int = _rs.get("y_offset", 28)

_ft = _L.get("footer", {})
FOOTER_LEFT_X: int = _ft.get("left_x", 100)
FOOTER_RIGHT_X: int = _ft.get("right_x", 1400)
FOOTER_LEFT_LINE1_Y: int = _ft.get("left_line1_y", 1960)
FOOTER_LEFT_LINE2_Y: int = _ft.get("left_line2_y", 1990)
FOOTER_LEFT_LINE3_Y: int = _ft.get("left_line3_y", 2020)
FOOTER_RIGHT_LINE1_Y: int = _ft.get("right_line1_y", 1990)
FOOTER_RIGHT_LINE2_Y: int = _ft.get("right_line2_y", 2020)
FOOTER_FONT: str = _ft.get("font", "gothammedium")
FOOTER_FONT_SIZE: int = _ft.get("font_size", 22)
FOOTER_NIB_SIZE: int = _ft.get("nib_size", 28)
FOOTER_COLOR_LIGHT: str = _ft.get("color_light", "#555555")
FOOTER_COLOR_DARK: str = _ft.get("color_dark", "#cccccc")

_gs = _L.get("standard", {})
_gs_art_r = _gs.get("art_bottom_ratio", 0.9224)
GS_ART = (0, 0, CANVAS_W, int(_gs_art_r * CANVAS_H))
GS_TITLE = tuple(_gs.get("title", {}).get("box", [128, 110, 1243, 114]))
GS_TITLE_FONT = _gs.get("title", {}).get("font", "belerenb")
GS_TITLE_FONT_SIZE: int = _gs.get("title", {}).get("font_size", 62)
GS_MANA_Y: int = _gs.get("mana", {}).get("y", 129)
GS_MANA_RIGHT: int = _gs.get("mana", {}).get("right", 1394)
GS_TYPE = tuple(_gs.get("type", {}).get("box", [128, 1189, 1243, 114]))
GS_TYPE_FONT = _gs.get("type", {}).get("font", "belerenb")
GS_TYPE_FONT_SIZE: int = _gs.get("type", {}).get("font_size", 50)
GS_RULES = tuple(_gs.get("rules", {}).get("box", [129, 1323, 1242, 604]))
GS_RULES_FONT = _gs.get("rules", {}).get("font", "mplantin")
GS_RULES_MAX: int = _gs.get("rules", {}).get("max_size", 58)
GS_RULES_MIN: int = _gs.get("rules", {}).get("min_size", 20)
_gs_fl = _gs.get("flavor", {})
GS_FLAVOR_FONT = _gs_fl.get("font", "mplantini")
GS_FLAVOR_MAX: int = _gs_fl.get("max_size", 52)
GS_FLAVOR_MIN: int = _gs_fl.get("min_size", 20)
_gs_pt = _gs.get("pt", {})
GS_PT_POS = tuple(_gs_pt.get("box_pos", [1136, 1858]))
GS_PT_TEXT = tuple(_gs_pt.get("text_center", [1292, 1908]))
GS_PT_FONT = _gs_pt.get("font", "belerenbsc")
GS_PT_FONT_SIZE: int = _gs_pt.get("font_size", 60)
GS_PT_CLEARANCE_Y: int = _gs_pt.get("clearance_y", 1858)

_pw = _L.get("planeswalker", {})
PW_ART = (0, 0, CANVAS_W, CANVAS_H)
PW_TITLE = tuple(_pw.get("title", {}).get("box", [128, 90, 1100, 100]))
PW_TITLE_FONT = _pw.get("title", {}).get("font", "belerenb")
PW_TITLE_FONT_SIZE: int = _pw.get("title", {}).get("font_size", 56)
PW_MANA_Y: int = _pw.get("mana", {}).get("y", 90)
PW_MANA_RIGHT: int = _pw.get("mana", {}).get("right", 1394)
PW_TYPE = tuple(_pw.get("type", {}).get("box", [128, 1189, 1243, 114]))
PW_TYPE_FONT = _pw.get("type", {}).get("font", "belerenb")
PW_TYPE_FONT_SIZE: int = _pw.get("type", {}).get("font_size", 42)
_pw_ab = _pw.get("ability", {})
PW_ABILITY_START_Y: int = _pw_ab.get("start_y", 900)
PW_ABILITY_X: int = _pw_ab.get("x", 180)
PW_ABILITY_W: int = _pw_ab.get("width", 1150)
PW_ABILITY_H: int = _pw_ab.get("height", 200)
PW_ABILITY_GAP: int = _pw_ab.get("gap", 20)
PW_ABILITY_FONT = _pw_ab.get("font", "mplantin")
PW_ABILITY_MAX: int = _pw_ab.get("max_size", 44)
PW_ABILITY_MIN: int = _pw_ab.get("min_size", 20)
PW_ABILITY_ICON_SIZE = tuple(_pw_ab.get("icon_size", [90, 65]))
PW_ABILITY_OVERLAY_COLOR = tuple(_pw_ab.get("overlay_color", [20, 20, 20, 160]))
PW_ABILITY_COST_FONT = _pw_ab.get("cost_font", "belerenb")
PW_ABILITY_COST_FONT_SIZE: int = _pw_ab.get("cost_font_size", 38)
PW_ABILITY_COST_OFFSET_X: int = _pw_ab.get("cost_text_offset_x", 70)
PW_ABILITY_COST_OFFSET_Y_PLUS: int = _pw_ab.get("cost_text_offset_y_plus", 25)
PW_ABILITY_COST_OFFSET_Y_MINUS: int = _pw_ab.get("cost_text_offset_y_minus", 85)
PW_ABILITY_COST_OFFSET_Y_NEUTRAL: int = _pw_ab.get("cost_text_offset_y_neutral", 55)
_pw_lo = _pw.get("loyalty", {})
PW_LOYALTY_X: int = _pw_lo.get("x", 1310)
PW_LOYALTY_Y: int = _pw_lo.get("y", 1950)
PW_LOYALTY_FONT = _pw_lo.get("font", "belerenb")
PW_LOYALTY_FONT_SIZE: int = _pw_lo.get("font_size", 56)

_SET_INFO = SimpleNamespace(
    year=2026,
    version="MCG",
    artist="Telegram Bot",
    generator="MTG Card Generator",
)

_RARITY_SVG_MAP = {
    "common": "unf-c",
    "uncommon": "unf-u",
    "rare": "unf-r",
    "mythic": "unf-m",
}

_MANA_RE = re.compile(r"\{([^}]+)\}")
_DARK_TEXT_FRAMES = {"W", "A", "M"}


@lru_cache(maxsize=256)
def _load_mana_svg(symbol: str, size: int = MANA_SYMBOL_SIZE) -> Image.Image | None:
    path = Assets.mana_symbol(symbol)
    if not path.exists():
        log.warning("Mana symbol not found: %s", path)
        return None
    return svg_to_pil(path, size)


@lru_cache(maxsize=32)
def _load_font(name: str, size: int) -> ImageFont.FreeTypeFont:
    path = Assets.font(name)
    if path.exists():
        return ImageFont.truetype(str(path), size)
    log.warning("Font not found: %s, using default", path)
    return ImageFont.load_default()


def _load_frame(path: Path) -> Image.Image | None:
    if path.exists():
        return Image.open(path).convert("RGBA")
    log.warning("Frame not found: %s", path)
    return None


@lru_cache(maxsize=16)
def _load_rarity_svg(rarity: str, size: int = RARITY_SIZE) -> Image.Image | None:
    stem = _RARITY_SVG_MAP.get(rarity.lower(), "unf-c")
    path = Assets.SET_SYMBOLS / f"{stem}.svg"
    if not path.exists():
        log.warning("Rarity symbol not found: %s", path)
        return None
    return svg_to_pil(path, size)


def _frame_color_code(colors: str) -> str:
    if not colors or colors == "C":
        return "A"
    if len(colors) > 1:
        return "M"
    return colors[0].upper()


def _text_color(color_code: str) -> str:
    return "black" if color_code in _DARK_TEXT_FRAMES else "white"


def _footer_color(color_code: str) -> str:
    return FOOTER_COLOR_LIGHT if color_code in _DARK_TEXT_FRAMES else FOOTER_COLOR_DARK


def _render_rarity_symbol(canvas: Image.Image, rarity: str, type_box: tuple) -> Image.Image:
    icon = _load_rarity_svg(rarity.lower(), RARITY_SIZE)
    if icon:
        x = RARITY_RIGHT - RARITY_SIZE
        y = type_box[1] + RARITY_Y_OFFSET
        canvas.paste(icon, (x, y), icon)
    return canvas


def _render_footer(draw: ImageDraw.ImageDraw, cc: str) -> None:
    fc = _footer_color(cc)
    font = _load_font(FOOTER_FONT, FOOTER_FONT_SIZE)
    nib_font = _load_font("mana", FOOTER_NIB_SIZE)
    si = _SET_INFO
    nib = "\ue924"

    left1 = str(si.year)
    right1 = f"\u2122 & \u00a9 {si.year}"
    right2 = si.generator

    draw.text((FOOTER_LEFT_X, FOOTER_LEFT_LINE1_Y), left1, fill=fc, font=font)

    version_part = f"{si.version} "
    draw.text((FOOTER_LEFT_X, FOOTER_LEFT_LINE2_Y), version_part, fill=fc, font=font)
    vbox = draw.textbbox((FOOTER_LEFT_X, 0), version_part, font=font)
    nib_x = vbox[2]
    nib_y_offset = (FOOTER_FONT_SIZE - FOOTER_NIB_SIZE) // 2
    draw.text((nib_x, FOOTER_LEFT_LINE2_Y + nib_y_offset), nib, fill=fc, font=nib_font)
    nib_box = draw.textbbox((nib_x, 0), nib, font=nib_font)
    artist_x = nib_box[2] + 2
    draw.text((artist_x, FOOTER_LEFT_LINE2_Y), f" {si.artist}", fill=fc, font=font)

    draw.text((FOOTER_LEFT_X, FOOTER_LEFT_LINE3_Y), "NOT FOR SALE", fill=fc, font=font)

    bbox1 = draw.textbbox((0, 0), right1, font=font)
    draw.text((FOOTER_RIGHT_X - (bbox1[2] - bbox1[0]), FOOTER_RIGHT_LINE1_Y), right1, fill=fc, font=font)

    bbox2 = draw.textbbox((0, 0), right2, font=font)
    draw.text((FOOTER_RIGHT_X - (bbox2[2] - bbox2[0]), FOOTER_RIGHT_LINE2_Y), right2, fill=fc, font=font)


def _parse_mana_cost(mana_cost: str) -> list[str]:
    """Parse mana cost into symbol keys for SVG lookup (e.g. '2', 'r', 'wu')."""
    if not mana_cost:
        return []
    text = mana_cost.strip()
    symbols: list[str] = []

    for part in re.split(r"(\{[^}]+\})", text):
        if not part:
            continue
        if part.startswith("{") and part.endswith("}"):
            inner = part[1:-1].strip()
            if inner:
                symbols.append(inner.lower().replace(" ", ""))
            continue
        i = 0
        while i < len(part):
            ch = part[i]
            if ch.isdigit():
                num = ch
                while i + 1 < len(part) and part[i + 1].isdigit():
                    i += 1
                    num += part[i]
                symbols.append(num)
            elif ch.upper() in "WUBRGCXST":
                symbols.append(ch.lower())
            i += 1
    return symbols


def _render_mana_cost(canvas: Image.Image, mana_cost: str, right_x: int, y: int) -> None:
    symbols = _parse_mana_cost(mana_cost)
    if not symbols:
        return
    total_w = len(symbols) * (MANA_SYMBOL_SIZE + MANA_SYMBOL_SPACING) - MANA_SYMBOL_SPACING
    x = right_x - total_w
    for sym in symbols:
        icon = _load_mana_svg(sym, MANA_SYMBOL_SIZE)
        if icon:
            canvas.paste(icon, (x, y), icon)
        x += MANA_SYMBOL_SIZE + MANA_SYMBOL_SPACING


def _draw_centered_text(draw, text, box_x, box_y, box_w, box_h, font, fill="white"):
    bbox = draw.textbbox((0, 0), text, font=font)
    th = bbox[3] - bbox[1]
    y_off = max(0, (box_h - th) // 3)
    draw.text((box_x, box_y + y_off), text, fill=fill, font=font)


def _wrap_text(text, font, max_width, draw):
    wrapped_lines = []
    for paragraph in text.split("\n"):
        words = paragraph.split(" ")
        cur_line = ""
        for word in words:
            test = f"{cur_line} {word}".strip() if cur_line else word
            tw = draw.textbbox((0, 0), test, font=font)[2]
            if tw <= max_width:
                cur_line = test
            else:
                if cur_line:
                    wrapped_lines.append(cur_line)
                cur_line = word
        wrapped_lines.append(cur_line or "")
    return "\n".join(wrapped_lines)


def _fit_text_size(draw, text, font_name, max_w, max_h, max_size=76, min_size=18):
    para_gaps = max(0, text.count("\n")) * PARAGRAPH_GAP
    for size in range(max_size, min_size - 1, -2):
        font = _load_font(font_name, size)
        wrapped = _wrap_text(text, font, max_w, draw)
        bbox = draw.multiline_textbbox((0, 0), wrapped, font=font)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if w <= max_w and (h + para_gaps) <= max_h:
            return font, wrapped
    font = _load_font(font_name, min_size)
    wrapped = _wrap_text(text, font, max_w, draw)
    return font, wrapped


def _measure_text_block_height(draw, text, w, font_name="mplantin", max_size=76, min_size=18):
    if not text:
        return 0
    para_gaps = max(0, text.count("\n")) * PARAGRAPH_GAP
    has_symbols = "{" in text and "}" in text
    plain = _MANA_RE.sub("@", text) if has_symbols else text
    font, wrapped = _fit_text_size(draw, plain, font_name, w, 9999, max_size, min_size)
    if has_symbols:
        icon_size = max(20, int(font.size * 0.9))
        line_h = icon_size + 6
        return (wrapped.count("\n") + 1) * line_h + para_gaps
    bbox = draw.multiline_textbbox((0, 0), wrapped, font=font)
    return (bbox[3] - bbox[1]) + para_gaps


def _render_rich_text(draw, canvas, text, x, y, w, h, font_name, color, max_size, min_size):
    plain = _MANA_RE.sub("@", text)
    font, _ = _fit_text_size(draw, plain, font_name, w, h, max_size, min_size)
    icon_size = max(20, int(font.size * 0.9))
    line_h = icon_size + 6
    paragraphs = text.split("\n")
    cur_y = y

    for pi, paragraph in enumerate(paragraphs):
        if pi > 0:
            cur_y += PARAGRAPH_GAP
        if cur_y + line_h > y + h:
            break
        tokens = re.split(r"(\{[^}]+\})", paragraph)
        cur_x = x

        for token in tokens:
            m_sym = _MANA_RE.match(token)
            if m_sym:
                if cur_x + icon_size > x + w and cur_x > x:
                    cur_y += line_h
                    cur_x = x
                    if cur_y + line_h > y + h:
                        break
                icon = _load_mana_svg(m_sym.group(1).lower(), icon_size)
                if icon:
                    canvas.paste(icon, (cur_x, cur_y), icon)
                    draw = ImageDraw.Draw(canvas)
                cur_x += icon_size + 2
            elif token:
                words = token.split(" ")
                for wi, word in enumerate(words):
                    segment = (" " + word) if (wi > 0 and cur_x > x) else word
                    seg_w = draw.textbbox((0, 0), segment, font=font)[2]
                    if cur_x + seg_w > x + w and cur_x > x:
                        cur_y += line_h
                        cur_x = x
                        if cur_y + line_h > y + h:
                            break
                        segment = segment.lstrip()
                    if not segment:
                        continue
                    draw.text((cur_x, cur_y), segment, fill=color, font=font)
                    cur_x = draw.textbbox((cur_x, cur_y), segment, font=font)[2]
        cur_y += line_h
    return cur_y - y


def _render_text_block(draw, canvas, text, x, y, w, h, font_name="mplantin", color="white", max_size=76, min_size=18, align="left"):
    if not text:
        return 0
    has_symbols = "{" in text and "}" in text
    if has_symbols:
        return _render_rich_text(draw, canvas, text, x, y, w, h, font_name, color, max_size, min_size)
    font, _ = _fit_text_size(draw, text, font_name, w, h, max_size, min_size)
    paragraphs = text.split("\n")
    cur_y = y
    for i, para in enumerate(paragraphs):
        if i > 0:
            cur_y += PARAGRAPH_GAP
        if cur_y >= y + h:
            break
        wrapped_para = _wrap_text(para, font, w, draw)
        draw.multiline_text((x, cur_y), wrapped_para, fill=color, font=font, align=align)
        bbox = draw.multiline_textbbox((x, cur_y), wrapped_para, font=font)
        cur_y = bbox[3]
    return cur_y - y


def render_standard_card(details: CardDetails, art_path: Path) -> Image.Image:
    canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 255))
    cc = _frame_color_code(details.colors)

    if art_path.exists() and art_path.stat().st_size > 0:
        art = Image.open(art_path).convert("RGBA")
        art = art.resize((GS_ART[2], GS_ART[3]), Image.LANCZOS)
        canvas.paste(art, (GS_ART[0], GS_ART[1]))

    frame = _load_frame(Assets.showcase_frame(cc))
    if frame:
        canvas = Image.alpha_composite(canvas, frame)

    is_creature = (
        "creature" in details.type_line.lower()
        or "существ" in details.type_line.lower()
    ) and details.power is not None and details.toughness is not None

    if is_creature:
        pt_box = _load_frame(Assets.pt_box(cc))
        if pt_box:
            canvas.paste(pt_box, GS_PT_POS, pt_box)

    draw = ImageDraw.Draw(canvas)

    if details.mana_cost:
        _render_mana_cost(canvas, details.mana_cost, GS_MANA_RIGHT, GS_MANA_Y)
        draw = ImageDraw.Draw(canvas)

    tc = _text_color(cc)
    title_font = _load_font(GS_TITLE_FONT, GS_TITLE_FONT_SIZE)
    _draw_centered_text(draw, details.name, GS_TITLE[0], GS_TITLE[1], GS_TITLE[2], GS_TITLE[3], title_font, fill=tc)

    type_font = _load_font(GS_TYPE_FONT, GS_TYPE_FONT_SIZE)
    _draw_centered_text(draw, details.type_line, GS_TYPE[0], GS_TYPE[1], GS_TYPE[2], GS_TYPE[3], type_font, fill=tc)
    canvas = _render_rarity_symbol(canvas, details.rarity, GS_TYPE)
    draw = ImageDraw.Draw(canvas)

    rules_y = GS_RULES[1]
    box_h = (GS_PT_CLEARANCE_Y - rules_y) if is_creature else GS_RULES[3]
    box_w = GS_RULES[2]

    rules_h = _measure_text_block_height(draw, details.rules_text, box_w, GS_RULES_FONT, GS_RULES_MAX, GS_RULES_MIN)
    flavor_h = _measure_text_block_height(draw, details.flavor_text, box_w, GS_FLAVOR_FONT, GS_FLAVOR_MAX, GS_FLAVOR_MIN) if details.flavor_text else 0
    total_h = rules_h + (GS_FLAVOR_GAP + flavor_h if flavor_h else 0)
    y_start = rules_y + max(0, (box_h - total_h) // 2)

    _render_text_block(draw, canvas, details.rules_text, GS_RULES[0], y_start, box_w, box_h, font_name=GS_RULES_FONT, color=tc, max_size=GS_RULES_MAX, min_size=GS_RULES_MIN)

    if details.flavor_text and flavor_h > 0:
        flavor_y = y_start + rules_h + GS_FLAVOR_GAP
        remaining_h = (rules_y + box_h) - flavor_y
        if remaining_h > 30:
            _render_text_block(draw, canvas, details.flavor_text, GS_RULES[0], flavor_y, box_w, remaining_h, font_name=GS_FLAVOR_FONT, color=tc, max_size=GS_FLAVOR_MAX, min_size=GS_FLAVOR_MIN)

    if is_creature:
        pt_str = f"{details.power}/{details.toughness}"
        pt_font = _load_font(GS_PT_FONT, GS_PT_FONT_SIZE)
        bbox = draw.textbbox((0, 0), pt_str, font=pt_font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text((GS_PT_TEXT[0] - tw // 2, GS_PT_TEXT[1] - th // 2), pt_str, fill=tc, font=pt_font)

    _render_footer(draw, cc)
    return canvas


def render_planeswalker(details: CardDetails, art_path: Path) -> Image.Image:
    canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 255))
    cc = _frame_color_code(details.colors)

    if art_path.exists() and art_path.stat().st_size > 0:
        art = Image.open(art_path).convert("RGBA")
        art = art.resize((PW_ART[2], PW_ART[3]), Image.LANCZOS)
        canvas.paste(art, (PW_ART[0], PW_ART[1]))

    frame = _load_frame(Assets.pw_frame(cc))
    if frame:
        canvas = Image.alpha_composite(canvas, frame)

    draw = ImageDraw.Draw(canvas)

    if details.mana_cost:
        _render_mana_cost(canvas, details.mana_cost, PW_MANA_RIGHT, PW_MANA_Y)
        draw = ImageDraw.Draw(canvas)

    tc = _text_color(cc)
    title_font = _load_font(PW_TITLE_FONT, PW_TITLE_FONT_SIZE)
    _draw_centered_text(draw, details.name, PW_TITLE[0], PW_TITLE[1], PW_TITLE[2], PW_TITLE[3], title_font, fill=tc)

    type_font = _load_font(PW_TYPE_FONT, PW_TYPE_FONT_SIZE)
    _draw_centered_text(draw, details.type_line, PW_TYPE[0], PW_TYPE[1], PW_TYPE[2], PW_TYPE[3], type_font, fill=tc)
    canvas = _render_rarity_symbol(canvas, details.rarity, PW_TYPE)
    draw = ImageDraw.Draw(canvas)

    _COST_RE = re.compile(r'^([+\-\u2212]?\d+|0)\s*:\s*')

    if details.abilities:
        ability_icons = {
            "+": Assets.PW_IMAGES / "planeswalkerPlus.png",
            "\u2212": Assets.PW_IMAGES / "planeswalkerMinus.png",
            "-": Assets.PW_IMAGES / "planeswalkerMinus.png",
            "0": Assets.PW_IMAGES / "planeswalkerNeutral.png",
        }
        icon_x = PW_ABILITY_X - PW_ABILITY_ICON_SIZE[0] - 10

        mask_img = Image.open(Assets.PW_IMAGES / "planeswalkerMaskText.png").convert("RGBA")
        solid = Image.new("RGBA", (CANVAS_W, CANVAS_H), tuple(PW_ABILITY_OVERLAY_COLOR))
        shaped = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
        shaped.paste(solid, mask=mask_img.split()[3])
        canvas = Image.alpha_composite(canvas, shaped)
        draw = ImageDraw.Draw(canvas)

        cost_font = _load_font(PW_ABILITY_COST_FONT, PW_ABILITY_COST_FONT_SIZE)
        cur_y = PW_ABILITY_START_Y

        for ab_text in details.abilities:
            icon_key = "0"
            if ab_text.startswith("+"):
                icon_key = "+"
            elif ab_text.startswith("-") or ab_text.startswith("\u2212"):
                icon_key = "-"

            m = _COST_RE.match(ab_text)
            if m:
                cost_label = m.group(1)
                ability_body = ab_text[m.end():]
            else:
                cost_label = ""
                ability_body = ab_text

            icon_path = ability_icons.get(icon_key)
            if icon_path and icon_path.exists():
                icon = Image.open(icon_path).convert("RGBA")
                icon = icon.resize(PW_ABILITY_ICON_SIZE, Image.LANCZOS)
                canvas.paste(icon, (icon_x, cur_y), icon)
                draw = ImageDraw.Draw(canvas)

                if cost_label:
                    cost_label_draw = cost_label.replace("\u2212", "-")
                    cb = draw.textbbox((0, 0), cost_label_draw, font=cost_font)
                    cw, ch = cb[2] - cb[0], cb[3] - cb[1]
                    _offset_y_map = {
                        "+": PW_ABILITY_COST_OFFSET_Y_PLUS,
                        "-": PW_ABILITY_COST_OFFSET_Y_MINUS,
                        "0": PW_ABILITY_COST_OFFSET_Y_NEUTRAL,
                    }
                    cost_offset_y = _offset_y_map.get(icon_key, PW_ABILITY_COST_OFFSET_Y_NEUTRAL)
                    cx = icon_x + PW_ABILITY_COST_OFFSET_X - cw // 2
                    cy = cur_y + cost_offset_y - ch // 2
                    draw.text((cx, cy), cost_label_draw, fill="white", font=cost_font)

            _render_text_block(draw, canvas, ability_body, PW_ABILITY_X, cur_y, PW_ABILITY_W, PW_ABILITY_H, font_name=PW_ABILITY_FONT, color=tc, max_size=PW_ABILITY_MAX, min_size=PW_ABILITY_MIN)
            cur_y += PW_ABILITY_H + PW_ABILITY_GAP

    if details.starting_loyalty is not None:
        loyalty_font = _load_font(PW_LOYALTY_FONT, PW_LOYALTY_FONT_SIZE)
        lt = str(details.starting_loyalty)
        bbox = draw.textbbox((0, 0), lt, font=loyalty_font)
        tw = bbox[2] - bbox[0]
        draw.text((PW_LOYALTY_X - tw // 2, PW_LOYALTY_Y), lt, fill="white", font=loyalty_font)

    _render_footer(draw, cc)
    return canvas


def render_card(details: CardDetails, art_path: Path) -> Image.Image:
    if details.card_type == "planeswalker" or "planeswalker" in details.type_line.lower():
        return render_planeswalker(details, art_path)
    return render_standard_card(details, art_path)


def render_card_to_bytes(details: CardDetails, art_path: Path) -> bytes:
    img = render_card(details, art_path)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()

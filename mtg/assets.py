"""Asset path helpers for MTG card rendering."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASSETS_ROOT = PROJECT_ROOT / "mtg_assets"


class Assets:
    FRAMES_SHOWCASE = ASSETS_ROOT / "img" / "frames" / "m15" / "genericShowcase"
    FRAMES_PW_BORDERLESS = ASSETS_ROOT / "img" / "frames" / "planeswalker" / "borderless"
    PT_BOXES = ASSETS_ROOT / "img" / "frames" / "m15" / "nickname"
    MANA_SYMBOLS = ASSETS_ROOT / "img" / "manaSymbols"
    FONTS = ASSETS_ROOT / "data" / "fonts"
    FONTS_ALT = ASSETS_ROOT / "fonts"
    PW_IMAGES = ASSETS_ROOT / "data" / "images" / "cardImages" / "planeswalker"
    SET_SYMBOLS = ASSETS_ROOT / "img" / "setSymbols" / "official"

    COLOR_MAP = {
        "W": "W", "U": "U", "B": "B", "R": "R", "G": "G",
        "M": "M", "A": "A", "L": "L", "C": "A",
    }
    COLOR_MAP_LOWER = {
        "W": "w", "U": "u", "B": "b", "R": "r", "G": "g",
        "M": "m", "A": "a", "L": "l", "C": "a",
    }

    @classmethod
    def showcase_frame(cls, color_code: str) -> Path:
        c = cls.COLOR_MAP.get(color_code, "M")
        return cls.FRAMES_SHOWCASE / f"m15GenericShowcaseFrame{c}.png"

    @classmethod
    def pw_frame(cls, color_code: str) -> Path:
        c = cls.COLOR_MAP_LOWER.get(color_code, "m")
        return cls.FRAMES_PW_BORDERLESS / f"{c}.png"

    @classmethod
    def pt_box(cls, color_code: str) -> Path:
        c = cls.COLOR_MAP.get(color_code, "M")
        return cls.PT_BOXES / f"m15NicknamePT{c}.png"

    @classmethod
    def mana_symbol(cls, symbol: str) -> Path:
        return cls.MANA_SYMBOLS / f"{symbol.lower()}.svg"

    @classmethod
    def font(cls, name: str) -> Path:
        mapping = {
            "belerenb": "beleren-b.ttf",
            "belerenbsc": "beleren-bsc.ttf",
            "mplantin": "mplantin.ttf",
            "mplantini": "mplantin-i.ttf",
            "gothammedium": "gotham-medium.ttf",
            "matrix": "matrix.ttf",
            "matrixb": "matrix-b.ttf",
            "mana": "mana.ttf",
        }
        primary = cls.FONTS / mapping.get(name, f"{name}.ttf")
        if primary.exists():
            return primary
        alt = cls.FONTS_ALT / mapping.get(name, f"{name}.ttf")
        if alt.exists():
            return alt
        return primary

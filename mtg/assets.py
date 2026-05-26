"""Asset path helpers for MTG card rendering."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASSETS_ROOT = PROJECT_ROOT / "mtg_assets"

_DEFAULT_FONT_FILES = {
    "minioncyrillic": "Minion Cyrillic Bold.ttf",
    "timesnewroman": "timesnewromanpsmt.ttf",
    "timesnewromanitalic": "timesnewromanps_italicmt.ttf",
    "gothammedium": "gotham-medium.ttf",
    "mana": "mana.ttf",
    "belerenbsc": "beleren-bsc.ttf",
    "belerenb": "beleren-b.ttf",
    "mplantin": "mplantin.ttf",
    "mplantini": "mplantin-i.ttf",
}


class Assets:
    FRAMES_SHOWCASE = ASSETS_ROOT / "img" / "frames" / "m15" / "genericShowcase"
    FRAMES_PW_BORDERLESS = ASSETS_ROOT / "img" / "frames" / "planeswalker" / "borderless"
    PT_BOXES = ASSETS_ROOT / "img" / "frames" / "m15" / "nickname"
    MANA_SYMBOLS = ASSETS_ROOT / "img" / "manaSymbols"
    FONTS = ASSETS_ROOT / "data" / "fonts"
    FONTS_ALT = ASSETS_ROOT / "fonts"
    PW_IMAGES = ASSETS_ROOT / "data" / "images" / "cardImages" / "planeswalker"
    SET_SYMBOLS = ASSETS_ROOT / "img" / "setSymbols" / "official"

    _font_files: dict[str, str] = dict(_DEFAULT_FONT_FILES)

    COLOR_MAP = {
        "W": "W", "U": "U", "B": "B", "R": "R", "G": "G",
        "M": "M", "A": "A", "L": "L", "C": "A",
    }
    COLOR_MAP_LOWER = {
        "W": "w", "U": "u", "B": "b", "R": "r", "G": "g",
        "M": "m", "A": "a", "L": "l", "C": "a",
    }

    @classmethod
    def configure_fonts(cls, font_files: dict[str, str] | None) -> None:
        """Merge font filename overrides from layout.yaml fonts.files."""
        cls._font_files = dict(_DEFAULT_FONT_FILES)
        if font_files:
            cls._font_files.update(font_files)

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
        sym = symbol.lower().strip()
        candidates = [sym]
        if "/" in sym:
            candidates.append(sym.replace("/", ""))
        for alt in candidates:
            path = cls.MANA_SYMBOLS / f"{alt}.svg"
            if path.exists():
                return path
        return cls.MANA_SYMBOLS / f"{sym}.svg"

    @classmethod
    def font(cls, name: str) -> Path:
        filename = cls._font_files.get(name, f"{name}.ttf")
        primary = cls.FONTS / filename
        if primary.exists():
            return primary
        alt = cls.FONTS_ALT / filename
        if alt.exists():
            return alt
        return primary

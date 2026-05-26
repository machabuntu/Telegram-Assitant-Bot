"""Parse AI card text responses into CardDetails."""

from __future__ import annotations

import re

from mtg.models import CardDetails

_KNOWN_FIELDS = frozenset({
    "CARD_TYPE", "NAME", "COLORS", "RARITY", "MANA_COST", "TYPE_LINE",
    "POWER", "TOUGHNESS", "RULES_TEXT", "FLAVOR_TEXT",
    "STARTING_LOYALTY", "ABILITY_1", "ABILITY_2", "ABILITY_3", "ABILITY_4",
})

_FIELD_LINE_RE = re.compile(
    r"(?:" + "|".join(sorted(_KNOWN_FIELDS)) + r")[ \t]*:",
    re.IGNORECASE,
)

_PT_RE = re.compile(r"^[\d*+-]+$")
_MANA_INNER_RE = re.compile(
    r"^[0-9]{1,2}$|^[WUBRGCXST]$|^[WUBRGC]{1,2}/[WUBRGC]{1,2}$",
    re.IGNORECASE,
)


def _parse_field(text: str, field: str) -> str:
    start_pat = re.compile(rf"^{re.escape(field)}[ \t]*:[ \t]*", re.MULTILINE | re.IGNORECASE)
    start_m = start_pat.search(text)
    if not start_m:
        return ""
    rest = text[start_m.end():]
    lines = rest.split("\n")
    value_lines = [lines[0]]
    for line in lines[1:]:
        if _FIELD_LINE_RE.match(line.lstrip()):
            break
        value_lines.append(line)
    return "\n".join(value_lines).strip()


def _valid_pt(value: str | None) -> str | None:
    if value and _PT_RE.match(value):
        return value
    return None


def _normalise_rules(raw: str, card_name: str = "") -> str:
    text = raw.replace("\\n", "\n").strip()
    if card_name:
        text = text.replace("~", card_name)
    return _normalise_mana_braces(text)


def _normalise_mana_braces(text: str) -> str:
    """Ensure inline mana uses {symbol} tokens the renderer understands."""
    if not text:
        return text

    # (T) or {tap} → {T}
    text = re.sub(r"\(\s*[Tt]\s*\)", "{T}", text)
    text = re.sub(r"\{[Tt]ap\}", "{T}", text, flags=re.IGNORECASE)
    text = re.sub(r"\{повернуть\}", "{T}", text, flags=re.IGNORECASE)

    # Collapse spaced braces: { R } → {R}
    text = re.sub(r"\{\s*([^}]+?)\s*\}", lambda m: "{" + m.group(1).strip().upper() + "}", text)

    return text


def _normalise_mana_cost(raw: str) -> str:
    """Normalize MANA_COST to a string the renderer can parse."""
    if not raw:
        return ""
    cost = raw.strip().strip('"').strip("'")
    cost = cost.replace("\u2212", "-")

    # Already all-brace format like {2}{R}{R}
    if cost.startswith("{") and "}" in cost:
        tokens = re.findall(r"\{([^}]+)\}", cost)
        parts: list[str] = []
        for token in tokens:
            inner = token.strip().upper().replace(" ", "")
            if not inner:
                continue
            if inner.isdigit() or _MANA_INNER_RE.match(inner):
                parts.append("{" + inner + "}")
                continue
            # {2RR} or {1WU} — expand compact form inside braces
            expanded: list[str] = []
            i = 0
            while i < len(inner):
                ch = inner[i]
                if ch.isdigit():
                    num = ch
                    while i + 1 < len(inner) and inner[i + 1].isdigit():
                        i += 1
                        num += inner[i]
                    expanded.append("{" + num + "}")
                elif ch in "WUBRGCXST":
                    expanded.append("{" + ch + "}")
                i += 1
            parts.extend(expanded)
        return "".join(parts)

    # Plain compact form: 2RR, 1WU, X, etc.
    parts = []
    i = 0
    while i < len(cost):
        ch = cost[i]
        if ch.isdigit():
            num = ch
            while i + 1 < len(cost) and cost[i + 1].isdigit():
                i += 1
                num += cost[i]
            parts.append("{" + num + "}")
        elif ch.upper() in "WUBRGCXST":
            parts.append("{" + ch.upper() + "}")
        i += 1
    return "".join(parts)


def _parse_card_type(text: str) -> str:
    raw = _parse_field(text, "CARD_TYPE").lower().strip()
    if "planeswalker" in raw or raw == "pw":
        return "planeswalker"
    return "standard"


def _parse_response_standard(text: str) -> CardDetails:
    name = _parse_field(text, "NAME") or "Безымянная карта"
    mana_cost = _normalise_mana_cost(_parse_field(text, "MANA_COST"))
    type_line = _parse_field(text, "TYPE_LINE")
    colors = _parse_field(text, "COLORS").upper().replace(" ", "")
    rarity = _parse_field(text, "RARITY").lower() or "common"
    power = _valid_pt(_parse_field(text, "POWER") or None)
    toughness = _valid_pt(_parse_field(text, "TOUGHNESS") or None)
    rules_text = _normalise_rules(_parse_field(text, "RULES_TEXT"), name)
    flavor_text = _parse_field(text, "FLAVOR_TEXT").strip('"').strip("'")

    return CardDetails(
        name=name,
        card_type="standard",
        mana_cost=mana_cost,
        type_line=type_line or "Карта",
        colors=colors,
        rarity=rarity,
        power=power,
        toughness=toughness,
        rules_text=rules_text,
        flavor_text=flavor_text,
    )


def _parse_response_planeswalker(text: str) -> CardDetails:
    name = _parse_field(text, "NAME") or "Безымянный planeswalker"
    mana_cost = _normalise_mana_cost(_parse_field(text, "MANA_COST"))
    type_line = _parse_field(text, "TYPE_LINE")
    colors = _parse_field(text, "COLORS").upper().replace(" ", "")
    rarity = _parse_field(text, "RARITY").lower() or "rare"
    loyalty_str = _parse_field(text, "STARTING_LOYALTY")

    abilities: list[str] = []
    for key in ("ABILITY_1", "ABILITY_2", "ABILITY_3", "ABILITY_4"):
        val = _parse_field(text, key)
        if val:
            abilities.append(_normalise_mana_braces(val))

    try:
        loyalty = int(loyalty_str)
    except (ValueError, TypeError):
        loyalty = 3

    return CardDetails(
        name=name,
        card_type="planeswalker",
        mana_cost=mana_cost,
        type_line=type_line or "Planeswalker",
        colors=colors,
        rarity=rarity,
        starting_loyalty=loyalty,
        abilities=abilities,
    )


def _normalize_yo_to_e(text: str) -> str:
    """Replace yo (Ё/ё) with ye (Е/е) in generated card text."""
    return text.translate(str.maketrans("ёЁ", "еЕ"))


def parse_card_response(text: str) -> CardDetails:
    """Parse AI response text into CardDetails."""
    text = _normalize_yo_to_e(text)
    card_type = _parse_card_type(text)
    if card_type == "planeswalker":
        return _parse_response_planeswalker(text)
    return _parse_response_standard(text)

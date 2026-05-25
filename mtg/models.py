"""Data models for MTG card generation."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class CardDetails(BaseModel):
    name: str
    card_type: str = "standard"
    mana_cost: str = ""
    type_line: str = ""
    colors: str = ""
    rarity: str = ""
    cmc: int = 0

    power: Optional[str] = None
    toughness: Optional[str] = None

    starting_loyalty: Optional[int] = None
    abilities: Optional[list[str]] = None

    rules_text: str = ""
    flavor_text: str = ""

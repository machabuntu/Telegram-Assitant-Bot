from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BingoGrid:
    topic: str
    cells: dict[tuple[int, int], str]

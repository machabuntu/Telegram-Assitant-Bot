"""Parse and validate LLM bingo JSON responses."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from bingo.models import BingoGrid

log = logging.getLogger(__name__)

MAX_CELL_TEXT_LEN = 80
GRID_SIZE = 5
EXPECTED_CELLS = GRID_SIZE * GRID_SIZE


def strip_json_markdown(raw: str) -> str:
    if not isinstance(raw, str):
        return ""
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    return s


def _truncate_cell_text(text: str) -> str:
    text = text.strip()
    if len(text) <= MAX_CELL_TEXT_LEN:
        return text
    log.warning("Bingo: пункт обрезан с %d до %d символов: %r", len(text), MAX_CELL_TEXT_LEN, text[:40])
    return text[: MAX_CELL_TEXT_LEN - 1].rstrip() + "…"


def validate_bingo_data(data: Any, fallback_topic: str) -> BingoGrid | None:
    if not isinstance(data, dict):
        return None

    cells_raw = data.get("cells")
    if not isinstance(cells_raw, list) or len(cells_raw) != EXPECTED_CELLS:
        log.warning(
            "Bingo JSON: ожидалось %d ячеек, получено %s",
            EXPECTED_CELLS,
            len(cells_raw) if isinstance(cells_raw, list) else "не список",
        )
        return None

    topic = data.get("topic")
    if not isinstance(topic, str) or not topic.strip():
        topic = fallback_topic
    else:
        topic = topic.strip()

    parsed_cells: dict[tuple[int, int], str] = {}
    for i, cell in enumerate(cells_raw):
        if not isinstance(cell, dict):
            log.warning("Bingo JSON: ячейка %d не является объектом", i)
            return None

        row = cell.get("row")
        col = cell.get("col")
        text = cell.get("text")

        if not isinstance(row, int) or not isinstance(col, int):
            log.warning("Bingo JSON: некорректные координаты у ячейки %d", i)
            return None
        if not (1 <= row <= GRID_SIZE and 1 <= col <= GRID_SIZE):
            log.warning("Bingo JSON: координаты вне диапазона у ячейки %d: row=%s col=%s", i, row, col)
            return None
        if not isinstance(text, str) or not text.strip():
            log.warning("Bingo JSON: пустой text у ячейки %d", i)
            return None

        key = (row, col)
        if key in parsed_cells:
            log.warning("Bingo JSON: дубликат координат (%d, %d)", row, col)
            return None

        parsed_cells[key] = _truncate_cell_text(text.replace("\\n", " ").replace("\n", " "))

    expected = {(r, c) for r in range(1, GRID_SIZE + 1) for c in range(1, GRID_SIZE + 1)}
    if set(parsed_cells.keys()) != expected:
        missing = expected - set(parsed_cells.keys())
        extra = set(parsed_cells.keys()) - expected
        log.warning("Bingo JSON: неполная сетка missing=%s extra=%s", missing, extra)
        return None

    return BingoGrid(topic=topic, cells=parsed_cells)


def parse_bingo_response(raw: str, fallback_topic: str) -> BingoGrid | None:
    cleaned = strip_json_markdown(raw)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        log.warning("Bingo: невалидный JSON: %s", e)
        return None
    return validate_bingo_data(data, fallback_topic)

"""
Standalone DB migration script.

Usage:
    python migrate.py                          # uses user_statistics.db in current dir
    python migrate.py path/to/user_statistics.db

What it does:
  1. Creates any missing tables (idempotent — safe to run multiple times).
  2. Adds any missing columns to existing tables via ALTER TABLE.

Run this on the production stand before (or instead of) restarting the bot
whenever the schema has changed.
"""

import sys
import sqlite3
import logging
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("migrate")

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else "user_statistics.db"

# ---------------------------------------------------------------------------
# Full table definitions (must stay in sync with init_database() in the bot)
# ---------------------------------------------------------------------------
TABLES = [
    """
    CREATE TABLE IF NOT EXISTS user_statistics (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        total_spent REAL DEFAULT 0,
        total_requests INTEGER DEFAULT 0,
        last_request_date TEXT,
        created_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS request_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        generation_id TEXT,
        command TEXT,
        cost REAL,
        model TEXT,
        tokens_prompt INTEGER,
        tokens_completion INTEGER,
        request_date TEXT,
        FOREIGN KEY (user_id) REFERENCES user_statistics (user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tournament_registrations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tournament_id TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        username TEXT,
        fighter_name TEXT NOT NULL,
        registered_at TEXT NOT NULL,
        disqualified INTEGER DEFAULT 0,
        UNIQUE(tournament_id, user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tournament_bans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fighter_name TEXT NOT NULL,
        banned_at TEXT NOT NULL,
        tournament_id TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tournaments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tournament_id TEXT UNIQUE NOT NULL,
        status TEXT DEFAULT 'registration',
        bracket_json TEXT,
        created_at TEXT,
        completed_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tournament_scores (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        total_points INTEGER DEFAULT 0,
        first_places INTEGER DEFAULT 0,
        second_places INTEGER DEFAULT 0,
        semifinal_places INTEGER DEFAULT 0
    )
    """,
]

# ---------------------------------------------------------------------------
# Column-level migrations: columns that may be missing from older DB files
# ---------------------------------------------------------------------------
COLUMN_MIGRATIONS = {
    "user_statistics": [
        ("total_spent",       "REAL    DEFAULT 0"),
        ("total_requests",    "INTEGER DEFAULT 0"),
        ("last_request_date", "TEXT"),
        ("created_at",        "TEXT"),
    ],
    "request_history": [
        ("generation_id",     "TEXT"),
        ("tokens_prompt",     "INTEGER"),
        ("tokens_completion", "INTEGER"),
    ],
    "tournament_scores": [
        ("first_places",     "INTEGER DEFAULT 0"),
        ("second_places",    "INTEGER DEFAULT 0"),
        ("semifinal_places", "INTEGER DEFAULT 0"),
    ],
    "tournament_registrations": [
        ("disqualified", "INTEGER DEFAULT 0"),
    ],
    "tournaments": [
        ("bracket_json", "TEXT"),
        ("completed_at", "TEXT"),
    ],
}


def migrate(db_path: str) -> None:
    log.info(f"Подключение к базе данных: {db_path}")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 1. Create missing tables
    log.info("Шаг 1: создание отсутствующих таблиц...")
    for ddl in TABLES:
        table_name = [w for w in ddl.split() if w][4]  # 5th word after CREATE TABLE IF NOT EXISTS
        cursor.execute(ddl)
        log.info(f"  OK  {table_name}")

    conn.commit()

    # 2. Add missing columns to existing tables
    log.info("Шаг 2: проверка и добавление недостающих колонок...")
    for table, columns in COLUMN_MIGRATIONS.items():
        cursor.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in cursor.fetchall()}
        if not existing:
            log.warning(f"  Таблица {table} не найдена — пропускаем колонки")
            continue
        for col_name, col_def in columns:
            if col_name not in existing:
                try:
                    cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}")
                    log.info(f"  ADDED  {table}.{col_name}")
                except sqlite3.OperationalError as e:
                    log.error(f"  ERROR  {table}.{col_name}: {e}")
            else:
                log.info(f"  EXISTS {table}.{col_name}")

    conn.commit()
    conn.close()
    log.info("Миграция завершена успешно.")


if __name__ == "__main__":
    migrate(DB_PATH)

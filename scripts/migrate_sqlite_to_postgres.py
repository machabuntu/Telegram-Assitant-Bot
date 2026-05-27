#!/usr/bin/env python3
"""
One-time migration: copy data from local SQLite user_statistics.db to PostgreSQL.

Usage:
    python scripts/migrate_sqlite_to_postgres.py
    python scripts/migrate_sqlite_to_postgres.py path/to/user_statistics.db

Requires database section in config.json.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("sqlite_to_pg")

SQLITE_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "user_statistics.db"
BATCH_SIZE = 5000

# Import order respects foreign keys.
# (table_name, columns, conflict_target for ON CONFLICT)
TABLES: list[tuple[str, list[str], str]] = [
    (
        "user_statistics",
        [
            "user_id",
            "username",
            "first_name",
            "last_name",
            "total_spent",
            "total_requests",
            "last_request_date",
            "created_at",
        ],
        "(user_id)",
    ),
    (
        "request_history",
        [
            "id",
            "user_id",
            "generation_id",
            "command",
            "cost",
            "model",
            "tokens_prompt",
            "tokens_completion",
            "request_date",
        ],
        "(id)",
    ),
    (
        "tournament_registrations",
        [
            "id",
            "tournament_id",
            "user_id",
            "username",
            "fighter_name",
            "registered_at",
            "disqualified",
            "validated",
        ],
        "(id)",
    ),
    (
        "tournament_bans",
        ["id", "fighter_name", "banned_at", "tournament_id"],
        "(id)",
    ),
    (
        "tournaments",
        [
            "id",
            "tournament_id",
            "status",
            "bracket_json",
            "created_at",
            "completed_at",
        ],
        "(id)",
    ),
    (
        "tournament_scores",
        [
            "user_id",
            "username",
            "total_points",
            "first_places",
            "second_places",
            "semifinal_places",
        ],
        "(user_id)",
    ),
    (
        "steam_games",
        ["appid", "name", "last_modified", "updated_at"],
        "(appid)",
    ),
    (
        "steam_user_wishlist",
        ["steamid", "appid", "updated_at"],
        "(steamid, appid)",
    ),
    (
        "steam_user_owned",
        ["steamid", "appid", "updated_at"],
        "(steamid, appid)",
    ),
    (
        "quiz_scores",
        [
            "user_id",
            "username",
            "first_name",
            "last_name",
            "total_points",
            "correct_answers",
            "quizzes_played",
            "last_played_at",
        ],
        "(user_id)",
    ),
]


def sqlite_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    )
    return cur.fetchone() is not None


def sqlite_count(conn: sqlite3.Connection, table: str) -> int:
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    return int(cur.fetchone()[0])


def pg_count(conn, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        return int(cur.fetchone()[0])


def import_table(
    sqlite_conn: sqlite3.Connection,
    pg_conn,
    table: str,
    columns: list[str],
    conflict_target: str,
) -> int:
    if not sqlite_table_exists(sqlite_conn, table):
        log.warning("SQLite: таблица %s не найдена — пропуск", table)
        return 0

    src_count = sqlite_count(sqlite_conn, table)
    log.info("Импорт %s: %d строк из SQLite...", table, src_count)
    if src_count == 0:
        return 0

    col_list = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    sql = (
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT {conflict_target} DO NOTHING"
    )

    cur = sqlite_conn.cursor()
    cur.execute(f"SELECT {col_list} FROM {table}")

    inserted = 0
    batch: list[tuple] = []
    with pg_conn.cursor() as pg_cur:
        while True:
            rows = cur.fetchmany(BATCH_SIZE)
            if not rows:
                break
            for row in rows:
                batch.append(tuple(row))
                if len(batch) >= BATCH_SIZE:
                    pg_cur.executemany(sql, batch)
                    inserted += len(batch)
                    batch.clear()
                    log.info("  %s: записано %d / %d", table, inserted, src_count)
        if batch:
            pg_cur.executemany(sql, batch)
            inserted += len(batch)

    pg_conn.commit()
    dst_count = pg_count(pg_conn, table)
    log.info("  %s: SQLite=%d, PostgreSQL=%d", table, src_count, dst_count)
    return inserted


def main() -> None:
    if not SQLITE_PATH.is_file():
        log.error("SQLite file not found: %s", SQLITE_PATH)
        sys.exit(1)

    cfg = database.load_config_from_file(ROOT / "config.json")
    if "database" not in cfg:
        log.error("config.json must contain a 'database' section")
        sys.exit(1)

    database.configure(cfg["database"])
    log.info("SQLite source: %s", SQLITE_PATH)
    log.info(
        "PostgreSQL target: %s:%s/%s",
        cfg["database"].get("host"),
        cfg["database"].get("port", 5432),
        cfg["database"].get("dbname"),
    )

    sqlite_conn = sqlite3.connect(str(SQLITE_PATH))

    pg_conn = psycopg.connect(database.build_dsn(), autocommit=False)
    try:
        log.info("Создание схемы PostgreSQL...")
        with database.get_connection() as conn:
            database.init_schema(conn)
            database.migrate_columns(conn)

        for table, columns, conflict_target in TABLES:
            import_table(sqlite_conn, pg_conn, table, columns, conflict_target)

        log.info("Сброс serial-последовательностей...")
        database.reset_serial_sequences(pg_conn)
        pg_conn.commit()

        log.info("Миграция данных завершена успешно.")
    finally:
        sqlite_conn.close()
        pg_conn.close()


if __name__ == "__main__":
    main()

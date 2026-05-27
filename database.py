"""PostgreSQL connection, schema initialization, and SQLite-compatible API."""

from __future__ import annotations

import json
import logging
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator, Iterable, Optional, Sequence

import psycopg
from psycopg import Connection
from psycopg import errors as pg_errors

logger = logging.getLogger(__name__)

_db_config: Optional[dict[str, Any]] = None

TABLE_DDL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS user_statistics (
        user_id BIGINT PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        total_spent DOUBLE PRECISION DEFAULT 0,
        total_requests INTEGER DEFAULT 0,
        last_request_date TEXT,
        created_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS request_history (
        id SERIAL PRIMARY KEY,
        user_id BIGINT,
        generation_id TEXT,
        command TEXT,
        cost DOUBLE PRECISION,
        model TEXT,
        tokens_prompt INTEGER,
        tokens_completion INTEGER,
        request_date TEXT,
        FOREIGN KEY (user_id) REFERENCES user_statistics (user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tournament_registrations (
        id SERIAL PRIMARY KEY,
        tournament_id TEXT NOT NULL,
        user_id BIGINT NOT NULL,
        username TEXT,
        fighter_name TEXT NOT NULL,
        registered_at TEXT NOT NULL,
        disqualified INTEGER DEFAULT 0,
        validated INTEGER DEFAULT 0,
        UNIQUE(tournament_id, user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tournament_bans (
        id SERIAL PRIMARY KEY,
        fighter_name TEXT NOT NULL,
        banned_at TEXT NOT NULL,
        tournament_id TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tournaments (
        id SERIAL PRIMARY KEY,
        tournament_id TEXT UNIQUE NOT NULL,
        status TEXT DEFAULT 'registration',
        bracket_json TEXT,
        created_at TEXT,
        completed_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tournament_scores (
        user_id BIGINT PRIMARY KEY,
        username TEXT,
        total_points INTEGER DEFAULT 0,
        first_places INTEGER DEFAULT 0,
        second_places INTEGER DEFAULT 0,
        semifinal_places INTEGER DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS steam_games (
        appid INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        last_modified INTEGER,
        updated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS steam_user_wishlist (
        steamid TEXT NOT NULL,
        appid INTEGER NOT NULL,
        updated_at TEXT,
        PRIMARY KEY (steamid, appid)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS steam_user_owned (
        steamid TEXT NOT NULL,
        appid INTEGER NOT NULL,
        updated_at TEXT,
        PRIMARY KEY (steamid, appid)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quiz_scores (
        user_id BIGINT PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        total_points INTEGER DEFAULT 0,
        correct_answers INTEGER DEFAULT 0,
        quizzes_played INTEGER DEFAULT 0,
        last_played_at TEXT
    )
    """,
]

COLUMN_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "user_statistics": [
        ("total_spent", "DOUBLE PRECISION DEFAULT 0"),
        ("total_requests", "INTEGER DEFAULT 0"),
        ("last_request_date", "TEXT"),
        ("created_at", "TEXT"),
    ],
    "request_history": [
        ("generation_id", "TEXT"),
        ("tokens_prompt", "INTEGER"),
        ("tokens_completion", "INTEGER"),
    ],
    "tournament_scores": [
        ("first_places", "INTEGER DEFAULT 0"),
        ("second_places", "INTEGER DEFAULT 0"),
        ("semifinal_places", "INTEGER DEFAULT 0"),
    ],
    "tournament_registrations": [
        ("disqualified", "INTEGER DEFAULT 0"),
        ("validated", "INTEGER DEFAULT 0"),
    ],
    "tournaments": [
        ("bracket_json", "TEXT"),
        ("completed_at", "TEXT"),
    ],
}

SERIAL_COLUMNS: dict[str, str] = {
    "request_history": "id",
    "tournament_registrations": "id",
    "tournament_bans": "id",
    "tournaments": "id",
}


def load_config_from_file(config_path: str | Path = "config.json") -> dict[str, Any]:
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def configure(db_config: dict[str, Any]) -> None:
    global _db_config
    _db_config = db_config


def get_db_config() -> dict[str, Any]:
    if _db_config is None:
        raise RuntimeError("database.configure() must be called before using the database")
    return _db_config


def build_dsn(db_config: Optional[dict[str, Any]] = None) -> str:
    cfg = db_config or get_db_config()
    host = cfg.get("host", "localhost")
    port = int(cfg.get("port", 5432))
    dbname = cfg["dbname"]
    user = cfg["user"]
    password = cfg["password"]
    sslmode = cfg.get("sslmode", "prefer")
    return (
        f"host={host} port={port} dbname={dbname} user={user} "
        f"password={password} sslmode={sslmode}"
    )


def _translate_sql(sql: str) -> str:
    """Convert SQLite-style placeholders and minor dialect differences."""
    sql = sql.replace("?", "%s")
    sql = re.sub(r"ON CONFLICT\s*\(\s*(\w+)\s*\)", r"ON CONFLICT (\1)", sql, flags=re.IGNORECASE)
    return sql


class CompatCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> "CompatCursor":
        if sql.strip().upper() == "BEGIN":
            return self
        self._cursor.execute(_translate_sql(sql), params or None)
        return self

    def executemany(self, sql: str, params_seq: Iterable[Sequence[Any]]) -> None:
        self._cursor.executemany(_translate_sql(sql), params_seq)

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()


class CompatConnection:
    """Drop-in replacement for sqlite3.Connection used by the bot."""

    def __init__(self, conn: Connection):
        self._conn = conn

    def cursor(self) -> CompatCursor:
        return CompatCursor(self._conn.cursor())

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def connect() -> CompatConnection:
    return CompatConnection(psycopg.connect(build_dsn(), autocommit=False))


@contextmanager
def get_connection(*, autocommit: bool = False) -> Generator[Connection, None, None]:
    conn = psycopg.connect(build_dsn(), autocommit=autocommit)
    try:
        yield conn
        if not autocommit:
            conn.commit()
    except Exception:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()


def init_schema(conn: Connection) -> None:
    with conn.cursor() as cursor:
        for ddl in TABLE_DDL:
            cursor.execute(ddl)


def migrate_columns(conn: Connection) -> None:
    with conn.cursor() as cursor:
        for table, columns in COLUMN_MIGRATIONS.items():
            cursor.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
                """,
                (table,),
            )
            existing = {row[0] for row in cursor.fetchall()}
            if not existing:
                logger.warning("Migration: table %s not found — skipping columns", table)
                continue
            for col_name, col_def in columns:
                if col_name in existing:
                    continue
                cursor.execute(
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col_name} {col_def}"
                )
                logger.info("Migration: added column %s.%s", table, col_name)


def init_database() -> None:
    with get_connection() as conn:
        init_schema(conn)
        migrate_columns(conn)


def reset_serial_sequences(conn: Connection) -> None:
    with conn.cursor() as cursor:
        for table, column in SERIAL_COLUMNS.items():
            cursor.execute(
                f"""
                SELECT setval(
                    pg_get_serial_sequence(%s, %s),
                    COALESCE((SELECT MAX({column}) FROM {table}), 1)
                )
                """,
                (table, column),
            )


def table_names() -> list[str]:
    names: list[str] = []
    for ddl in TABLE_DDL:
        parts = ddl.split()
        for i, part in enumerate(parts):
            if part.upper() == "EXISTS" and i + 1 < len(parts):
                names.append(parts[i + 1])
                break
    return names

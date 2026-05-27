"""
Standalone PostgreSQL schema migration script.

Usage:
    python migrate.py

What it does:
  1. Creates any missing tables (idempotent — safe to run multiple times).
  2. Adds any missing columns to existing tables.

Run this on the production stand before (or instead of) restarting the bot
whenever the schema has changed.
"""

import logging

import database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("migrate")


def migrate() -> None:
    cfg = database.load_config_from_file()
    database.configure(cfg["database"])
    log.info(
        "Подключение к PostgreSQL: %s:%s/%s",
        cfg["database"].get("host"),
        cfg["database"].get("port", 5432),
        cfg["database"].get("dbname"),
    )

    log.info("Шаг 1: создание отсутствующих таблиц...")
    with database.get_connection() as conn:
        database.init_schema(conn)
        for name in database.table_names():
            log.info("  OK  %s", name)

    log.info("Шаг 2: проверка и добавление недостающих колонок...")
    with database.get_connection() as conn:
        database.migrate_columns(conn)

    log.info("Миграция завершена успешно.")


if __name__ == "__main__":
    migrate()

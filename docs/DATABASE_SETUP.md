# Настройка PostgreSQL для бота

Бот использует **PostgreSQL 16** вместо локального файла `user_statistics.db`.

---

## Параметры подключения

В `config.json` секция `database`:

```json
"database": {
    "host": "185.79.139.82",
    "port": 5432,
    "dbname": "user_statistics",
    "user": "telegram_assistant",
    "password": "YOUR_PASSWORD",
    "sslmode": "prefer"
}
```

Пароль храните только в `config.json` (файл в `.gitignore`).

---

## Шаг 1. Подготовка сервера PostgreSQL

На сервере `185.79.139.82` должны быть:

- PostgreSQL 16, слушающий порт **5432**
- База данных **user_statistics**
- Пользователь **telegram_assistant** с правами на схему `public`

Пример (на сервере PostgreSQL, от суперпользователя):

```sql
CREATE USER telegram_assistant WITH PASSWORD 'your_secure_password';
CREATE DATABASE user_statistics OWNER telegram_assistant;
GRANT ALL PRIVILEGES ON DATABASE user_statistics TO telegram_assistant;
```

Подключитесь к базе и выдайте права на схему:

```sql
\c user_statistics
GRANT ALL ON SCHEMA public TO telegram_assistant;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO telegram_assistant;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO telegram_assistant;
```

---

## Шаг 2. Firewall

VPS, где работает бот, должен иметь доступ к `185.79.139.82:5432`.

Проверка с машины бота:

```bash
nc -zv 185.79.139.82 5432
# или
psql "host=185.79.139.82 port=5432 dbname=user_statistics user=telegram_assistant sslmode=prefer"
```

Если подключение не проходит — откройте порт в firewall PostgreSQL-сервера для IP вашего VPS (`pg_hba.conf` + `listen_addresses`).

---

## Шаг 3. Зависимости

```bash
cd /media/games/OpenAI-Whisper
pip install -r requirements.txt
```

---

## Шаг 4. Миграция данных из SQLite (один раз)

Если в проекте есть актуальный `user_statistics.db`:

```bash
python scripts/migrate_sqlite_to_postgres.py
# или с явным путём:
python scripts/migrate_sqlite_to_postgres.py /path/to/user_statistics.db
```

Скрипт:

1. Создаёт таблицы в PostgreSQL
2. Копирует все данные из SQLite (включая ~167k игр Steam)
3. Сбрасывает serial-последовательности

Повторный запуск безопасен (`ON CONFLICT DO NOTHING`).

---

## Шаг 5. Проверка схемы

```bash
python migrate.py
```

Создаёт недостающие таблицы и колонки (идемпотентно).

---

## Шаг 6. Запуск бота

```bash
python ai_assistant_bot.py
```

Проверьте:

- `/stats` — статистика расходов
- `/leaderboard`, `/banlist` — турнирные данные
- `/randomsteamgame` — список игр Steam
- `/quizleaderboards` — очки викторины

---

## Таблицы в базе

| Таблица | Назначение |
|---------|------------|
| `user_statistics` | Суммарные расходы пользователей |
| `request_history` | История запросов к API |
| `tournaments` | Турниры |
| `tournament_registrations` | Регистрации на турнир |
| `tournament_bans` | Банлист бойцов |
| `tournament_scores` | Очки лидерборда |
| `steam_games` | Каталог игр Steam |
| `steam_user_wishlist` | Вишлист пользователя |
| `steam_user_owned` | Купленные игры |
| `quiz_scores` | Очки викторины |

---

## Backup

После успешной миграции **не удаляйте сразу** `user_statistics.db` — оставьте как резервную копию на несколько дней.

---

## Troubleshooting

| Проблема | Решение |
|----------|---------|
| `connection refused` | Firewall, `listen_addresses`, порт 5432 |
| `password authentication failed` | Проверьте пароль в `config.json` |
| `permission denied for schema public` | Выдайте GRANT пользователю (шаг 1) |
| `relation does not exist` | Запустите `python migrate.py` |
| Пустая статистика после миграции | Запустите `scripts/migrate_sqlite_to_postgres.py` |

---

## Безопасность

- Не храните пароль БД в git
- Рекомендуется сменить пароль, если он передавался в открытом виде
- Ограничьте доступ к PostgreSQL по IP (только VPS с ботом)

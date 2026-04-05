import os
import asyncio
import subprocess
import tempfile
import logging
import re
from pathlib import Path
from typing import Optional
import json
import base64
import mimetypes
from urllib.parse import urlparse
from io import BytesIO
import sqlite3
from datetime import datetime

import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import requests
import time

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


class _RedactDataImageLogFilter(logging.Filter):
    """Убирает из любых лог-сообщений data:image/...;base64,... чтобы файлы не раздувались."""

    _pat = re.compile(r"data:image/[^;]+;base64,[A-Za-z0-9+/=\r\n]+", re.DOTALL)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if "data:image" not in msg or ";base64," not in msg:
            return True
        new_msg = self._pat.sub(lambda m: f"<omitted base64 {len(m.group(0))} chars>", msg)
        if new_msg != msg:
            record.msg = new_msg
            record.args = ()
        return True


# Фильтр на корневой логгер — срабатывает для всех дочерних логгеров
logging.getLogger().addFilter(_RedactDataImageLogFilter())

# Отключаем логи HTTP запросов
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)

class TelegramWhisperBot:
    def __init__(self, config_file: str = "config.json"):
        self.config = self.load_config(config_file)
        self.application = None
        self.temp_dir = Path(tempfile.gettempdir()) / "whisper_bot"
        self.temp_dir.mkdir(exist_ok=True)
        # Хранилище последних изображений по chat_id
        self.last_images = {}
        # Хранилище последних сгенерированных изображений по chat_id
        self.last_generated_images = {}
        # Хранилище множественных изображений из последнего сообщения по chat_id
        self.last_multiple_images = {}
        # Папка для сохранения сгенерированных изображений
        self.generated_images_dir = Path("generated_images")
        self.generated_images_dir.mkdir(exist_ok=True)
        # База данных для статистики пользователей
        self.db_path = "user_statistics.db"
        self.init_database()
        # Список доступных моделей OpenRouter
        self.available_models = []
        # Файл для хранения выбранных моделей по chat_id
        self.selected_models_file = "selected_models.json"
        # Хранилище выбранных моделей {chat_id: model_id}
        self.selected_models = self.load_selected_models()
        # Спам-защита для /reg: {user_id: {"count": int, "banned_until": datetime | None}}
        self._reg_spam: dict = {}

    def load_config(self, config_file: str) -> dict:
        """Загружает конфигурацию из JSON файла"""
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            logger.error(f"Файл конфигурации {config_file} не найден!")
            raise
        except json.JSONDecodeError as e:
            logger.error(f"Ошибка в формате JSON файла {config_file}: {e}")
            raise
    
    def get_api_config(self, api_name: str):
        """Получает конфигурацию API для указанной команды
        
        Args:
            api_name: Имя API конфигурации (например, 'summary_api', 'describe_api', 'imagegen_api')
        
        Returns:
            dict: Конфигурация выбранного провайдера
        """
        try:
            api_config = self.config[api_name]
            provider = api_config.get("provider", "openrouter")
            
            # Получаем все доступные провайдеры (исключая ключ "provider")
            available_providers = [k for k in api_config.keys() if k != "provider"]
            
            if provider not in available_providers:
                raise ValueError(f"Провайдер '{provider}' не найден в конфигурации {api_name}. Доступные провайдеры: {available_providers}")
            
            logger.info(f"Использую провайдер '{provider}' для {api_name}")
            return api_config[provider]
        except KeyError as e:
            logger.error(f"Конфигурация {api_name} не найдена: {e}")
            raise
        except Exception as e:
            logger.error(f"Ошибка при получении конфигурации {api_name}: {e}")
            raise
    
    def reload_config(self):
        """Перезагружает конфигурацию из файла config.json"""
        try:
            old_config = self.config.copy()
            self.config = self.load_config("config.json")
            logger.info("Конфигурация успешно перезагружена из config.json")
            
            # Логируем изменения в провайдерах
            for api_name in self.config:
                if api_name.endswith('_api') and isinstance(self.config[api_name], dict):
                    old_provider = old_config.get(api_name, {}).get("provider", "неизвестно")
                    new_provider = self.config[api_name].get("provider", "неизвестно")
                    if old_provider != new_provider:
                        logger.info(f"Провайдер {api_name}: {old_provider} -> {new_provider}")
            
            return True
        except Exception as e:
            logger.error(f"Ошибка при перезагрузке конфигурации: {e}")
            return False
    
    def init_database(self):
        """Инициализирует базу данных для статистики пользователей"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Создаем таблицу для хранения статистики пользователей
            cursor.execute('''
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
            ''')
            
            # Создаем таблицу для детальной истории запросов
            cursor.execute('''
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
            ''')
            
            # Турнирные таблицы
            cursor.execute('''
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
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS tournament_bans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fighter_name TEXT NOT NULL,
                    banned_at TEXT NOT NULL,
                    tournament_id TEXT
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS tournaments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tournament_id TEXT UNIQUE NOT NULL,
                    status TEXT DEFAULT 'registration',
                    bracket_json TEXT,
                    created_at TEXT,
                    completed_at TEXT
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS tournament_scores (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    total_points INTEGER DEFAULT 0,
                    first_places INTEGER DEFAULT 0,
                    second_places INTEGER DEFAULT 0,
                    semifinal_places INTEGER DEFAULT 0
                )
            ''')

            self._migrate_columns(conn)
            conn.commit()
            conn.close()
            logger.info("База данных статистики успешно инициализирована")
        except Exception as e:
            logger.error(f"Ошибка при инициализации базы данных: {e}")

    def _migrate_columns(self, conn: sqlite3.Connection):
        """Добавляет недостающие колонки в существующие таблицы (безопасный ALTER TABLE).
        Вызывается автоматически при каждом старте бота — не трогает уже существующие колонки."""
        expected = {
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
                ("bracket_json",  "TEXT"),
                ("completed_at",  "TEXT"),
            ],
        }
        cursor = conn.cursor()
        for table, columns in expected.items():
            try:
                cursor.execute(f"PRAGMA table_info({table})")
                existing = {row[1] for row in cursor.fetchall()}
                if not existing:
                    # Таблица ещё не создана — пропускаем, CREATE TABLE IF NOT EXISTS создаст её выше
                    continue
                for col_name, col_def in columns:
                    if col_name not in existing:
                        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}")
                        logger.info(f"Миграция БД: добавлена колонка {table}.{col_name}")
            except Exception as e:
                logger.error(f"Миграция БД: ошибка при обработке таблицы {table}: {e}")
        conn.commit()

    def load_selected_models(self) -> dict:
        """Загружает выбранные модели из файла"""
        try:
            if os.path.exists(self.selected_models_file):
                with open(self.selected_models_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # Конвертируем ключи обратно в int (JSON хранит их как строки)
                    return {int(k): v for k, v in data.items()}
            return {}
        except Exception as e:
            logger.error(f"Ошибка при загрузке выбранных моделей: {e}")
            return {}
    
    def save_selected_models(self):
        """Сохраняет выбранные модели в файл"""
        try:
            with open(self.selected_models_file, 'w', encoding='utf-8') as f:
                json.dump(self.selected_models, f, ensure_ascii=False, indent=2)
            logger.info("Выбранные модели сохранены")
        except Exception as e:
            logger.error(f"Ошибка при сохранении выбранных моделей: {e}")
    
    def fetch_openrouter_models(self):
        """Загружает и фильтрует список моделей с OpenRouter API"""
        try:
            # Получаем API ключ из конфига
            api_key = self.config.get("ask_api", {}).get("openrouter", {}).get("key")
            if not api_key:
                logger.error("Не найден API ключ OpenRouter для загрузки моделей")
                return []
            
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }
            
            logger.info("Загружаю список моделей с OpenRouter API...")
            response = requests.get(
                "https://openrouter.ai/api/v1/models",
                headers=headers,
                timeout=30
            )
            
            if response.status_code != 200:
                logger.error(f"Ошибка при загрузке моделей: {response.status_code} - {response.text}")
                return []
            
            data = response.json()
            models_data = data.get("data", [])
            
            # Фильтруем модели
            # created не старше 6 месяцев (в секундах: 6 * 30 * 24 * 60 * 60)
            six_months_ago = time.time() - (6 * 30 * 24 * 60 * 60)
            
            filtered_models = []
            for model in models_data:
                model_id = model.get("id", "")
                created = model.get("created", 0)
                architecture = model.get("architecture", {})
                input_modalities = architecture.get("input_modalities", [])
                output_modalities = architecture.get("output_modalities", [])
                
                # Проверяем условия:
                # 1. created не старше 6 месяцев
                # 2. input_modalities содержит "text"
                # 3. output_modalities ТОЛЬКО ["text"] (строго)
                if (created >= six_months_ago and 
                    "text" in input_modalities and 
                    output_modalities == ["text"]):
                    filtered_models.append({
                        "id": model_id,
                        "name": model.get("name", model_id),
                        "created": created
                    })
            
            # Сортируем по дате создания (новые первые)
            filtered_models.sort(key=lambda x: x["created"], reverse=True)
            
            self.available_models = filtered_models
            logger.info(f"Загружено {len(filtered_models)} моделей (из {len(models_data)} всего)")
            return filtered_models
            
        except Exception as e:
            logger.error(f"Ошибка при загрузке моделей с OpenRouter: {e}", exc_info=True)
            return []
    
    async def update_models_periodically(self, context: ContextTypes.DEFAULT_TYPE):
        """Периодически обновляет список моделей (вызывается job_queue)"""
        logger.info("Периодическое обновление списка моделей...")
        self.fetch_openrouter_models()
    
    async def track_generation_cost(self, generation_id: str, user_id: int, username: str, 
                                     first_name: str, last_name: str, command: str):
        """Отслеживает стоимость генерации и обновляет статистику пользователя
        
        Args:
            generation_id: ID генерации из OpenRouter
            user_id: Telegram user ID
            username: Telegram username
            first_name: Имя пользователя
            last_name: Фамилия пользователя
            command: Команда, которая была использована
        """
        try:
            # Получаем конфигурацию OpenRouter (используем describe_api для совместимости)
            api_config = self.get_api_config("describe_api")
            
            # Запрашиваем метаданные генерации
            url = f"https://openrouter.ai/api/v1/generation?id={generation_id}"
            headers = {
                "Authorization": f"Bearer {api_config['key']}"
            }
            
            response = requests.get(url, headers=headers, timeout=30)
            
            if response.status_code == 200:
                result = response.json()
                logger.info(f"Метаданные генерации {generation_id}: {self._format_api_result_for_log(result)}")
                
                data = result.get("data", {})
                total_cost = data.get("total_cost", 0)
                model = data.get("model", "unknown")
                tokens_prompt = data.get("tokens_prompt", 0)
                tokens_completion = data.get("tokens_completion", 0)
                
                # Сохраняем статистику
                self.save_user_statistics(
                    user_id=user_id,
                    username=username,
                    first_name=first_name,
                    last_name=last_name,
                    cost=total_cost,
                    generation_id=generation_id,
                    command=command,
                    model=model,
                    tokens_prompt=tokens_prompt,
                    tokens_completion=tokens_completion
                )
                
                logger.info(f"Стоимость запроса пользователя {username} (ID: {user_id}): ${total_cost:.6f}")
            else:
                logger.warning(f"Не удалось получить метаданные генерации {generation_id}: {response.status_code}")
                
        except Exception as e:
            logger.error(f"Ошибка при отслеживании стоимости генерации: {e}")
    
    def save_user_statistics(self, user_id: int, username: str, first_name: str, last_name: str,
                            cost: float, generation_id: str, command: str, model: str,
                            tokens_prompt: int, tokens_completion: int):
        """Сохраняет статистику использования пользователем
        
        Args:
            user_id: Telegram user ID
            username: Telegram username
            first_name: Имя пользователя
            last_name: Фамилия пользователя
            cost: Стоимость запроса
            generation_id: ID генерации
            command: Использованная команда
            model: Модель, которая использовалась
            tokens_prompt: Количество токенов в промпте
            tokens_completion: Количество токенов в ответе
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            current_time = datetime.now().isoformat()
            
            # Проверяем, существует ли пользователь
            cursor.execute('SELECT user_id FROM user_statistics WHERE user_id = ?', (user_id,))
            exists = cursor.fetchone()
            
            if exists:
                # Обновляем существующую запись
                cursor.execute('''
                    UPDATE user_statistics 
                    SET username = ?,
                        first_name = ?,
                        last_name = ?,
                        total_spent = total_spent + ?,
                        total_requests = total_requests + 1,
                        last_request_date = ?
                    WHERE user_id = ?
                ''', (username, first_name, last_name, cost, current_time, user_id))
            else:
                # Создаем новую запись
                cursor.execute('''
                    INSERT INTO user_statistics 
                    (user_id, username, first_name, last_name, total_spent, total_requests, 
                     last_request_date, created_at)
                    VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                ''', (user_id, username, first_name, last_name, cost, current_time, current_time))
            
            # Добавляем запись в историю
            cursor.execute('''
                INSERT INTO request_history 
                (user_id, generation_id, command, cost, model, tokens_prompt, 
                 tokens_completion, request_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (user_id, generation_id, command, cost, model, tokens_prompt, 
                  tokens_completion, current_time))
            
            conn.commit()
            conn.close()
            logger.info(f"Статистика пользователя {user_id} обновлена: +${cost:.6f}")
            
        except Exception as e:
            logger.error(f"Ошибка при сохранении статистики: {e}")
    
    def get_generation_id_from_response(self, response_data: dict) -> Optional[str]:
        """Извлекает generation ID из ответа OpenRouter API
        
        Args:
            response_data: JSON ответ от OpenRouter API
            
        Returns:
            str: ID генерации или None
        """
        try:
            # ID генерации обычно находится в поле 'id'
            generation_id = response_data.get('id')
            if generation_id:
                logger.info(f"Извлечен generation_id: {generation_id}")
                return generation_id
            else:
                logger.warning("generation_id не найден в ответе API")
                return None
        except Exception as e:
            logger.error(f"Ошибка при извлечении generation_id: {e}")
            return None

    def _sanitize_for_log(self, obj, depth: int = 0):
        """Убирает из структуры data:image...;base64,... и гигантские base64-строки для безопасного логирования."""
        if depth > 24:
            return "<max depth>"
        if obj is None or isinstance(obj, (bool, int, float)):
            return obj
        if isinstance(obj, str):
            if "data:image/" in obj and ";base64," in obj:
                try:
                    head, b64rest = obj.split(";base64,", 1)
                    return f"{head};base64,<omitted {len(b64rest)} chars>"
                except ValueError:
                    pass
            if len(obj) > 4000:
                st = obj.strip()
                if len(st) > 4000 and re.match(r"^[A-Za-z0-9+/=\s]+$", st[:2000]):
                    return f"<long base64-like string omitted: {len(obj)} chars>"
            return obj
        if isinstance(obj, dict):
            return {str(k): self._sanitize_for_log(v, depth + 1) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._sanitize_for_log(v, depth + 1) for v in obj]
        if isinstance(obj, tuple):
            return tuple(self._sanitize_for_log(v, depth + 1) for v in obj)
        return f"<{type(obj).__name__}>"

    def _format_api_result_for_log(self, obj) -> str:
        """JSON-снимок ответа API без раздувания лога base64."""
        try:
            s = json.dumps(self._sanitize_for_log(obj), ensure_ascii=False, default=str)
        except Exception:
            s = str(self._sanitize_for_log(obj))
        max_total = 12000
        if len(s) > max_total:
            return s[:max_total] + f"...<log truncated, was {len(s)} chars>"
        return s

    def _truncate_http_error_body(self, text: str, max_len: int = 2000) -> str:
        """Тело ошибки HTTP без data-URL и без мегабайт текста."""
        if not text:
            return text
        if "data:image/" in text and ";base64," in text:
            text = re.sub(
                r"data:image/[^;]+;base64,[A-Za-z0-9+/=\r\n]+",
                lambda m: f"<data:image base64 omitted {len(m.group(0))} chars>",
                text,
                flags=re.DOTALL,
            )
        if len(text) > max_len:
            return text[:max_len] + f"...<truncated {len(text) - max_len} chars>"
        return text

    def _single_line_log_preview(self, text: str, max_len: int = 500) -> str:
        """Превращает фрагмент ответа API в одну строку без \\n/\\r, чтобы одна запись logging не разбивала файл на «пустые» строки."""
        if not text:
            return "<пусто>"
        s = text.replace("\r\n", "\n").replace("\r", "\n")
        s = s.replace("\n", " \\n ")
        s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        if len(s) > max_len:
            return s[:max_len] + f"…<ещё {len(s) - max_len} симв.>"
        return s

    def save_generated_image(self, image_bytes: bytes, image_format: str, chat_id: int, command_type: str) -> Path:
        """Сохраняет сгенерированное изображение в папку
        
        Args:
            image_bytes: Байты изображения
            image_format: Формат изображения (png, jpeg, etc.)
            chat_id: ID чата
            command_type: Тип команды (imagegen, imagechange, changelast)
        
        Returns:
            Path: Путь к сохранённому файлу
        """
        from datetime import datetime
        
        # Создаем имя файла с временной меткой
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{command_type}_{chat_id}_{timestamp}.{image_format}"
        filepath = self.generated_images_dir / filename
        
        # Сохраняем файл
        with open(filepath, 'wb') as f:
            f.write(image_bytes)
        
        logger.info(f"Сгенерированное изображение сохранено: {filepath}")
        return filepath
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /start"""
        await update.message.reply_text(
            "🤖 Привет! Я AI-ассистент с тринадцатью функциями:\n\n"
            "📹 **Анализ YouTube видео:**\n"
            "• `/summary <URL_видео>` - создание краткого содержания видео\n\n"
            "🖼️ **Анализ изображений:**\n"
            "• `/describe` - анализ последнего изображения в чате\n"
            "• `/describe <URL_изображения>` - анализ изображения по ссылке\n\n"
            "💬 **Текстовый чат:**\n"
            "• `/ask <вопрос>` - отправка текстового запроса в модель\n"
            "• `/model` - выбор модели для команды /askmodel\n"
            "• `/askmodel <вопрос>` - запрос в выбранную модель\n\n"
            "🎨 **Генерация изображений:**\n"
            "• `/imagegen <текст>` - генерация изображения по описанию\n"
            "• `/abcgen <тема>` - генерация русской азбуки на заданную тему\n\n"
            "✨ **Изменение изображений:**\n"
            "• `/imagechange <текст>` - изменение последнего изображения из чата\n"
            "• `/changelast <текст>` - изменение последнего сгенерированного изображения\n\n"
            "🔀 **Объединение изображений:**\n"
            "• `/mergeimage <текст>` - обработка нескольких изображений из последнего сообщения\n\n"
            "💰 **Баланс и статистика:**\n"
            "• `/balance` - проверка остатка средств на OpenRouter\n"
            "• `/statistics` - статистика расходов пользователей\n\n"
            "⚙️ **Управление:**\n"
            "• `/reload` - перезагрузка конфигурации без перезапуска бота\n\n"
            "Выберите нужную команду для начала работы!"
        )
    
    async def summary_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /summary — создаёт краткое содержание YouTube-видео через Google Gemini"""
        # Проверяем, что сообщение пришло из разрешенного канала
        if not self.is_authorized_channel(update):
            await update.message.reply_text("Доступ запрещен. Бот работает только в определенных каналах.")
            return
        
        # Проверяем наличие URL в команде
        if not context.args:
            await update.message.reply_text("Пожалуйста, укажите URL видео: /summary <URL_видео>")
            return
        
        youtube_url = context.args[0]
        logger.info(f"Обработка видео через Gemini: {youtube_url}")
        
        # Отправляем сообщение о начале обработки с цитированием
        processing_msg = await update.message.reply_text(
            "🔄 Отправляю видео в Google Gemini для анализа...",
            reply_to_message_id=update.message.message_id
        )
        
        try:
            api_config = self.get_api_config("summary_api")
            provider = self.config["summary_api"].get("provider", "google")
            
            if provider == "google":
                # Новый путь: напрямую передаём YouTube URL в Gemini
                await self.update_status(processing_msg, "🤖 Gemini анализирует видео...")
                summary = await self.create_summary_with_gemini(youtube_url, api_config)
            else:
                # Старый путь через скачивание + транскрипцию + LLM (для grok/openrouter)
                await self.update_status(processing_msg, "📥 Скачиваю аудио с YouTube...")
                audio_file = await self.download_audio(youtube_url)
                if not audio_file:
                    await self.update_status(processing_msg, "❌ Ошибка при скачивании аудио. Проверьте URL видео.")
                    return
                
                await self.update_status(processing_msg, "🎤 Создаю транскрипт...")
                transcript = await self.transcribe_audio_with_progress(audio_file, processing_msg)
                if not transcript:
                    await self.update_status(processing_msg, "❌ Ошибка при создании транскрипта.")
                    return
                
                await self.update_status(processing_msg, "🧹 Очищаю транскрипт...")
                cleaned_transcript = self.clean_transcript(transcript)
                
                await self.update_status(processing_msg, "🤖 Генерирую summary...")
                summary = await self.create_summary(cleaned_transcript)
                
                # Очищаем временные файлы после старого пути
                await self.cleanup_temp_files()
            
            if not summary:
                await self.update_status(processing_msg, "❌ Ошибка при создании summary.")
                return
            
            # Отправляем результат
            await self.update_status(processing_msg, "✅ Готово!")
            
            await self.send_ai_response(
                update.message, summary,
                header="📝 <b>Краткое содержание видео:</b>",
                continuation_header="Продолжение"
            )
            
        except Exception as e:
            logger.error(f"Ошибка при обработке видео: {e}")
            await self.update_status(processing_msg, f"❌ Произошла ошибка: {str(e)}")
    
    async def update_status(self, message, status_text):
        """Обновляет статус обработки"""
        try:
            await message.edit_text(status_text)
        except Exception as e:
            logger.error(f"Ошибка при обновлении статуса: {e}")
    
    async def describe_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /describe"""
        # Проверяем, что сообщение пришло из разрешенного канала
        if not self.is_authorized_channel(update):
            await update.message.reply_text("Доступ запрещен. Бот работает только в определенных каналах.")
            return
        
        try:
            image_data = None
            image_source = ""
            
            # Проверяем, есть ли URL в команде
            if context.args:
                url = context.args[0]
                if not self.is_image_url(url):
                    await update.message.reply_text("❌ Указанная ссылка не является изображением. Пожалуйста, укажите корректную ссылку на изображение.")
                    return
                
                # Скачиваем изображение по URL
                image_data = await self.download_image(url)
                if not image_data:
                    await update.message.reply_text("❌ Не удалось скачать изображение по указанной ссылке.")
                    return
                image_source = f"изображение по ссылке: {url}"
            else:
                # Получаем последнее изображение из чата
                chat_id = update.effective_chat.id
                image_data = await self.get_last_image_from_chat(update, context, chat_id)
                if not image_data:
                    await update.message.reply_text(
                        "❌ Не найдено изображений в чате.\n\n"
                        "**Как использовать команду /describe:**\n"
                        "1. Сначала отправьте изображение в чат\n"
                        "2. Затем используйте команду `/describe`\n\n"
                        "**Или используйте ссылку:**\n"
                        "• `/describe <URL_изображения>` - для анализа изображения по ссылке"
                    )
                    return
                image_source = "последнее изображение из чата"
            
            # Создаем сообщение о начале обработки
            processing_msg = await update.message.reply_text(f"🖼️ Анализирую {image_source}...")
            
            # Отправляем изображение в AI API
            await self.update_status(processing_msg, "🤖 Анализирую изображение...")
            result = await self.describe_image_with_ai(image_data)
            
            if not result:
                await self.update_status(processing_msg, "❌ Ошибка при анализе изображения.")
                return
            
            # Обрабатываем результат (может быть tuple или str)
            description = None
            generation_id = None
            
            if isinstance(result, tuple) and len(result) == 2:
                # OpenRouter возвращает (description, generation_id)
                description, generation_id = result
            else:
                # Grok возвращает только description
                description = result
            
            # Отслеживаем стоимость для OpenRouter
            if generation_id:
                user = update.effective_user
                user_id = user.id
                username = user.username or ""
                first_name = user.first_name or ""
                last_name = user.last_name or ""
                await self.track_generation_cost(generation_id, user_id, username, first_name, last_name, "describe")
            
            # Отправляем результат
            await self.update_status(processing_msg, "✅ Готово!")
            
            await self.send_ai_response(
                update.message, description,
                header="🖼️ <b>Описание изображения:</b>",
                continuation_header="Продолжение описания"
            )
            
        except Exception as e:
            logger.error(f"Ошибка при анализе изображения: {e}")
            await update.message.reply_text(f"❌ Произошла ошибка: {str(e)}")
    
    async def ask_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /ask - отправляет текстовый запрос в модель"""
        # Проверяем, что сообщение пришло из разрешенного канала
        if not self.is_authorized_channel(update):
            await update.message.reply_text("Доступ запрещен. Бот работает только в определенных каналах.")
            return
        
        # Получаем текст сообщения
        message_text = update.message.text or ""
        
        # Извлекаем текст после команды /ask
        # Поддерживаем мультилайновый ввод
        # Обрабатываем разные варианты: /ask, /ask@botname, /ask текст
        if message_text.startswith('/ask'):
            # Находим конец команды (может быть /ask или /ask@botname)
            # Ищем пробел или перенос строки после команды
            command_end = 4  # Длина '/ask'
            # Проверяем, есть ли @botname
            if '@' in message_text[4:]:
                # Находим конец @botname (до пробела или переноса строки)
                at_pos = message_text.find('@', 4)
                space_pos = message_text.find(' ', at_pos)
                newline_pos = message_text.find('\n', at_pos)
                if space_pos != -1 or newline_pos != -1:
                    command_end = min([pos for pos in [space_pos, newline_pos] if pos != -1])
                else:
                    command_end = len(message_text)
            
            # Извлекаем промпт после команды
            prompt = message_text[command_end:].strip()
        else:
            # Если команда была вызвана через context.args (старый способ)
            if context.args:
                prompt = ' '.join(context.args)
            else:
                await update.message.reply_text(
                    "❌ Пожалуйста, укажите ваш вопрос после команды /ask\n\n"
                    "**Примеры использования:**\n"
                    "• `/ask Как правильно пить пиво?`\n"
                    "• `/ask` (на новой строке) Ваш вопрос\n"
                    "• `/ask` (на нескольких строках) Ваш\nвопрос\nна\nнескольких\nстроках"
                )
                return
        
        # Проверяем, что промпт не пустой
        if not prompt or not prompt.strip():
            await update.message.reply_text(
                "❌ Пожалуйста, укажите ваш вопрос после команды /ask\n\n"
                "**Примеры использования:**\n"
                "• `/ask Как правильно пить пиво?`\n"
                "• `/ask` (на новой строке) Ваш вопрос\n"
                "• `/ask` (на нескольких строках) Ваш\nвопрос\nна\nнескольких\nстроках"
            )
            return
        
        logger.info(f"Обработка запроса /ask: {prompt[:100]}...")
        
        # Отправляем сообщение о начале обработки
        processing_msg = await update.message.reply_text("🤖 Обрабатываю ваш запрос...")
        
        try:
            # Получаем конфигурацию API
            api_config = self.get_api_config("ask_api")
            
            # Отправляем запрос в API
            await self.update_status(processing_msg, "🤖 Отправляю запрос в модель...")
            result = await self.ask_with_openrouter(prompt, api_config)
            
            if not result:
                await self.update_status(processing_msg, "❌ Ошибка при обработке запроса.")
                return
            
            # Обрабатываем результат
            response_text, generation_id = result
            
            # Отслеживаем стоимость для OpenRouter
            if generation_id:
                user = update.effective_user
                user_id = user.id
                username = user.username or ""
                first_name = user.first_name or ""
                last_name = user.last_name or ""
                await self.track_generation_cost(generation_id, user_id, username, first_name, last_name, "ask")
            
            # Отправляем результат
            await self.update_status(processing_msg, "✅ Готово!")
            
            await self.send_ai_response(
                update.message, response_text,
                header="💬 <b>Ответ:</b>",
                continuation_header="Продолжение ответа"
            )
            
        except Exception as e:
            logger.error(f"Ошибка при обработке запроса /ask: {e}", exc_info=True)
            await self.update_status(processing_msg, f"❌ Произошла ошибка: {str(e)}")
    
    def get_model_keyboard(self, page: int, current_model: str) -> tuple:
        """Создает клавиатуру с моделями для указанной страницы
        
        Returns:
            tuple: (keyboard, total_pages, start_idx, end_idx)
        """
        models_per_page = 10  # Количество моделей на странице
        total_models = len(self.available_models)
        total_pages = (total_models + models_per_page - 1) // models_per_page
        
        # Ограничиваем страницу допустимым диапазоном
        page = max(0, min(page, total_pages - 1))
        
        start_idx = page * models_per_page
        end_idx = min(start_idx + models_per_page, total_models)
        
        keyboard = []
        
        # Добавляем кнопки моделей
        for idx in range(start_idx, end_idx):
            model = self.available_models[idx]
            model_id = model["id"]
            model_name = model["name"]
            # Обрезаем название, если слишком длинное
            display_name = model_name if len(model_name) <= 35 else model_name[:32] + "..."
            # Добавляем галочку для текущей выбранной модели
            if model_id == current_model:
                display_name = f"✅ {display_name}"
            keyboard.append([InlineKeyboardButton(display_name, callback_data=f"sel:{idx}")])
        
        # Добавляем навигационные кнопки
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("◀️ Назад", callback_data=f"pg:{page-1}"))
        nav_buttons.append(InlineKeyboardButton(f"📄 {page+1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("Вперёд ▶️", callback_data=f"pg:{page+1}"))
        
        if nav_buttons:
            keyboard.append(nav_buttons)
        
        return keyboard, total_pages, start_idx, end_idx
    
    async def model_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /model - показывает список доступных моделей"""
        # Проверяем, что сообщение пришло из разрешенного канала
        if not self.is_authorized_channel(update):
            await update.message.reply_text("Доступ запрещен. Бот работает только в определенных каналах.")
            return
        
        chat_id = update.effective_chat.id
        
        # Проверяем, есть ли загруженные модели
        if not self.available_models:
            await update.message.reply_text("⏳ Загружаю список моделей...")
            self.fetch_openrouter_models()
        
        if not self.available_models:
            await update.message.reply_text("❌ Не удалось загрузить список моделей. Попробуйте позже.")
            return
        
        # Получаем текущую выбранную модель
        current_model = self.selected_models.get(chat_id, "")
        current_model_display = current_model if current_model else "Не выбрана"
        
        # Получаем клавиатуру для первой страницы
        keyboard, total_pages, start_idx, end_idx = self.get_model_keyboard(0, current_model)
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"🤖 *Выберите модель для команды /askmodel*\n\n"
            f"📌 Текущая: `{current_model_display}`\n"
            f"📊 Всего моделей: {len(self.available_models)}",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        logger.info(f"Отправлен список моделей, страница 1/{total_pages}")
    
    async def model_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик нажатий на кнопки выбора модели"""
        query = update.callback_query
        data = query.data
        
        logger.info(f"Получен callback: {data}")
        
        if not data:
            await query.answer()
            return
        
        chat_id = None
        if query.message and query.message.chat:
            chat_id = query.message.chat.id
        elif update.effective_chat:
            chat_id = update.effective_chat.id
        
        if chat_id is None:
            logger.warning("Не удалось определить chat_id для callback")
            await query.answer("Ошибка: не удалось определить чат")
            return
        
        # Проверяем, что чат авторизован
        if not self.is_authorized_channel(update):
            await query.answer("Доступ запрещен")
            return
        
        # Если список моделей пустой, пробуем загрузить заново
        if not self.available_models:
            self.fetch_openrouter_models()
            if not self.available_models:
                await query.answer("Ошибка: список моделей пуст")
                return
        
        current_model = self.selected_models.get(chat_id, "")
        
        # Обработка нажатия на номер страницы (ничего не делаем)
        if data == "noop":
            await query.answer()
            return
        
        # Обработка навигации по страницам
        if data.startswith("pg:"):
            try:
                page = int(data[3:])
                keyboard, total_pages, _, _ = self.get_model_keyboard(page, current_model)
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                current_model_display = current_model if current_model else "Не выбрана"
                
                await query.answer()
                await query.edit_message_text(
                    f"🤖 *Выберите модель для команды /askmodel*\n\n"
                    f"📌 Текущая: `{current_model_display}`\n"
                    f"📊 Всего моделей: {len(self.available_models)}",
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
                logger.info(f"Переход на страницу {page+1}/{total_pages}")
            except Exception as e:
                logger.error(f"Ошибка при навигации: {e}")
                await query.answer("Ошибка навигации")
            return
        
        # Обработка выбора модели
        if data.startswith("sel:"):
            try:
                model_idx = int(data[4:])
                
                if model_idx < 0 or model_idx >= len(self.available_models):
                    logger.error(f"Индекс модели вне диапазона: {model_idx}")
                    await query.answer("Ошибка: модель не найдена")
                    return
                
                model_id = self.available_models[model_idx]["id"]
                
                # Сохраняем выбранную модель
                self.selected_models[chat_id] = model_id
                self.save_selected_models()
                
                logger.info(f"Чат {chat_id} выбрал модель: {model_id}")
                
                # Определяем текущую страницу
                models_per_page = 10
                current_page = model_idx // models_per_page
                
                # Обновляем клавиатуру
                keyboard, total_pages, _, _ = self.get_model_keyboard(current_page, model_id)
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await query.answer("✅ Выбрана модель!")
                # Удаляем меню выбора, чтобы не захламлять чат
                try:
                    await query.delete_message()
                except Exception as e:
                    logger.warning(f"Не удалось удалить сообщение с меню выбора: {e}")
                    # Фоллбек: обновляем сообщение, если удалить не получилось
                    await query.edit_message_text(
                        f"✅ Модель выбрана: `{model_id}`",
                        parse_mode='Markdown'
                    )
            except Exception as e:
                logger.error(f"Ошибка при выборе модели: {e}", exc_info=True)
                await query.answer("Ошибка при выборе модели")
            return
        
        # Неизвестный callback
        logger.warning(f"Неизвестный callback_data: {data}")
        await query.answer()
    
    async def askmodel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /askmodel - отправляет запрос в выбранную модель"""
        # Проверяем, что сообщение пришло из разрешенного канала
        if not self.is_authorized_channel(update):
            await update.message.reply_text("Доступ запрещен. Бот работает только в определенных каналах.")
            return
        
        chat_id = update.effective_chat.id
        
        # Проверяем, выбрана ли модель
        if chat_id not in self.selected_models:
            await update.message.reply_text(
                "❌ Модель не выбрана!\n\n"
                "Используйте команду /model чтобы выбрать модель для запросов."
            )
            return
        
        selected_model = self.selected_models[chat_id]
        
        # Получаем текст сообщения
        message_text = update.message.text or ""
        
        # Извлекаем текст после команды /askmodel
        # Поддерживаем мультилайновый ввод
        if message_text.startswith('/askmodel'):
            command_end = 9  # Длина '/askmodel'
            if '@' in message_text[9:]:
                at_pos = message_text.find('@', 9)
                space_pos = message_text.find(' ', at_pos)
                newline_pos = message_text.find('\n', at_pos)
                if space_pos != -1 or newline_pos != -1:
                    command_end = min([pos for pos in [space_pos, newline_pos] if pos != -1])
                else:
                    command_end = len(message_text)
            prompt = message_text[command_end:].strip()
        else:
            if context.args:
                prompt = ' '.join(context.args)
            else:
                await update.message.reply_text(
                    "❌ Пожалуйста, укажите ваш вопрос после команды /askmodel\n\n"
                    f"📌 Текущая модель: `{selected_model}`\n\n"
                    "**Примеры использования:**\n"
                    "• `/askmodel Как правильно пить пиво?`",
                    parse_mode='Markdown'
                )
                return
        
        if not prompt or not prompt.strip():
            await update.message.reply_text(
                "❌ Пожалуйста, укажите ваш вопрос после команды /askmodel\n\n"
                f"📌 Текущая модель: `{selected_model}`",
                parse_mode='Markdown'
            )
            return
        
        logger.info(f"Обработка запроса /askmodel (модель: {selected_model}): {prompt[:100]}...")
        
        # Отправляем сообщение о начале обработки
        processing_msg = await update.message.reply_text(f"🤖 Отправляю запрос в модель `{selected_model}`...", parse_mode='Markdown')
        
        try:
            # Получаем базовую конфигурацию API и заменяем модель
            api_config = self.get_api_config("ask_api").copy()
            api_config["model"] = selected_model
            
            # Отправляем запрос в API
            await self.update_status(processing_msg, f"🤖 Ожидаю ответ от `{selected_model}`...")
            result = await self.ask_with_openrouter(prompt, api_config)
            
            if not result:
                await self.update_status(processing_msg, "❌ Ошибка при обработке запроса.")
                return
            
            # Обрабатываем результат
            response_text, generation_id = result
            
            # Отслеживаем стоимость для OpenRouter
            if generation_id:
                user = update.effective_user
                user_id = user.id
                username = user.username or ""
                first_name = user.first_name or ""
                last_name = user.last_name or ""
                await self.track_generation_cost(generation_id, user_id, username, first_name, last_name, "askmodel")
            
            # Отправляем результат
            await self.update_status(processing_msg, "✅ Готово!")
            
            import html as html_module
            safe_model = html_module.escape(selected_model)
            await self.send_ai_response(
                update.message, response_text,
                header=f"💬 <b>Ответ от</b> <code>{safe_model}</code><b>:</b>",
                continuation_header="Продолжение ответа"
            )

        except Exception as e:
            logger.error(f"Ошибка при обработке запроса /askmodel: {e}", exc_info=True)
            await self.update_status(processing_msg, f"❌ Произошла ошибка: {str(e)}")
    
    async def imagegen_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /imagegen"""
        # Проверяем, что сообщение пришло из разрешенного канала
        if not self.is_authorized_channel(update):
            await update.message.reply_text("Доступ запрещен. Бот работает только в определенных каналах.")
            return
        
        # Проверяем наличие текста в команде
        if not context.args:
            await update.message.reply_text("Пожалуйста, укажите описание для генерации: /imagegen <описание изображения>")
            return
        
        # Объединяем все аргументы в одну строку
        prompt = ' '.join(context.args)
        logger.info(f"Генерация изображения по запросу: {prompt}")
        
        try:
            # Создаем сообщение о начале обработки
            processing_msg = await update.message.reply_text(
                f"🎨 Генерирую изображение...\n\n📝 Запрос: {prompt}",
                reply_to_message_id=update.message.message_id
            )
            
            # Генерируем изображение через настроенный API
            image_result = await self.generate_image_with_ai(prompt, api_name="imagegen_api")
            
            if not image_result:
                await self.update_status(processing_msg, "❌ Ошибка при генерации изображения.")
                return
            
            # Извлекаем generation_id для отслеживания стоимости (даже при ошибке)
            generation_id = None
            if isinstance(image_result, dict) and 'generation_id' in image_result:
                generation_id = image_result['generation_id']
            
            # Проверяем на ошибку
            if isinstance(image_result, dict) and 'error' in image_result:
                await self.update_status(processing_msg, f"❌ {image_result['error']}")
                # Отслеживаем стоимость даже при ошибке
                if generation_id:
                    user = update.effective_user
                    user_id = user.id
                    username = user.username or ""
                    first_name = user.first_name or ""
                    last_name = user.last_name or ""
                    await self.track_generation_cost(generation_id, user_id, username, first_name, last_name, "imagegen")
                return
            
            # Отправляем результат
            await self.update_status(processing_msg, "✅ Изображение успешно сгенерировано!")
            
            # Проверяем, что мы получили - URL или base64 данные
            if isinstance(image_result, dict):
                # Если это словарь с base64 данными
                if 'data' in image_result and 'format' in image_result:
                    image_bytes = image_result['data']
                    image_format = image_result['format']
                    
                    # Сохраняем изображение в папку
                    chat_id = update.effective_chat.id
                    self.save_generated_image(image_bytes, image_format, chat_id, "imagegen")
                    
                    # Сохраняем в хранилище последних сгенерированных изображений
                    self.last_generated_images[chat_id] = image_bytes
                    
                    # Создаем BytesIO объект из байтов
                    image_file = BytesIO(image_bytes)
                    image_file.name = f"generated_image.{image_format}"
                    
                    # Отправляем изображение как файл
                    await update.message.reply_photo(
                        photo=image_file,
                        caption=f"🎨 **Сгенерированное изображение**\n\n📝 Запрос: {prompt}"
                    )
                # Если это словарь с URL
                elif 'url' in image_result:
                    image_url = image_result['url']
                    # Скачиваем изображение для сохранения
                    try:
                        response = requests.get(image_url, timeout=30)
                        if response.status_code == 200:
                            image_bytes = response.content
                            # Определяем формат по content-type
                            content_type = response.headers.get('content-type', 'image/jpeg')
                            image_format = content_type.split('/')[-1]
                            
                            # Сохраняем изображение
                            chat_id = update.effective_chat.id
                            self.save_generated_image(image_bytes, image_format, chat_id, "imagegen")
                            
                            # Сохраняем в хранилище
                            self.last_generated_images[chat_id] = image_bytes
                    except Exception as e:
                        logger.warning(f"Не удалось скачать изображение для сохранения: {e}")
                    
                    # Отправляем URL
                    await update.message.reply_photo(
                        photo=image_url,
                        caption=f"🎨 **Сгенерированное изображение**\n\n📝 Запрос: {prompt}"
                    )
            else:
                await self.update_status(processing_msg, "❌ Неизвестный формат изображения.")
            
            # Отслеживаем стоимость
            if generation_id:
                user = update.effective_user
                user_id = user.id
                username = user.username or ""
                first_name = user.first_name or ""
                last_name = user.last_name or ""
                await self.track_generation_cost(generation_id, user_id, username, first_name, last_name, "imagegen")
            
        except Exception as e:
            logger.error(f"Ошибка при генерации изображения: {e}")
            await update.message.reply_text(f"❌ Произошла ошибка: {str(e)}")
    
    async def abcgen_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /abcgen - генерация русской азбуки"""
        # Проверяем, что сообщение пришло из разрешенного канала
        if not self.is_authorized_channel(update):
            await update.message.reply_text("Доступ запрещен. Бот работает только в определенных каналах.")
            return
        
        # Проверяем наличие текста в команде
        if not context.args:
            await update.message.reply_text("Пожалуйста, укажите описание для генерации: /abcgen <тема азбуки>")
            return
        
        # Объединяем все аргументы в одну строку
        user_prompt = ' '.join(context.args)
        # Формируем полный промпт с инструкцией
        full_prompt = f'Нарисуй русскую азбуку "{user_prompt}" с заголовком и подписями. Avoid cropped borders. Content should correctly fit into the picture.'
        logger.info(f"Генерация русской азбуки по запросу: {user_prompt}")
        
        try:
            # Создаем сообщение о начале обработки
            processing_msg = await update.message.reply_text(
                f"🔤 Генерирую русскую азбуку...\n\n📝 Тема: {user_prompt}",
                reply_to_message_id=update.message.message_id
            )
            
            # Генерируем изображение через настроенный API
            image_result = await self.generate_image_with_ai(full_prompt, api_name="abcgen_api")
            
            if not image_result:
                await self.update_status(processing_msg, "❌ Ошибка при генерации азбуки.")
                return
            
            # Извлекаем generation_id для отслеживания стоимости (даже при ошибке)
            generation_id = None
            if isinstance(image_result, dict) and 'generation_id' in image_result:
                generation_id = image_result['generation_id']
            
            # Проверяем на ошибку
            if isinstance(image_result, dict) and 'error' in image_result:
                await self.update_status(processing_msg, f"❌ {image_result['error']}")
                # Отслеживаем стоимость даже при ошибке
                if generation_id:
                    user = update.effective_user
                    user_id = user.id
                    username = user.username or ""
                    first_name = user.first_name or ""
                    last_name = user.last_name or ""
                    await self.track_generation_cost(generation_id, user_id, username, first_name, last_name, "abcgen")
                return
            
            # Отправляем результат
            await self.update_status(processing_msg, "✅ Азбука успешно сгенерирована!")
            
            # Проверяем, что мы получили - URL или base64 данные
            if isinstance(image_result, dict):
                # Если это словарь с base64 данными
                if 'data' in image_result and 'format' in image_result:
                    image_bytes = image_result['data']
                    image_format = image_result['format']
                    
                    # Сохраняем изображение в папку
                    chat_id = update.effective_chat.id
                    self.save_generated_image(image_bytes, image_format, chat_id, "abcgen")
                    
                    # Сохраняем в хранилище последних сгенерированных изображений
                    self.last_generated_images[chat_id] = image_bytes
                    
                    # Создаем BytesIO объект из байтов
                    image_file = BytesIO(image_bytes)
                    image_file.name = f"alphabet_{user_prompt[:20]}.{image_format}"
                    
                    # Отправляем изображение как файл
                    await update.message.reply_photo(
                        photo=image_file,
                        caption=f"🔤 **Русская азбука**\n\n📝 Тема: {user_prompt}"
                    )
                # Если это словарь с URL
                elif 'url' in image_result:
                    image_url = image_result['url']
                    # Скачиваем изображение для сохранения
                    try:
                        response = requests.get(image_url, timeout=30)
                        if response.status_code == 200:
                            image_bytes = response.content
                            # Определяем формат по content-type
                            content_type = response.headers.get('content-type', 'image/jpeg')
                            image_format = content_type.split('/')[-1]
                            
                            # Сохраняем изображение
                            chat_id = update.effective_chat.id
                            self.save_generated_image(image_bytes, image_format, chat_id, "abcgen")
                            
                            # Сохраняем в хранилище
                            self.last_generated_images[chat_id] = image_bytes
                    except Exception as e:
                        logger.warning(f"Не удалось скачать изображение для сохранения: {e}")
                    
                    await update.message.reply_photo(
                        photo=image_url,
                        caption=f"🔤 **Русская азбука**\n\n📝 Тема: {user_prompt}"
                    )
            else:
                # Если это строка (URL)
                try:
                    response = requests.get(image_result, timeout=30)
                    if response.status_code == 200:
                        image_bytes = response.content
                        content_type = response.headers.get('content-type', 'image/jpeg')
                        image_format = content_type.split('/')[-1]
                        
                        chat_id = update.effective_chat.id
                        self.save_generated_image(image_bytes, image_format, chat_id, "abcgen")
                        self.last_generated_images[chat_id] = image_bytes
                except Exception as e:
                    logger.warning(f"Не удалось скачать изображение для сохранения: {e}")
                
                await update.message.reply_photo(
                    photo=image_result,
                    caption=f"🔤 **Русская азбука**\n\n📝 Тема: {user_prompt}"
                )
            
            # Отслеживаем стоимость успешного запроса
            if generation_id:
                user = update.effective_user
                user_id = user.id
                username = user.username or ""
                first_name = user.first_name or ""
                last_name = user.last_name or ""
                await self.track_generation_cost(generation_id, user_id, username, first_name, last_name, "abcgen")
            
        except Exception as e:
            logger.error(f"Ошибка при генерации азбуки: {e}")
            await update.message.reply_text(f"❌ Произошла ошибка: {str(e)}")
    
    async def imagechange_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /imagechange"""
        # Проверяем, что сообщение пришло из разрешенного канала
        if not self.is_authorized_channel(update):
            await update.message.reply_text("Доступ запрещен. Бот работает только в определенных каналах.")
            return
        
        # Проверяем наличие текста в команде
        if not context.args:
            await update.message.reply_text("Пожалуйста, укажите запрос для изменения: /imagechange <описание изменений>")
            return
        
        # Объединяем все аргументы в одну строку
        prompt = ' '.join(context.args)
        logger.info(f"Изменение изображения по запросу: {prompt}")
        
        try:
            # Получаем последнее изображение из чата
            chat_id = update.effective_chat.id
            image_data = await self.get_last_image_from_chat(update, context, chat_id)
            
            if not image_data:
                await update.message.reply_text(
                    "❌ Не найдено изображений в чате.\n\n"
                    "**Как использовать команду /imagechange:**\n"
                    "1. Сначала отправьте изображение в чат\n"
                    "2. Затем используйте команду `/imagechange <описание изменений>`\n\n"
                    "Например:\n"
                    "• `/imagechange сделай изображение в стиле аниме`\n"
                    "• `/imagechange добавь эффект акварели`"
                )
                return
            
            # Создаем сообщение о начале обработки
            processing_msg = await update.message.reply_text(
                f"✨ Изменяю изображение...\n\n📝 Запрос: {prompt}",
                reply_to_message_id=update.message.message_id
            )
            
            # Изменяем изображение через настроенный API
            image_result = await self.modify_image_with_ai(image_data, prompt, api_name="imagechange_api")
            
            if not image_result:
                await self.update_status(processing_msg, "❌ Ошибка при изменении изображения.")
                return
            
            # Извлекаем generation_id для отслеживания стоимости (даже при ошибке)
            generation_id = None
            if isinstance(image_result, dict) and 'generation_id' in image_result:
                generation_id = image_result['generation_id']
            
            # Проверяем на ошибку
            if isinstance(image_result, dict) and 'error' in image_result:
                await self.update_status(processing_msg, f"❌ {image_result['error']}")
                # Отслеживаем стоимость даже при ошибке
                if generation_id:
                    user = update.effective_user
                    user_id = user.id
                    username = user.username or ""
                    first_name = user.first_name or ""
                    last_name = user.last_name or ""
                    await self.track_generation_cost(generation_id, user_id, username, first_name, last_name, "imagechange")
                return
            
            # Отправляем результат
            await self.update_status(processing_msg, "✅ Изображение успешно изменено!")
            
            # Проверяем, что мы получили - URL или base64 данные
            if isinstance(image_result, dict):
                # Если это словарь с base64 данными
                if 'data' in image_result and 'format' in image_result:
                    image_bytes = image_result['data']
                    image_format = image_result['format']
                    
                    # Сохраняем изображение в папку
                    chat_id = update.effective_chat.id
                    self.save_generated_image(image_bytes, image_format, chat_id, "imagechange")
                    
                    # Сохраняем в хранилище последних сгенерированных изображений
                    self.last_generated_images[chat_id] = image_bytes
                    
                    # Создаем BytesIO объект из байтов
                    image_file = BytesIO(image_bytes)
                    image_file.name = f"modified_image.{image_format}"
                    
                    # Отправляем изображение как файл
                    await update.message.reply_photo(
                        photo=image_file,
                        caption=f"✨ **Изменённое изображение**\n\n📝 Запрос: {prompt}"
                    )
                # Если это словарь с URL
                elif 'url' in image_result:
                    image_url = image_result['url']
                    # Скачиваем изображение для сохранения
                    try:
                        response = requests.get(image_url, timeout=30)
                        if response.status_code == 200:
                            image_bytes = response.content
                            # Определяем формат по content-type
                            content_type = response.headers.get('content-type', 'image/jpeg')
                            image_format = content_type.split('/')[-1]
                            
                            # Сохраняем изображение
                            chat_id = update.effective_chat.id
                            self.save_generated_image(image_bytes, image_format, chat_id, "imagechange")
                            
                            # Сохраняем в хранилище
                            self.last_generated_images[chat_id] = image_bytes
                    except Exception as e:
                        logger.warning(f"Не удалось скачать изображение для сохранения: {e}")
                    
                    # Отправляем URL
                    await update.message.reply_photo(
                        photo=image_url,
                        caption=f"✨ **Изменённое изображение**\n\n📝 Запрос: {prompt}"
                    )
            else:
                await self.update_status(processing_msg, "❌ Неизвестный формат изображения.")
            
            # Отслеживаем стоимость
            if generation_id:
                user = update.effective_user
                user_id = user.id
                username = user.username or ""
                first_name = user.first_name or ""
                last_name = user.last_name or ""
                await self.track_generation_cost(generation_id, user_id, username, first_name, last_name, "imagechange")
            
        except Exception as e:
            logger.error(f"Ошибка при изменении изображения: {e}")
            await update.message.reply_text(f"❌ Произошла ошибка: {str(e)}")
    
    async def changelast_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /changelast - изменяет последнее сгенерированное изображение"""
        # Проверяем, что сообщение пришло из разрешенного канала
        if not self.is_authorized_channel(update):
            await update.message.reply_text("Доступ запрещен. Бот работает только в определенных каналах.")
            return
        
        # Проверяем наличие текста в команде
        if not context.args:
            await update.message.reply_text("Пожалуйста, укажите запрос для изменения: /changelast <описание изменений>")
            return
        
        # Объединяем все аргументы в одну строку
        prompt = ' '.join(context.args)
        logger.info(f"Изменение последнего сгенерированного изображения по запросу: {prompt}")
        
        try:
            # Получаем последнее сгенерированное изображение для этого чата
            chat_id = update.effective_chat.id
            
            if chat_id not in self.last_generated_images or not self.last_generated_images[chat_id]:
                await update.message.reply_text(
                    "❌ Не найдено сгенерированных изображений.\n\n"
                    "**Как использовать команду /changelast:**\n"
                    "1. Сначала сгенерируйте изображение с помощью `/imagegen` или `/imagechange`\n"
                    "2. Затем используйте команду `/changelast <описание изменений>` для последовательных изменений\n\n"
                    "Это удобно для постепенной доработки одного и того же изображения без копирования его обратно в чат!"
                )
                return
            
            image_data = self.last_generated_images[chat_id]
            
            # Создаем сообщение о начале обработки
            processing_msg = await update.message.reply_text(
                f"✨ Изменяю последнее сгенерированное изображение...\n\n📝 Запрос: {prompt}",
                reply_to_message_id=update.message.message_id
            )
            
            # Изменяем изображение через настроенный API
            image_result = await self.modify_image_with_ai(image_data, prompt, api_name="changelast_api")
            
            if not image_result:
                await self.update_status(processing_msg, "❌ Ошибка при изменении изображения.")
                return
            
            # Извлекаем generation_id для отслеживания стоимости (даже при ошибке)
            generation_id = None
            if isinstance(image_result, dict) and 'generation_id' in image_result:
                generation_id = image_result['generation_id']
            
            # Проверяем на ошибку
            if isinstance(image_result, dict) and 'error' in image_result:
                await self.update_status(processing_msg, f"❌ {image_result['error']}")
                # Отслеживаем стоимость даже при ошибке
                if generation_id:
                    user = update.effective_user
                    user_id = user.id
                    username = user.username or ""
                    first_name = user.first_name or ""
                    last_name = user.last_name or ""
                    await self.track_generation_cost(generation_id, user_id, username, first_name, last_name, "changelast")
                return
            
            # Отправляем результат
            await self.update_status(processing_msg, "✅ Изображение успешно изменено!")
            
            # Проверяем, что мы получили - URL или base64 данные
            if isinstance(image_result, dict):
                # Если это словарь с base64 данными
                if 'data' in image_result and 'format' in image_result:
                    image_bytes = image_result['data']
                    image_format = image_result['format']
                    
                    # Сохраняем изображение в папку
                    self.save_generated_image(image_bytes, image_format, chat_id, "changelast")
                    
                    # Обновляем хранилище последних сгенерированных изображений
                    self.last_generated_images[chat_id] = image_bytes
                    
                    # Создаем BytesIO объект из байтов
                    image_file = BytesIO(image_bytes)
                    image_file.name = f"modified_image.{image_format}"
                    
                    # Отправляем изображение как файл
                    await update.message.reply_photo(
                        photo=image_file,
                        caption=f"✨ **Изменённое изображение**\n\n📝 Запрос: {prompt}"
                    )
                # Если это словарь с URL
                elif 'url' in image_result:
                    image_url = image_result['url']
                    # Скачиваем изображение для сохранения
                    try:
                        response = requests.get(image_url, timeout=30)
                        if response.status_code == 200:
                            image_bytes = response.content
                            # Определяем формат по content-type
                            content_type = response.headers.get('content-type', 'image/jpeg')
                            image_format = content_type.split('/')[-1]
                            
                            # Сохраняем изображение
                            self.save_generated_image(image_bytes, image_format, chat_id, "changelast")
                            
                            # Обновляем хранилище
                            self.last_generated_images[chat_id] = image_bytes
                    except Exception as e:
                        logger.warning(f"Не удалось скачать изображение для сохранения: {e}")
                    
                    # Отправляем URL
                    await update.message.reply_photo(
                        photo=image_url,
                        caption=f"✨ **Изменённое изображение**\n\n📝 Запрос: {prompt}"
                    )
            else:
                await self.update_status(processing_msg, "❌ Неизвестный формат изображения.")
            
            # Отслеживаем стоимость
            if generation_id:
                user = update.effective_user
                user_id = user.id
                username = user.username or ""
                first_name = user.first_name or ""
                last_name = user.last_name or ""
                await self.track_generation_cost(generation_id, user_id, username, first_name, last_name, "changelast")
            
        except Exception as e:
            logger.error(f"Ошибка при изменении последнего сгенерированного изображения: {e}")
            await update.message.reply_text(f"❌ Произошла ошибка: {str(e)}")
    
    async def mergeimage_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /mergeimage - обработка нескольких изображений"""
        # Проверяем, что сообщение пришло из разрешенного канала
        if not self.is_authorized_channel(update):
            await update.message.reply_text("Доступ запрещен. Бот работает только в определенных каналах.")
            return
        
        # Проверяем наличие текста в команде
        if not context.args:
            await update.message.reply_text("Пожалуйста, укажите запрос: /mergeimage <описание запроса>")
            return
        
        # Объединяем все аргументы в одну строку
        prompt = ' '.join(context.args)
        logger.info(f"Обработка нескольких изображений по запросу: {prompt}")
        
        try:
            # Получаем изображения из последнего сообщения
            chat_id = update.effective_chat.id
            
            if chat_id not in self.last_multiple_images or not self.last_multiple_images[chat_id]:
                await update.message.reply_text(
                    "❌ Не найдено изображений в последнем сообщении.\n\n"
                    "**Как использовать команду /mergeimage:**\n"
                    "1. Отправьте несколько изображений в одном сообщении (группой)\n"
                    "2. Затем используйте команду `/mergeimage <описание запроса>`\n\n"
                    "Например:\n"
                    "• `/mergeimage объедини эти изображения в одно`\n"
                    "• `/mergeimage сравни эти изображения`\n"
                    "• `/mergeimage найди отличия между изображениями`"
                )
                return
            
            # Получаем последнюю группу изображений
            latest_group_id = list(self.last_multiple_images[chat_id].keys())[-1]
            images_list = self.last_multiple_images[chat_id][latest_group_id]
            
            # Проверяем, что изображений больше одного
            if len(images_list) < 2:
                await update.message.reply_text(
                    f"❌ Найдено только {len(images_list)} изображение. Для команды /mergeimage нужно минимум 2 изображения.\n\n"
                    "**Как отправить несколько изображений:**\n"
                    "1. Выберите несколько изображений в Telegram\n"
                    "2. Отправьте их одним сообщением (они будут сгруппированы)\n"
                    "3. Используйте команду `/mergeimage <запрос>`"
                )
                return
            
            # Создаем сообщение о начале обработки
            processing_msg = await update.message.reply_text(
                f"🔀 Обрабатываю {len(images_list)} изображений...\n\n📝 Запрос: {prompt}",
                reply_to_message_id=update.message.message_id
            )
            
            # Отправляем изображения в AI API
            result = await self.process_multiple_images_with_ai(images_list, prompt, api_name="mergeimage_api")
            
            if not result:
                await self.update_status(processing_msg, "❌ Ошибка при обработке изображений.")
                return
            
            # Извлекаем generation_id для отслеживания стоимости (даже при ошибке)
            generation_id = None
            if isinstance(result, dict) and 'generation_id' in result:
                generation_id = result['generation_id']
            
            # Проверяем на ошибку
            if isinstance(result, dict) and 'error' in result:
                await self.update_status(processing_msg, f"❌ {result['error']}")
                # Отслеживаем стоимость даже при ошибке
                if generation_id:
                    user = update.effective_user
                    user_id = user.id
                    username = user.username or ""
                    first_name = user.first_name or ""
                    last_name = user.last_name or ""
                    await self.track_generation_cost(generation_id, user_id, username, first_name, last_name, "mergeimage")
                return
            
            # Отправляем результат
            await self.update_status(processing_msg, "✅ Изображения успешно обработаны!")
            
            # Проверяем тип результата
            if isinstance(result, dict):
                # Если это сгенерированное изображение
                if 'data' in result and 'format' in result:
                    image_bytes = result['data']
                    image_format = result['format']
                    
                    # Сохраняем изображение
                    self.save_generated_image(image_bytes, image_format, chat_id, "mergeimage")
                    
                    # Сохраняем в хранилище последних сгенерированных
                    self.last_generated_images[chat_id] = image_bytes
                    
                    # Отправляем изображение
                    image_file = BytesIO(image_bytes)
                    image_file.name = f"merged_image.{image_format}"
                    
                    await update.message.reply_photo(
                        photo=image_file,
                        caption=f"🔀 **Результат обработки {len(images_list)} изображений**\n\n📝 Запрос: {prompt}"
                    )
                # Если это URL изображения
                elif 'url' in result:
                    image_url = result['url']
                    # Скачиваем изображение для сохранения
                    try:
                        response = requests.get(image_url, timeout=30)
                        if response.status_code == 200:
                            image_bytes = response.content
                            content_type = response.headers.get('content-type', 'image/jpeg')
                            image_format = content_type.split('/')[-1]
                            
                            # Сохраняем изображение
                            self.save_generated_image(image_bytes, image_format, chat_id, "mergeimage")
                            self.last_generated_images[chat_id] = image_bytes
                    except Exception as e:
                        logger.warning(f"Не удалось скачать изображение для сохранения: {e}")
                    
                    await update.message.reply_photo(
                        photo=image_url,
                        caption=f"🔀 **Результат обработки {len(images_list)} изображений**\n\n📝 Запрос: {prompt}"
                    )
                # Если это текстовый ответ
                elif 'description' in result:
                    description = result['description']
                    await self.send_ai_response(
                        update.message, description,
                        header=f"🔀 <b>Результат обработки {len(images_list)} изображений:</b>",
                        continuation_header="Продолжение"
                    )
            
            # Отслеживаем стоимость
            if generation_id:
                user = update.effective_user
                user_id = user.id
                username = user.username or ""
                first_name = user.first_name or ""
                last_name = user.last_name or ""
                await self.track_generation_cost(generation_id, user_id, username, first_name, last_name, "mergeimage")
            
        except Exception as e:
            logger.error(f"Ошибка при обработке нескольких изображений: {e}")
            await update.message.reply_text(f"❌ Произошла ошибка: {str(e)}")
    
    async def balance_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /balance - проверка баланса OpenRouter"""
        # Проверяем, что сообщение пришло из разрешенного канала
        if not self.is_authorized_channel(update):
            await update.message.reply_text(
                "❌ Эта команда доступна только в авторизованном канале."
            )
            return
        
        try:
            await update.message.reply_text("💰 Запрашиваю информацию о балансе...")
            
            # Получаем конфигурацию API
            api_config = self.get_api_config("balance_api")
            
            # Делаем запрос к API
            url = api_config["url"]
            headers = {
                "Authorization": f"Bearer {api_config['key']}"
            }
            
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            
            result = response.json()
            logger.info(f"Ответ OpenRouter API (balance): {result}")
            
            # Извлекаем данные
            data = result.get("data", {})
            total_credits = data.get("total_credits", 0)
            total_usage = data.get("total_usage", 0)
            
            # Вычисляем остаток
            remaining_balance = total_credits - total_usage
            
            # Формируем красивое сообщение с HTML форматированием
            message = (
                f"💰 <b>Баланс OpenRouter:</b>\n\n"
                f"💳 Всего кредитов: ${total_credits:.2f}\n"
                f"📊 Использовано: ${total_usage:.4f}\n"
                f"✅ Остаток: <b>${remaining_balance:.4f}</b>"
            )
            
            await update.message.reply_text(message, parse_mode='HTML')
            logger.info(f"Баланс успешно получен: ${remaining_balance:.4f}")
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Ошибка при запросе баланса: {e}")
            await update.message.reply_text(
                f"❌ Ошибка при запросе баланса: {str(e)}\n\n"
                "Проверьте ваш API ключ OpenRouter."
            )
        except (KeyError, ValueError) as e:
            logger.error(f"Ошибка при обработке ответа о балансе: {e}")
            await update.message.reply_text(
                f"❌ Ошибка при обработке ответа: {str(e)}\n\n"
                "Неожиданный формат ответа от OpenRouter API."
            )
        except Exception as e:
            logger.error(f"Неожиданная ошибка при проверке баланса: {e}")
            await update.message.reply_text(f"❌ Произошла неожиданная ошибка: {str(e)}")
    
    async def statistics_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /statistics - показывает статистику расходов пользователей"""
        # Проверяем, что сообщение пришло из разрешенного канала
        if not self.is_authorized_channel(update):
            await update.message.reply_text(
                "❌ Эта команда доступна только в авторизованном канале."
            )
            return
        
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Получаем статистику пользователей, отсортированную по расходам
            cursor.execute('''
                SELECT user_id, username, first_name, last_name, total_spent, total_requests
                FROM user_statistics
                ORDER BY total_spent DESC
            ''')
            
            users = cursor.fetchall()
            conn.close()
            
            if not users:
                await update.message.reply_text(
                    "📊 <b>Статистика использования</b>\n\n"
                    "Пока нет данных о расходах.",
                    parse_mode='HTML'
                )
                return
            
            # Формируем сообщение со статистикой
            message_parts = ["📊 <b>Статистика расходов пользователей</b>\n"]
            
            total_all_users = 0
            for idx, (user_id, username, first_name, last_name, total_spent, total_requests) in enumerate(users, 1):
                total_all_users += total_spent
                
                # Формируем имя пользователя (приоритет username)
                display_name = ""
                if username:
                    display_name = f"@{username}"
                elif first_name:
                    display_name = first_name
                    if last_name:
                        display_name += f" {last_name}"
                else:
                    display_name = f"User {user_id}"
                
                # Добавляем эмодзи для топ-3
                medal = ""
                if idx == 1:
                    medal = "🥇 "
                elif idx == 2:
                    medal = "🥈 "
                elif idx == 3:
                    medal = "🥉 "
                
                # Всё в одну строку
                message_parts.append(
                    f"\n{medal}<b>{idx}.</b> {display_name} • ${total_spent:.4f} • {total_requests} запросов"
                )
            
            # Добавляем общую сумму
            message_parts.append(
                f"\n\n💵 <b>Всего:</b> ${total_all_users:.6f} | 👥 {len(users)} юзеров"
            )
            
            message = "".join(message_parts)
            
            # Telegram имеет ограничение на длину сообщения (4096 символов)
            if len(message) > 4000:
                # Разбиваем на несколько сообщений
                await update.message.reply_text(
                    "📊 <b>Статистика расходов пользователей</b>\n\n"
                    "Слишком много данных, отправляю топ-20...",
                    parse_mode='HTML'
                )
                
                message_parts = ["📊 <b>Топ-20 пользователей</b>\n"]
                for idx, (user_id, username, first_name, last_name, total_spent, total_requests) in enumerate(users[:20], 1):
                    # Формируем имя пользователя (приоритет username)
                    display_name = ""
                    if username:
                        display_name = f"@{username}"
                    elif first_name:
                        display_name = first_name
                        if last_name:
                            display_name += f" {last_name}"
                    else:
                        display_name = f"User {user_id}"
                    
                    medal = ""
                    if idx == 1:
                        medal = "🥇 "
                    elif idx == 2:
                        medal = "🥈 "
                    elif idx == 3:
                        medal = "🥉 "
                    
                    # Всё в одну строку
                    message_parts.append(
                        f"\n{medal}<b>{idx}.</b> {display_name} • ${total_spent:.4f} • {total_requests} запросов"
                    )
                
                message_parts.append(f"\n\n💵 <b>Всего:</b> ${total_all_users:.6f} | 👥 {len(users)} юзеров")
                message = "".join(message_parts)
            
            await update.message.reply_text(message, parse_mode='HTML')
            logger.info("Статистика расходов пользователей успешно отправлена")
            
        except Exception as e:
            logger.error(f"Ошибка при получении статистики: {e}")
            await update.message.reply_text(
                f"❌ Произошла ошибка при получении статистики: {str(e)}"
            )
    
    async def reload_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /reload - перезагружает конфигурацию бота"""
        # Проверяем, что сообщение пришло из разрешенного канала
        if not self.is_authorized_channel(update):
            await update.message.reply_text("Доступ запрещен. Бот работает только в определенных каналах.")
            return
        
        try:
            # Отправляем сообщение о начале перезагрузки
            processing_msg = await update.message.reply_text(
                "🔄 Перезагружаю конфигурацию...",
                reply_to_message_id=update.message.message_id
            )
            
            # Перезагружаем конфигурацию
            success = self.reload_config()
            
            if success:
                # Получаем информацию о текущих провайдерах
                providers_info = []
                for api_name in self.config:
                    if api_name.endswith('_api') and isinstance(self.config[api_name], dict):
                        provider = self.config[api_name].get("provider", "неизвестно")
                        model = self.config[api_name].get(provider, {}).get("model", "неизвестно")
                        providers_info.append(f"• <b>{api_name.replace('_api', '')}</b>: {provider} ({model})")
                
                providers_text = "\n".join(providers_info) if providers_info else "• Нет настроенных API"
                
                await self.update_status(
                    processing_msg,
                    f"✅ <b>Конфигурация успешно перезагружена!</b>\n\n"
                    f"📋 <b>Текущие настройки:</b>\n{providers_text}\n\n"
                    f"🔄 Все команды теперь используют новые настройки."
                )
                logger.info("Команда /reload выполнена успешно")
            else:
                await self.update_status(
                    processing_msg,
                    "❌ <b>Ошибка при перезагрузке конфигурации!</b>\n\n"
                    "Проверьте файл config.json на наличие ошибок."
                )
                logger.error("Команда /reload завершилась с ошибкой")
                
        except Exception as e:
            logger.error(f"Ошибка в команде /reload: {e}")
            await update.message.reply_text(
                f"❌ Произошла ошибка при перезагрузке: {str(e)}"
            )
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик для всех сообщений"""
        # Сохраняем изображения для последующего использования
        if update.message:
            chat_id = update.effective_chat.id
            
            # Инициализируем список для множественных изображений
            multiple_images = []
            
            try:
                if update.message.photo:
                    # Сохраняем фото с максимальным разрешением
                    photo = update.message.photo[-1]
                    try:
                        # Добавляем таймаут для загрузки файла (60 секунд)
                        file = await asyncio.wait_for(
                            context.bot.get_file(photo.file_id),
                            timeout=60.0
                        )
                        image_data = await asyncio.wait_for(
                            file.download_as_bytearray(),
                            timeout=120.0  # 2 минуты на загрузку больших файлов
                        )
                        image_bytes = bytes(image_data)
                        self.last_images[chat_id] = image_bytes
                        multiple_images.append(image_bytes)
                        logger.info(f"Сохранено изображение для чата {chat_id}, размер: {len(image_bytes)} байт")
                    except asyncio.TimeoutError:
                        logger.error(f"Таймаут при загрузке изображения для чата {chat_id}")
                    except Exception as e:
                        logger.error(f"Ошибка при загрузке изображения для чата {chat_id}: {e}", exc_info=True)
                elif update.message.document and update.message.document.mime_type and update.message.document.mime_type.startswith('image/'):
                    # Сохраняем документ-изображение
                    try:
                        # Добавляем таймаут для загрузки файла (60 секунд)
                        file = await asyncio.wait_for(
                            context.bot.get_file(update.message.document.file_id),
                            timeout=60.0
                        )
                        image_data = await asyncio.wait_for(
                            file.download_as_bytearray(),
                            timeout=120.0  # 2 минуты на загрузку больших файлов
                        )
                        image_bytes = bytes(image_data)
                        self.last_images[chat_id] = image_bytes
                        multiple_images.append(image_bytes)
                        logger.info(f"Сохранено изображение-документ для чата {chat_id}, размер: {len(image_bytes)} байт")
                    except asyncio.TimeoutError:
                        logger.error(f"Таймаут при загрузке изображения-документа для чата {chat_id}")
                    except Exception as e:
                        logger.error(f"Ошибка при загрузке изображения-документа для чата {chat_id}: {e}", exc_info=True)
            except Exception as e:
                # Логируем общую ошибку, но не отправляем сообщение пользователю
                # так как это может быть просто загрузка изображения без команды
                logger.error(f"Неожиданная ошибка при обработке сообщения с изображением для чата {chat_id}: {e}", exc_info=True)
            
            # Проверяем media_group_id для группы изображений
            if update.message.media_group_id:
                # Если есть media_group_id, это часть группы медиа
                if chat_id not in self.last_multiple_images:
                    self.last_multiple_images[chat_id] = {}
                
                media_group_id = update.message.media_group_id
                if media_group_id not in self.last_multiple_images[chat_id]:
                    self.last_multiple_images[chat_id][media_group_id] = []
                
                if multiple_images:
                    self.last_multiple_images[chat_id][media_group_id].extend(multiple_images)
                    logger.info(f"Добавлено изображение в группу {media_group_id}, всего: {len(self.last_multiple_images[chat_id][media_group_id])}")
            elif multiple_images:
                # Одиночное изображение - сохраняем как группу из одного
                self.last_multiple_images[chat_id] = {'single': multiple_images}
                logger.info(f"Сохранено одиночное изображение для чата {chat_id}")
        
        # Если сообщение начинается с /summary, но не обработалось как команда
        if update.message and update.message.text and update.message.text.startswith('/summary'):
            await self.summary_command(update, context)
        # Если сообщение начинается с /describe, но не обработалось как команда
        elif update.message and update.message.text and update.message.text.startswith('/describe'):
            await self.describe_command(update, context)
        # Если сообщение начинается с /askmodel, но не обработалось как команда (проверяем ДО /ask!)
        elif update.message and update.message.text and update.message.text.startswith('/askmodel'):
            await self.askmodel_command(update, context)
        # Если сообщение начинается с /ask, но не обработалось как команда
        elif update.message and update.message.text and update.message.text.startswith('/ask'):
            await self.ask_command(update, context)
        # Если сообщение начинается с /model, но не обработалось как команда
        elif update.message and update.message.text and update.message.text.startswith('/model'):
            await self.model_command(update, context)
        # Если сообщение начинается с /imagegen, но не обработалось как команда
        elif update.message and update.message.text and update.message.text.startswith('/imagegen'):
            await self.imagegen_command(update, context)
        # Если сообщение начинается с /imagechange, но не обработалось как команда
        elif update.message and update.message.text and update.message.text.startswith('/imagechange'):
            await self.imagechange_command(update, context)
        # Если сообщение начинается с /changelast, но не обработалось как команда
        elif update.message and update.message.text and update.message.text.startswith('/changelast'):
            await self.changelast_command(update, context)
        # Если сообщение начинается с /mergeimage, но не обработалось как команда
        elif update.message and update.message.text and update.message.text.startswith('/mergeimage'):
            await self.mergeimage_command(update, context)
    
    def is_authorized_channel(self, update: Update) -> bool:
        """Проверяет, разрешен ли канал для использования бота"""
        # Поддержка как одиночного ID, так и списка ID; а также альтернативного ключа allowed_channel_ids
        allowed_channel_id = self.config.get("allowed_channel_id")
        allowed_channel_ids = self.config.get("allowed_channel_ids")
        chat_id = update.effective_chat.id
        chat_id_str = str(chat_id)

        # Если ничего не указано или стоит заглушка — разрешаем всем
        if (allowed_channel_id is None and allowed_channel_ids is None) or allowed_channel_id == "YOUR_CHANNEL_ID":
            return True

        # Если указан список ID (в любом ключе)
        if isinstance(allowed_channel_ids, list):
            return any(chat_id_str == str(cid) for cid in allowed_channel_ids)
        if isinstance(allowed_channel_id, list):
            return any(chat_id_str == str(cid) for cid in allowed_channel_id)

        # Иначе трактуем как одиночное значение
        if allowed_channel_ids is not None:
            return chat_id_str == str(allowed_channel_ids)
        if allowed_channel_id is not None:
            return chat_id_str == str(allowed_channel_id)

        return True
    
    def convert_cookies_to_utf8(self, cookies_file: str):
        """Конвертирует файл cookies в UTF-8"""
        try:
            # Пробуем разные кодировки
            for encoding in ['utf-8', 'cp1251', 'latin1', 'iso-8859-1']:
                try:
                    with open(cookies_file, 'r', encoding=encoding) as f:
                        content = f.read()
                    
                    # Если файл успешно прочитан, сохраняем в UTF-8
                    with open(cookies_file, 'w', encoding='utf-8') as f:
                        f.write(content)
                    
                    logger.info(f"Cookies файл конвертирован в UTF-8 (исходная кодировка: {encoding})")
                    return
                    
                except UnicodeDecodeError:
                    continue
            
            # Если все кодировки не подошли, читаем как байты и декодируем с ошибками
            with open(cookies_file, 'rb') as f:
                content = f.read()
            
            # Декодируем с заменой нечитаемых символов
            text_content = content.decode('utf-8', errors='replace')
            
            with open(cookies_file, 'w', encoding='utf-8') as f:
                f.write(text_content)
            
            logger.info("Cookies файл конвертирован в UTF-8 с заменой нечитаемых символов")
            
        except Exception as e:
            logger.error(f"Ошибка при конвертации cookies файла: {e}")
    
    def clean_transcript(self, transcript: str) -> str:
        """Очищает транскрипт от таймкодов и нежелательных фраз"""
        try:
            lines = transcript.split('\n')
            cleaned_lines = []
            
            # Слова и фразы для удаления (регистронезависимо)
            unwanted_phrases = [
                'torzok',
                'продолжение следует'
            ]
            
            for line in lines:
                original_line = line
                
                # Удаляем таймкоды Whisper (формат [02:40.000 --> 02:42.000])
                line = re.sub(r'\[\d{1,2}:\d{2}\.\d{3}\s*-->\s*\d{1,2}:\d{2}\.\d{3}\]', '', line)
                # Удаляем таймкоды с разным количеством цифр после точки
                line = re.sub(r'\[\d{1,2}:\d{2}\.\d{1,3}\s*-->\s*\d{1,2}:\d{2}\.\d{1,3}\]', '', line)
                # Удаляем таймкоды без миллисекунд (формат [02:40 --> 02:42])
                line = re.sub(r'\[\d{1,2}:\d{2}\s*-->\s*\d{1,2}:\d{2}\]', '', line)
                # Удаляем другие возможные форматы таймкодов
                line = re.sub(r'\[?\d{1,2}:\d{2}:\d{2}\]?', '', line)
                line = re.sub(r'\[?\d{1,2}:\d{2}\]?', '', line)
                
                # Логируем, если таймкод был удален
                if original_line != line and '[' in original_line and ']' in original_line:
                    logger.info(f"Удален таймкод: '{original_line.strip()}' -> '{line.strip()}'")
                
                # Удаляем лишние пробелы
                line = line.strip()
                
                # Пропускаем пустые строки
                if not line:
                    continue
                
                # Проверяем, содержит ли строка нежелательные фразы
                line_lower = line.lower()
                should_skip = False
                
                for phrase in unwanted_phrases:
                    if phrase in line_lower:
                        should_skip = True
                        logger.info(f"Удаляю строку с нежелательной фразой '{phrase}': {line}")
                        break
                
                if not should_skip:
                    cleaned_lines.append(line)
            
            cleaned_transcript = '\n'.join(cleaned_lines)
            
            # Удаляем множественные переносы строк
            cleaned_transcript = re.sub(r'\n\s*\n\s*\n+', '\n\n', cleaned_transcript)
            
            logger.info(f"Транскрипт очищен: {len(lines)} строк -> {len(cleaned_lines)} строк")
            return cleaned_transcript.strip()
            
        except Exception as e:
            logger.error(f"Ошибка при очистке транскрипта: {e}")
            return transcript  # Возвращаем исходный транскрипт в случае ошибки
    
    async def check_video_availability(self, youtube_url: str):
        """Проверяет доступность видео и получает информацию о нем"""
        try:
            # Команда для получения информации о видео
            info_cmd = [
                str(Path(self.config["yt_dlp_path"]) / "yt-dlp.exe"),
                "--dump-json",
                "--no-warnings",
                youtube_url
            ]
            
            # Добавляем cookies, если они указаны
            if self.config.get("youtube_cookies") and self.config["youtube_cookies"].strip():
                info_cmd.extend(["--cookies", self.config["youtube_cookies"]])
            
            logger.info(f"Выполняю диагностическую команду: {' '.join(info_cmd)}")
            
            process = await asyncio.create_subprocess_exec(
                *info_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            
            # Декодируем вывод
            try:
                stdout_text = stdout.decode('utf-8')
                stderr_text = stderr.decode('utf-8')
            except UnicodeDecodeError:
                stdout_text = stdout.decode('utf-8', errors='replace')
                stderr_text = stderr.decode('utf-8', errors='replace')
            
            if process.returncode == 0:
                logger.info("Видео доступно, но не удалось скачать аудио")
                logger.info(f"Информация о видео: {stdout_text[:200]}...")
            else:
                logger.error(f"Видео недоступно: {stderr_text}")
                
                # Пробуем без cookies
                logger.info("Пробую без cookies...")
                info_cmd_no_cookies = [
                    str(Path(self.config["yt_dlp_path"]) / "yt-dlp.exe"),
                    "--dump-json",
                    "--no-warnings",
                    youtube_url
                ]
                
                process2 = await asyncio.create_subprocess_exec(
                    *info_cmd_no_cookies,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                
                stdout2, stderr2 = await process2.communicate()
                
                try:
                    stderr2_text = stderr2.decode('utf-8')
                except UnicodeDecodeError:
                    stderr2_text = stderr2.decode('utf-8', errors='replace')
                
                if process2.returncode == 0:
                    logger.info("Видео доступно без cookies")
                else:
                    logger.error(f"Видео недоступно даже без cookies: {stderr2_text}")
                    
                    # Попробуем скачать без cookies
                    logger.info("Пробую скачать без cookies...")
                    return await self.download_without_cookies(youtube_url)
                    
        except Exception as e:
            logger.error(f"Ошибка при проверке доступности видео: {e}")
    
    async def download_without_cookies(self, youtube_url: str) -> Optional[Path]:
        """Пробует скачать видео без cookies"""
        try:
            # Создаем уникальное имя файла
            import uuid
            audio_filename = f"audio_{uuid.uuid4().hex}.mp3"
            audio_path = self.temp_dir / audio_filename
            
            # Упрощенная команда без cookies
            cmd = [
                str(Path(self.config["yt_dlp_path"]) / "yt-dlp.exe"),
                "-x",
                "--output", str(audio_path),
                "--format", "bestaudio",
                "--no-warnings",
                youtube_url
            ]
            
            logger.info(f"Выполняю команду без cookies: {' '.join(cmd)}")
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            
            # Декодируем вывод
            try:
                stderr_text = stderr.decode('utf-8')
            except UnicodeDecodeError:
                stderr_text = stderr.decode('utf-8', errors='replace')
            
            if process.returncode == 0 and audio_path.exists() and audio_path.stat().st_size > 0:
                logger.info(f"Аудио успешно скачано без cookies: {audio_path}")
                return audio_path
            else:
                logger.error(f"Не удалось скачать без cookies: {stderr_text}")
                return None
                
        except Exception as e:
            logger.error(f"Ошибка при скачивании без cookies: {e}")
            return None
    
    async def download_audio(self, youtube_url: str) -> Optional[Path]:
        """Скачивает аудио с YouTube используя yt-dlp"""
        try:
            # Создаем уникальное имя файла
            import uuid
            audio_filename = f"audio_{uuid.uuid4().hex}.mp3"
            audio_path = self.temp_dir / audio_filename
            
            # Команда yt-dlp с обходом ограничений
            cmd = [
                str(Path(self.config["yt_dlp_path"]) / "yt-dlp.exe"),
                "-x",  # Извлекать только аудио
                "--audio-format", "mp3",
                "--output", str(audio_path),
                "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            ]
            
            # Добавляем cookies, если они указаны
            if self.config.get("youtube_cookies") and self.config["youtube_cookies"].strip():
                cookies_file = self.config["youtube_cookies"]
                # Проверяем, что файл cookies существует и конвертируем его в UTF-8
                if os.path.exists(cookies_file):
                    self.convert_cookies_to_utf8(cookies_file)
                cmd.extend(["--cookies", cookies_file])
            
            cmd.append(youtube_url)
            
            logger.info(f"Выполняю команду: {' '.join(cmd)}")
            
            # Выполняем команду
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            
            # Декодируем вывод с правильной кодировкой
            try:
                stderr_text = stderr.decode('utf-8')
            except UnicodeDecodeError:
                stderr_text = stderr.decode('utf-8', errors='replace')
            
            # Если первая попытка не удалась, пробуем альтернативные методы
            if process.returncode != 0 or not (audio_path.exists() and audio_path.stat().st_size > 0):
                logger.warning(f"Первая попытка не удалась: {stderr_text}")
                logger.info("Пробую альтернативные методы...")
                
                # Пробуем с другими extractor args
                alternative_cmd = [
                    str(Path(self.config["yt_dlp_path"]) / "yt-dlp.exe"),
                    "-x",
                    "--audio-format", "mp3",
                    "--output", str(audio_path),
                ]
                
                # Добавляем cookies и в альтернативную команду
                if self.config.get("youtube_cookies") and self.config["youtube_cookies"].strip():
                    cookies_file = self.config["youtube_cookies"]
                    if os.path.exists(cookies_file):
                        self.convert_cookies_to_utf8(cookies_file)
                    alternative_cmd.extend(["--cookies", cookies_file])
                
                alternative_cmd.append(youtube_url)
                
                logger.info(f"Выполняю альтернативную команду: {' '.join(alternative_cmd)}")
                
                process2 = await asyncio.create_subprocess_exec(
                    *alternative_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                
                stdout2, stderr2 = await process2.communicate()
                
                # Декодируем вывод второй попытки
                try:
                    stderr2_text = stderr2.decode('utf-8')
                except UnicodeDecodeError:
                    stderr2_text = stderr2.decode('utf-8', errors='replace')
                
                if process2.returncode != 0:
                    logger.error(f"Ошибка yt-dlp (все попытки): {stderr2_text}")
                    return None
                
                if not (audio_path.exists() and audio_path.stat().st_size > 0):
                    logger.warning("Вторая попытка не удалась, пробую упрощенную команду...")
                    
                    # Третья попытка - упрощенная команда
                    simple_cmd = [
                        str(Path(self.config["yt_dlp_path"]) / "yt-dlp.exe"),
                        "-x",
                        "--output", str(audio_path),
                        "--format", "bestaudio",
                    ]
                    
                    if self.config.get("youtube_cookies") and self.config["youtube_cookies"].strip():
                        simple_cmd.extend(["--cookies", self.config["youtube_cookies"]])
                    
                    simple_cmd.append(youtube_url)
                    
                    logger.info(f"Выполняю упрощенную команду: {' '.join(simple_cmd)}")
                    
                    process3 = await asyncio.create_subprocess_exec(
                        *simple_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    
                    stdout3, stderr3 = await process3.communicate()
                    
                    # Декодируем вывод третьей попытки
                    try:
                        stderr3_text = stderr3.decode('utf-8')
                    except UnicodeDecodeError:
                        stderr3_text = stderr3.decode('utf-8', errors='replace')
                    
                    if process3.returncode != 0:
                        logger.error(f"Ошибка yt-dlp (все попытки): {stderr3_text}")
                        return None
                    
                    if not (audio_path.exists() and audio_path.stat().st_size > 0):
                        logger.error("Файл аудио не был создан или пустой после всех попыток")
                        
                        # Попробуем диагностическую команду для получения информации о видео
                        logger.info("Проверяю доступность видео...")
                        await self.check_video_availability(youtube_url)
                        
                        return None
            
            logger.info(f"Аудио успешно скачано: {audio_path}")
            return audio_path
                
        except Exception as e:
            logger.error(f"Ошибка при скачивании аудио: {e}")
            return None
    
    def get_audio_duration(self, audio_file: Path) -> float:
        """Получает длительность аудио файла в секундах"""
        try:
            import subprocess
            
            # Сначала пробуем ffprobe
            try:
                cmd = [
                    "ffprobe",
                    "-v", "quiet",
                    "-show_entries", "format=duration",
                    "-of", "csv=p=0",
                    str(audio_file)
                ]
                
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if result.returncode == 0:
                    duration = float(result.stdout.strip())
                    logger.info(f"Длительность аудио (ffprobe): {duration:.2f} секунд")
                    return duration
            except FileNotFoundError:
                logger.info("ffprobe не найден, пробую альтернативный метод")
            
            # Альтернативный метод через Python библиотеки
            try:
                import wave
                with wave.open(str(audio_file), 'rb') as wav_file:
                    frames = wav_file.getnframes()
                    rate = wav_file.getframerate()
                    duration = frames / float(rate)
                    logger.info(f"Длительность аудио (wave): {duration:.2f} секунд")
                    return duration
            except Exception:
                pass
            
            # Если ничего не сработало, пробуем оценить по размеру файла
            file_size = audio_file.stat().st_size
            # Примерная оценка: 128kbps MP3 ≈ 16KB/сек
            estimated_duration = file_size / (16 * 1024)
            logger.info(f"Примерная длительность аудио (по размеру): {estimated_duration:.2f} секунд")
            return estimated_duration
            
        except Exception as e:
            logger.warning(f"Ошибка при получении длительности аудио: {e}")
            return 0.0
    
    def parse_whisper_timestamp(self, line: str) -> float:
        """Извлекает время из строки Whisper (формат [02:40.000 --> 02:42.000])"""
        try:
            # Ищем паттерн [MM:SS.mmm --> MM:SS.mmm]
            match = re.search(r'\[(\d{1,2}):(\d{2})\.(\d{3})\s*-->\s*(\d{1,2}):(\d{2})\.(\d{3})\]', line)
            if match:
                start_min, start_sec, start_ms, end_min, end_sec, end_ms = match.groups()
                # Берем конечное время как прогресс
                end_time = int(end_min) * 60 + int(end_sec) + int(end_ms) / 1000.0
                return end_time
            return 0.0
        except Exception as e:
            logger.warning(f"Ошибка при парсинге времени: {e}")
            return 0.0
    
    def create_progress_bar(self, progress: float, width: int = 20) -> str:
        """Создает текстовый прогресс-бар"""
        filled = int(progress * width)
        bar = "█" * filled + "░" * (width - filled)
        return f"[{bar}] {progress * 100:.1f}%"
    
    def split_message(self, text: str, max_length: int = 4000) -> list:
        """Разбивает длинное сообщение на части"""
        if len(text) <= max_length:
            return [text]
        
        parts = []
        current_part = ""
        
        # Разбиваем по абзацам
        paragraphs = text.split('\n\n')
        
        for paragraph in paragraphs:
            # Если абзац сам по себе слишком длинный, разбиваем по предложениям
            if len(paragraph) > max_length:
                sentences = paragraph.split('. ')
                for sentence in sentences:
                    if len(current_part + sentence + '. ') <= max_length:
                        current_part += sentence + '. '
                    else:
                        if current_part:
                            parts.append(current_part.strip())
                        current_part = sentence + '. '
            else:
                # Если текущая часть + абзац помещается
                if len(current_part + paragraph + '\n\n') <= max_length:
                    current_part += paragraph + '\n\n'
                else:
                    # Сохраняем текущую часть и начинаем новую
                    if current_part:
                        parts.append(current_part.strip())
                    current_part = paragraph + '\n\n'
        
        # Добавляем последнюю часть
        if current_part:
            parts.append(current_part.strip())
        
        return parts
    
    def markdown_to_telegram_html(self, text: str) -> str:
        """Конвертирует Markdown-текст (от LLM) в Telegram HTML.
        
        Обрабатывает: заголовки (#), жирный (**), курсив (*/_), код (```/`),
        списки (- / * / 1.), горизонтальные линии (---).
        Экранирует HTML-сущности (<, >, &).
        """
        import html as html_module
        
        lines = text.split('\n')
        result_lines = []
        in_code_block = False
        
        for line in lines:
            # Обработка блоков кода (```)
            if line.strip().startswith('```'):
                if in_code_block:
                    result_lines.append('</code></pre>')
                    in_code_block = False
                else:
                    result_lines.append('<pre><code>')
                    in_code_block = True
                continue
            
            if in_code_block:
                result_lines.append(html_module.escape(line))
                continue
            
            # Экранируем HTML-сущности
            line = html_module.escape(line)
            
            # Горизонтальная линия
            if re.match(r'^-{3,}$', line.strip()) or re.match(r'^\*{3,}$', line.strip()):
                result_lines.append('—' * 20)
                continue
            
            # Заголовки: ### → <b>, ## → <b>, # → <b>  (Telegram не поддерживает <h1>)
            header_match = re.match(r'^(#{1,6})\s+(.+)$', line)
            if header_match:
                header_text = header_match.group(2).strip()
                # Обрабатываем инлайн-форматирование внутри заголовка
                header_text = self._inline_markdown_to_html(header_text)
                result_lines.append(f'\n<b>{header_text}</b>')
                continue
            
            # Инлайн-форматирование
            line = self._inline_markdown_to_html(line)
            
            result_lines.append(line)
        
        # Если блок кода не был закрыт
        if in_code_block:
            result_lines.append('</code></pre>')
        
        return '\n'.join(result_lines)
    
    def _inline_markdown_to_html(self, text: str) -> str:
        """Конвертирует инлайн-Markdown в Telegram HTML.
        
        Обрабатывает: **bold**, *italic*, __bold__, _italic_, `code`, ~~strikethrough~~
        """
        # Жирный + курсив (***text***)
        text = re.sub(r'\*{3}(.+?)\*{3}', r'<b><i>\1</i></b>', text)
        # Жирный (**text** или __text__)
        text = re.sub(r'\*{2}(.+?)\*{2}', r'<b>\1</b>', text)
        text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)
        # Курсив (*text* или _text_), но не внутри слов с подчёркиваниями
        text = re.sub(r'(?<!\w)\*(.+?)\*(?!\w)', r'<i>\1</i>', text)
        text = re.sub(r'(?<!\w)_(.+?)_(?!\w)', r'<i>\1</i>', text)
        # Зачёркнутый (~~text~~)
        text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)
        # Инлайн-код (`code`)
        text = re.sub(r'`(.+?)`', r'<code>\1</code>', text)
        # Ссылки [text](url) → просто text (Telegram HTML ссылки сложнее)
        text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
        
        return text
    
    def escape_markdown_v2(self, text: str) -> str:
        """Экранирует специальные символы для Telegram MarkdownV2
        
        Сохраняет базовое форматирование: **bold**, *italic*, `code`, ```code blocks```
        Экранирует остальные специальные символы.
        """
        # Символы, которые нужно экранировать в MarkdownV2
        # Но мы сохраняем *, `, чтобы форматирование работало
        escape_chars = ['_', '[', ']', '(', ')', '~', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
        
        result = text
        for char in escape_chars:
            result = result.replace(char, f'\\{char}')
        
        return result
    
    async def send_markdown_message(self, message, text: str, reply_to_message_id: int = None):
        """Отправляет сообщение с поддержкой Markdown
        
        Пробует отправить с Markdown, при ошибке - без форматирования.
        
        Args:
            message: Объект сообщения для ответа
            text: Текст сообщения
            reply_to_message_id: ID сообщения для ответа (опционально)
        """
        try:
            # Пробуем отправить с Markdown (старый формат, более лояльный к ошибкам)
            if reply_to_message_id:
                return await message.reply_text(text, parse_mode='Markdown', reply_to_message_id=reply_to_message_id)
            else:
                return await message.reply_text(text, parse_mode='Markdown')
        except Exception as e:
            logger.warning(f"Ошибка при отправке с Markdown: {e}, отправляю без форматирования")
            try:
                # Если Markdown не сработал, пробуем без форматирования
                if reply_to_message_id:
                    return await message.reply_text(text, reply_to_message_id=reply_to_message_id)
                else:
                    return await message.reply_text(text)
            except Exception as e2:
                logger.error(f"Ошибка при отправке сообщения: {e2}")
                raise
    
    async def send_ai_response(self, target, ai_text: str, header: str, continuation_header: str = "Продолжение",
                               chat_id: str = None):
        """Универсальный метод отправки ответа от LLM с корректным Telegram HTML.

        Конвертирует Markdown → HTML, разбивает на части, отправляет с fallback.

        Args:
            target: Telegram message объект (reply_text) или Bot объект (send_message, требует chat_id)
            ai_text: Сырой текст от LLM (может содержать Markdown)
            header: HTML-заголовок первого сообщения, например '📝 <b>Краткое содержание:</b>'
            continuation_header: Текст заголовка для продолжений
            chat_id: ID чата (обязателен когда target — Bot, а не message)
        """
        html_text = self.markdown_to_telegram_html(ai_text)
        full_message = f"{header}\n\n{html_text}"
        parts = self.split_message(full_message)

        logger.info(f"Длина ответа: {len(ai_text)} символов, частей: {len(parts)}")

        # Определяем, является ли target Bot-объектом или message-объектом
        is_bot = hasattr(target, 'send_message') and not hasattr(target, 'reply_text')

        for i, part in enumerate(parts):
            text_to_send = part if i == 0 else f"📝 <b>{continuation_header} ({i+1}/{len(parts)}):</b>\n\n{part}"
            try:
                if is_bot:
                    await target.send_message(chat_id=chat_id, text=text_to_send, parse_mode='HTML')
                else:
                    await target.reply_text(text_to_send, parse_mode='HTML')
            except Exception as e:
                logger.warning(f"Ошибка HTML parse_mode (часть {i+1}): {e}, отправляю без форматирования")
                plain = part if i == 0 else f"{continuation_header} ({i+1}/{len(parts)}):\n\n{part}"
                try:
                    if is_bot:
                        await target.send_message(chat_id=chat_id, text=plain)
                    else:
                        await target.reply_text(plain)
                except Exception as e2:
                    logger.error(f"Ошибка при отправке части {i+1}: {e2}")
    
    def is_image_url(self, url: str) -> bool:
        """Проверяет, является ли URL ссылкой на изображение"""
        try:
            parsed_url = urlparse(url)
            if not parsed_url.scheme or not parsed_url.netloc:
                return False
            
            # Проверяем расширение файла
            path = parsed_url.path.lower()
            image_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.svg']
            if any(path.endswith(ext) for ext in image_extensions):
                return True
            
            # Дополнительная проверка через HEAD запрос
            try:
                response = requests.head(url, timeout=10, allow_redirects=True)
                content_type = response.headers.get('content-type', '').lower()
                return content_type.startswith('image/')
            except:
                return False
                
        except Exception as e:
            logger.warning(f"Ошибка при проверке URL изображения: {e}")
            return False
    
    async def download_image(self, url: str) -> Optional[bytes]:
        """Скачивает изображение по URL"""
        try:
            logger.info(f"Скачиваю изображение: {url}")
            response = requests.get(url, timeout=30, allow_redirects=True)
            response.raise_for_status()
            
            # Проверяем content-type
            content_type = response.headers.get('content-type', '').lower()
            if not content_type.startswith('image/'):
                logger.error(f"URL не содержит изображение. Content-Type: {content_type}")
                return None
            
            return response.content
        except Exception as e:
            logger.error(f"Ошибка при скачивании изображения: {e}")
            return None
    
    async def get_last_image_from_chat(self, update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> Optional[bytes]:
        """Получает последнее изображение из чата"""
        try:
            # Сначала проверяем сохраненные изображения
            if chat_id in self.last_images:
                logger.info(f"Найдено сохраненное изображение для чата {chat_id}")
                return self.last_images[chat_id]
            
            # Если нет сохраненных изображений, проверяем текущее сообщение
            if update.message:
                message = update.message
                if message.photo:
                    # Получаем фото с максимальным разрешением
                    try:
                        photo = message.photo[-1]
                        file = await asyncio.wait_for(
                            context.bot.get_file(photo.file_id),
                            timeout=60.0
                        )
                        image_data = await asyncio.wait_for(
                            file.download_as_bytearray(),
                            timeout=120.0
                        )
                        logger.info(f"Найдено изображение в текущем сообщении от {message.from_user.username if message.from_user.username else 'Unknown'}")
                        return bytes(image_data)
                    except asyncio.TimeoutError:
                        logger.error(f"Таймаут при загрузке изображения для чата {chat_id}")
                        return None
                    except Exception as e:
                        logger.error(f"Ошибка при загрузке изображения для чата {chat_id}: {e}")
                        return None
                elif message.document and message.document.mime_type and message.document.mime_type.startswith('image/'):
                    # Получаем документ-изображение
                    try:
                        file = await asyncio.wait_for(
                            context.bot.get_file(message.document.file_id),
                            timeout=60.0
                        )
                        image_data = await asyncio.wait_for(
                            file.download_as_bytearray(),
                            timeout=120.0
                        )
                        logger.info(f"Найдено изображение-документ в текущем сообщении от {message.from_user.username if message.from_user.username else 'Unknown'}")
                        return bytes(image_data)
                    except asyncio.TimeoutError:
                        logger.error(f"Таймаут при загрузке изображения-документа для чата {chat_id}")
                        return None
                    except Exception as e:
                        logger.error(f"Ошибка при загрузке изображения-документа для чата {chat_id}: {e}")
                        return None
            
            # Если изображений не найдено
            logger.warning(f"В чате {chat_id} не найдено изображений")
            return None
            
        except Exception as e:
            logger.error(f"Ошибка при получении последнего изображения: {e}", exc_info=True)
            return None
    
    async def describe_image_with_ai(self, image_data: bytes):
        """Отправляет изображение в AI API для описания (Grok или OpenRouter)
        
        Returns:
            tuple: (description, generation_id) для OpenRouter
            str: description для Grok (без generation_id)
            None: в случае ошибки
        """
        try:
            # Кодируем изображение в base64
            image_base64 = base64.b64encode(image_data).decode('utf-8')
            
            # Определяем MIME тип
            mime_type = "image/jpeg"  # По умолчанию
            if image_data.startswith(b'\x89PNG'):
                mime_type = "image/png"
            elif image_data.startswith(b'GIF'):
                mime_type = "image/gif"
            elif image_data.startswith(b'RIFF') and b'WEBP' in image_data[:20]:
                mime_type = "image/webp"
            
            # Получаем конфигурацию провайдера
            api_config = self.get_api_config("describe_api")
            provider = self.config["describe_api"].get("provider", "grok")
            
            logger.info(f"Использую провайдер '{provider}' для описания изображения")
            logger.info(f"API конфигурация: {api_config}")
            
            if provider == "grok":
                # Grok не возвращает generation_id
                return await self._describe_with_grok(image_data, image_base64, mime_type, api_config)
            else:
                # Все остальные провайдеры (openrouter, openrouter_nvidia, etc.) возвращают (description, generation_id)
                return await self._describe_with_openrouter(image_data, image_base64, mime_type, api_config)
                
        except Exception as e:
            logger.error(f"Ошибка при описании изображения: {e}")
            return None
    
    async def _describe_with_grok(self, image_data: bytes, image_base64: str, mime_type: str, api_config: dict) -> Optional[str]:
        """Описание изображения через Grok API"""
        try:
            headers = {
                "Authorization": f"Bearer {api_config['key']}",
                "Content-Type": "application/json"
            }
            
            data = {
                "model": api_config["model"],
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "Опиши это изображение на русском языке. Если на нем есть персонажи, попытайся определить их. Ответ не более 2000 символов."
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime_type};base64,{image_base64}"
                                }
                            }
                        ]
                    }
                ],
                "max_tokens": 2000
            }
            
            logger.info("Отправляю изображение в Grok API")
            response = requests.post(
                api_config["url"],
                headers=headers,
                json=data,
                timeout=300
            )
            
            if response.status_code == 200:
                # Логируем сырой ответ для отладки
                raw_response = response.text
                logger.info(
                    f"Сырой ответ Grok API (длина: {len(raw_response)}): {self._single_line_log_preview(raw_response, 500)}"
                )

                try:
                    result = response.json()
                    logger.info(f"Ответ Grok API: {self._format_api_result_for_log(result)}")
                    description = result['choices'][0]['message']['content']
                    logger.info("Описание изображения успешно получено через Grok")
                    return description
                except json.JSONDecodeError as e:
                    logger.error(f"Ошибка парсинга JSON от Grok API: {e}")
                    logger.error(f"Полный сырой ответ: {self._single_line_log_preview(raw_response, 2000)}")
                    return None
                except (KeyError, IndexError) as e:
                    logger.error(f"Неожиданная структура ответа от Grok API: {e}")
                    logger.error(f"Структура ответа: {self._format_api_result_for_log(result)}")
                    return None
            else:
                logger.error(f"Ошибка Grok API: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            logger.error(f"Ошибка при описании изображения через Grok: {e}")
            return None
    
    async def _describe_with_openrouter(self, image_data: bytes, image_base64: str, mime_type: str, api_config: dict) -> Optional[str]:
        """Описание изображения через OpenRouter API"""
        try:
            headers = {
                "Authorization": f"Bearer {api_config['key']}",
                "Content-Type": "application/json"
            }
            
            data = {
                "model": api_config["model"],
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "Опиши это изображение на русском языке. Если на картинке изображен мем, попытайся понять и объяснить его. Если на нем есть персонажи, попытайся определить их. Если мемов или узнаваемых персонажей нет, то не упоминай об этом. Ответ не более 2000 символов."
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime_type};base64,{image_base64}"
                                }
                            }
                        ]
                    }
                ],
                "max_tokens": 3000
            }
            
            logger.info("Отправляю изображение в OpenRouter API")
            response = requests.post(
                api_config["url"],
                headers=headers,
                json=data,
                timeout=300
            )
            
            if response.status_code == 200:
                # Проверяем Content-Type
                content_type = response.headers.get('content-type', '')
                logger.info(f"Content-Type ответа OpenRouter API (describe): {content_type}")
                
                # Логируем сырой ответ для отладки
                raw_response = response.text
                
                # Проверяем на пустой ответ
                if not raw_response or len(raw_response.strip()) == 0:
                    logger.error("Получен пустой ответ от OpenRouter API (describe)")
                    return None
                
                logger.info(
                    f"Сырой ответ OpenRouter API (describe, длина: {len(raw_response)}): "
                    f"{self._single_line_log_preview(raw_response, 500)}"
                )

                # Проверяем, что это действительно JSON
                if 'application/json' not in content_type.lower():
                    logger.error(f"Получен не-JSON ответ от OpenRouter API. Content-Type: {content_type}")
                    logger.error(f"Полный ответ: {raw_response}")
                    return None
                
                try:
                    result = response.json()
                    logger.info(f"Ответ OpenRouter API (describe): {self._format_api_result_for_log(result)}")
                    description = result['choices'][0]['message']['content']
                    generation_id = self.get_generation_id_from_response(result)
                    logger.info("Описание изображения успешно получено через OpenRouter")
                    return (description, generation_id)
                except json.JSONDecodeError as e:
                    logger.error(f"Ошибка парсинга JSON от OpenRouter API (describe): {e}")
                    logger.error(f"Полный сырой ответ: {self._single_line_log_preview(raw_response, 2000)}")
                    return None
                except (KeyError, IndexError) as e:
                    logger.error(f"Неожиданная структура ответа от OpenRouter API (describe): {e}")
                    logger.error(f"Структура ответа: {self._format_api_result_for_log(result)}")
                    return None
            else:
                logger.error(f"Ошибка OpenRouter API: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            logger.error(f"Ошибка при описании изображения через OpenRouter: {e}")
            return None
    
    async def ask_with_openrouter(self, prompt: str, api_config: dict) -> Optional[tuple]:
        """Отправляет текстовый запрос в OpenRouter API
        
        Args:
            prompt: Текстовый запрос пользователя
            api_config: Конфигурация API
            
        Returns:
            tuple: (response_text, generation_id) или None в случае ошибки
        """
        try:
            headers = {
                "Authorization": f"Bearer {api_config['key']}",
                "Content-Type": "application/json"
            }
            
            data = {
                "model": api_config["model"],
                "messages": [
                    {
                        "role": "user",
                        "content": prompt
                    }
                ]
            }
            
            logger.info(f"Отправляю текстовый запрос в OpenRouter API (модель: {api_config['model']})")
            response = requests.post(
                api_config["url"],
                headers=headers,
                json=data,
                timeout=300
            )
            
            if response.status_code == 200:
                content_type = response.headers.get('content-type', '')
                logger.info(f"Content-Type ответа OpenRouter API (ask): {content_type}")
                
                raw_response = response.text
                
                if not raw_response or len(raw_response.strip()) == 0:
                    logger.error("Получен пустой ответ от OpenRouter API (ask)")
                    return None
                
                logger.info(
                    f"Сырой ответ OpenRouter API (ask, длина: {len(raw_response)}): "
                    f"{self._single_line_log_preview(raw_response, 500)}"
                )

                if 'application/json' not in content_type.lower():
                    logger.error(f"Получен не-JSON ответ от OpenRouter API. Content-Type: {content_type}")
                    logger.error(f"Полный ответ: {raw_response}")
                    return None
                
                try:
                    result = response.json()
                    logger.info(f"Ответ OpenRouter API (ask): {self._format_api_result_for_log(result)}")
                    response_text = result['choices'][0]['message']['content']
                    generation_id = self.get_generation_id_from_response(result)
                    logger.info("Текстовый ответ успешно получен через OpenRouter")
                    return (response_text, generation_id)
                except json.JSONDecodeError as e:
                    logger.error(f"Ошибка парсинга JSON от OpenRouter API (ask): {e}")
                    logger.error(f"Полный сырой ответ: {self._single_line_log_preview(raw_response, 2000)}")
                    return None
                except (KeyError, IndexError) as e:
                    logger.error(f"Неожиданная структура ответа от OpenRouter API (ask): {e}")
                    logger.error(f"Структура ответа: {self._format_api_result_for_log(result)}")
                    return None
            else:
                logger.error(f"Ошибка OpenRouter API: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            logger.error(f"Ошибка при отправке текстового запроса через OpenRouter: {e}", exc_info=True)
            return None
    
    def _check_api_response_error(self, result: dict):
        """Проверяет ответ API на наличие ошибок в native_finish_reason
        
        Returns:
            tuple: (has_error, error_type, should_retry)
            - has_error: bool - есть ли ошибка
            - error_type: str - тип ошибки (NO_IMAGE, RECITATION, etc.)
            - should_retry: bool - нужно ли повторить запрос
        """
        try:
            if 'choices' not in result or len(result['choices']) == 0:
                return False, None, False
            
            choice = result['choices'][0]
            native_finish_reason = choice.get('native_finish_reason', '')
            finish_reason = choice.get('finish_reason', '')
            
            # Если native_finish_reason существует и не пустой
            if native_finish_reason:
                logger.info(f"native_finish_reason: {native_finish_reason}")
                
                # NO_IMAGE - нужно повторить запрос
                if native_finish_reason == 'NO_IMAGE':
                    return True, 'NO_IMAGE', True
                
                # Другие ошибки - не повторяем запрос
                # STOP (Gemini), completed (OpenAI) - успешное завершение
                if native_finish_reason not in ['STOP', 'completed', '']:
                    return True, native_finish_reason, False
            
            # Проверяем finish_reason для других ошибок
            # stop, completed - успешное завершение
            if finish_reason and finish_reason not in ['stop', 'completed', '']:
                return True, finish_reason, False
            
            return False, None, False
            
        except Exception as e:
            logger.warning(f"Ошибка при проверке native_finish_reason: {e}")
            return False, None, False
    
    async def generate_image_with_ai(self, prompt: str, retry_count: int = 0, api_name: str = "imagegen_api"):
        """Генерирует изображение через настроенный API
        
        Args:
            prompt: Текстовый запрос для генерации
            retry_count: Счетчик попыток (используется внутри функции)
            api_name: Имя API конфигурации (например, 'imagegen_api', 'abcgen_api')
        
        Возвращает:
            - str: URL изображения, если API вернул URL
            - dict: {'data': bytes, 'format': str} если API вернул base64 данные
            - dict: {'error': str} если произошла ошибка с описанием
            - None: если произошла критическая ошибка
        """
        max_retries = 2
        
        try:
            api_config = self.get_api_config(api_name)
            
            headers = {
                "Authorization": f"Bearer {api_config['key']}",
                "Content-Type": "application/json"
            }
            
            data = {
                "model": api_config["model"],
                "messages": [
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                "modalities": ["image"]
            }
            
            attempt_msg = f" (попытка {retry_count + 1}/{max_retries + 1})" if retry_count > 0 else ""
            logger.info(f"Отправляю запрос на генерацию изображения в API с моделью {api_config['model']}{attempt_msg}")
            response = requests.post(
                api_config["url"],
                headers=headers,
                json=data,
                timeout=300
            )
            
            if response.status_code == 200:
                result = response.json()
                logger.info(f"Получен ответ от API, обрабатываю...")
                logger.info(f"Ответ API: {self._format_api_result_for_log(result)}")
                
                # Извлекаем generation_id для отслеживания стоимости
                generation_id = self.get_generation_id_from_response(result)
                
                # Проверяем наличие ошибок в ответе
                has_error, error_type, should_retry = self._check_api_response_error(result)
                
                if has_error:
                    if should_retry and retry_count < max_retries:
                        logger.warning(f"Получен {error_type}, повторяю запрос (попытка {retry_count + 2}/{max_retries + 1})...")
                        import asyncio
                        await asyncio.sleep(1)  # Небольшая задержка перед повтором
                        return await self.generate_image_with_ai(prompt, retry_count + 1, api_name)
                    else:
                        if should_retry:
                            error_msg = f"Не удалось сгенерировать изображение после {max_retries + 1} попыток (native_finish_reason: {error_type})"
                        else:
                            error_msg = f"Ошибка генерации изображения (native_finish_reason: {error_type})"
                        logger.error(error_msg)
                        # Возвращаем ошибку с generation_id для отслеживания стоимости
                        return {'error': error_msg, 'generation_id': generation_id}
                
                # Проверяем различные форматы ответа
                if 'choices' in result and len(result['choices']) > 0:
                    choice = result['choices'][0]
                    
                    # Проверяем формат с images в message
                    if 'message' in choice:
                        message = choice['message']
                        
                        # Формат: choices[0].message.images[0].image_url.url
                        if 'images' in message and isinstance(message['images'], list) and len(message['images']) > 0:
                            image_obj = message['images'][0]
                            if 'image_url' in image_obj and 'url' in image_obj['image_url']:
                                image_url = image_obj['image_url']['url']
                                
                                # Проверяем, это base64 data URL или обычный URL
                                if image_url.startswith('data:image/'):
                                    logger.info("Изображение получено в формате base64, декодирую...")
                                    # Формат: data:image/png;base64,iVBORw0KG...
                                    match = re.match(r'data:image/(\w+);base64,(.+)', image_url)
                                    if match:
                                        image_format = match.group(1)
                                        base64_data = match.group(2)
                                        image_bytes = base64.b64decode(base64_data)
                                        logger.info(f"Изображение успешно декодировано, формат: {image_format}, размер: {len(image_bytes)} байт")
                                        return {
                                            'data': image_bytes,
                                            'format': image_format,
                                            'generation_id': generation_id
                                        }
                                else:
                                    # Обычный HTTP URL
                                    logger.info("Изображение успешно сгенерировано через OpenRouter (URL)")
                                    return {'url': image_url, 'generation_id': generation_id}
                        
                        # Проверяем message.content
                        if 'content' in message:
                            content = message['content']
                            
                            # Если content - это data URL с base64
                            if isinstance(content, str) and content.startswith('data:image/'):
                                logger.info("Изображение получено в message.content в формате base64, декодирую...")
                                match = re.match(r'data:image/(\w+);base64,(.+)', content)
                                if match:
                                    image_format = match.group(1)
                                    base64_data = match.group(2)
                                    image_bytes = base64.b64decode(base64_data)
                                    logger.info(f"Изображение успешно декодировано, формат: {image_format}, размер: {len(image_bytes)} байт")
                                    return {
                                        'data': image_bytes,
                                        'format': image_format,
                                        'generation_id': generation_id
                                    }
                            
                            # Если content - это обычный HTTP URL
                            if isinstance(content, str) and (content.startswith('http://') or content.startswith('https://')):
                                logger.info("Изображение успешно сгенерировано через OpenRouter (URL в content)")
                                return {'url': content, 'generation_id': generation_id}
                            
                            # Если content - это текст с встроенным URL
                            url_match = re.search(r'(https?://[^\s]+)', content)
                            if url_match:
                                image_url = url_match.group(1)
                                logger.info("Изображение успешно сгенерировано через OpenRouter (URL извлечен из текста)")
                                return {'url': image_url, 'generation_id': generation_id}
                    
                    # Проверяем data URL для base64
                    if 'data' in result:
                        data_result = result['data']
                        if isinstance(data_result, list) and len(data_result) > 0:
                            if 'url' in data_result[0]:
                                url = data_result[0]['url']
                                if url.startswith('data:image/'):
                                    logger.info("Изображение получено в data[].url в формате base64, декодирую...")
                                    match = re.match(r'data:image/(\w+);base64,(.+)', url)
                                    if match:
                                        image_format = match.group(1)
                                        base64_data = match.group(2)
                                        image_bytes = base64.b64decode(base64_data)
                                        logger.info(f"Изображение успешно декодировано, формат: {image_format}, размер: {len(image_bytes)} байт")
                                        return {
                                            'data': image_bytes,
                                            'format': image_format,
                                            'generation_id': generation_id
                                        }
                                else:
                                    logger.info("Изображение успешно сгенерировано через OpenRouter (URL в data)")
                                    return {'url': url, 'generation_id': generation_id}
                
                logger.error(f"Неожиданный формат ответа от OpenRouter API: {self._format_api_result_for_log(result)}")
                return None
            else:
                logger.error(
                    f"Ошибка OpenRouter API: {response.status_code} - {self._truncate_http_error_body(response.text)}"
                )
                return None
                
        except Exception as e:
            logger.error(f"Ошибка при генерации изображения через OpenRouter: {e}")
            return None
    
    async def modify_image_with_ai(self, image_data: bytes, prompt: str, retry_count: int = 0, api_name: str = "imagechange_api"):
        """Изменяет изображение через настроенный API
        
        Args:
            image_data: Байты исходного изображения
            prompt: Текстовый запрос для изменения изображения
            retry_count: Счетчик попыток (используется внутри функции)
            api_name: Имя API конфигурации (например, 'imagechange_api', 'changelast_api')
        
        Возвращает:
            - str: URL изображения, если API вернул URL
            - dict: {'data': bytes, 'format': str} если API вернул base64 данные
            - dict: {'error': str} если произошла ошибка с описанием
            - None: если произошла критическая ошибка
        """
        max_retries = 2
        
        try:
            api_config = self.get_api_config(api_name)
            
            # Кодируем исходное изображение в base64
            image_base64 = base64.b64encode(image_data).decode('utf-8')
            
            # Определяем MIME тип
            mime_type = "image/jpeg"  # По умолчанию
            if image_data.startswith(b'\x89PNG'):
                mime_type = "image/png"
            elif image_data.startswith(b'GIF'):
                mime_type = "image/gif"
            elif image_data.startswith(b'RIFF') and b'WEBP' in image_data[:20]:
                mime_type = "image/webp"
            
            headers = {
                "Authorization": f"Bearer {api_config['key']}",
                "Content-Type": "application/json"
            }
            
            data = {
                "model": api_config["model"],
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": prompt
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime_type};base64,{image_base64}"
                                }
                            }
                        ]
                    }
                ],
                "modalities": ["image"]
            }
            
            attempt_msg = f" (попытка {retry_count + 1}/{max_retries + 1})" if retry_count > 0 else ""
            logger.info(f"Отправляю запрос на изменение изображения в API с моделью {api_config['model']}{attempt_msg}")
            response = requests.post(
                api_config["url"],
                headers=headers,
                json=data,
                timeout=300
            )
            
            if response.status_code == 200:
                result = response.json()
                logger.info(f"Получен ответ от API, обрабатываю...")
                logger.info(f"Ответ API: {self._format_api_result_for_log(result)}")
                
                # Извлекаем generation_id для отслеживания стоимости
                generation_id = self.get_generation_id_from_response(result)
                
                # Проверяем наличие ошибок в ответе
                has_error, error_type, should_retry = self._check_api_response_error(result)
                
                if has_error:
                    if should_retry and retry_count < max_retries:
                        logger.warning(f"Получен {error_type}, повторяю запрос (попытка {retry_count + 2}/{max_retries + 1})...")
                        import asyncio
                        await asyncio.sleep(1)  # Небольшая задержка перед повтором
                        return await self.modify_image_with_ai(image_data, prompt, retry_count + 1, api_name)
                    else:
                        if should_retry:
                            error_msg = f"Не удалось изменить изображение после {max_retries + 1} попыток (native_finish_reason: {error_type})"
                        else:
                            error_msg = f"Ошибка изменения изображения (native_finish_reason: {error_type})"
                        logger.error(error_msg)
                        # Возвращаем ошибку с generation_id для отслеживания стоимости
                        return {'error': error_msg, 'generation_id': generation_id}
                
                # Проверяем различные форматы ответа
                if 'choices' in result and len(result['choices']) > 0:
                    choice = result['choices'][0]
                    
                    # Проверяем формат с images в message
                    if 'message' in choice:
                        message = choice['message']
                        
                        # Формат: choices[0].message.images[0].image_url.url
                        if 'images' in message and isinstance(message['images'], list) and len(message['images']) > 0:
                            image_obj = message['images'][0]
                            if 'image_url' in image_obj and 'url' in image_obj['image_url']:
                                image_url = image_obj['image_url']['url']
                                
                                # Проверяем, это base64 data URL или обычный URL
                                if image_url.startswith('data:image/'):
                                    logger.info("Изображение получено в формате base64, декодирую...")
                                    # Формат: data:image/png;base64,iVBORw0KG...
                                    match = re.match(r'data:image/(\w+);base64,(.+)', image_url)
                                    if match:
                                        image_format = match.group(1)
                                        base64_data = match.group(2)
                                        image_bytes = base64.b64decode(base64_data)
                                        logger.info(f"Изображение успешно декодировано, формат: {image_format}, размер: {len(image_bytes)} байт")
                                        return {
                                            'data': image_bytes,
                                            'format': image_format,
                                            'generation_id': generation_id
                                        }
                                else:
                                    # Обычный HTTP URL
                                    logger.info("Изображение успешно изменено через OpenRouter (URL)")
                                    return {'url': image_url, 'generation_id': generation_id}
                        
                        # Проверяем message.content
                        if 'content' in message:
                            content = message['content']
                            
                            # Если content - это data URL с base64
                            if isinstance(content, str) and content.startswith('data:image/'):
                                logger.info("Изображение получено в message.content в формате base64, декодирую...")
                                match = re.match(r'data:image/(\w+);base64,(.+)', content)
                                if match:
                                    image_format = match.group(1)
                                    base64_data = match.group(2)
                                    image_bytes = base64.b64decode(base64_data)
                                    logger.info(f"Изображение успешно декодировано, формат: {image_format}, размер: {len(image_bytes)} байт")
                                    return {
                                        'data': image_bytes,
                                        'format': image_format,
                                        'generation_id': generation_id
                                    }
                            
                            # Если content - это обычный HTTP URL
                            if isinstance(content, str) and (content.startswith('http://') or content.startswith('https://')):
                                logger.info("Изображение успешно изменено через OpenRouter (URL в content)")
                                return {'url': content, 'generation_id': generation_id}
                            
                            # Если content - это текст с встроенным URL
                            url_match = re.search(r'(https?://[^\s]+)', content)
                            if url_match:
                                image_url = url_match.group(1)
                                logger.info("Изображение успешно изменено через OpenRouter (URL извлечен из текста)")
                                return {'url': image_url, 'generation_id': generation_id}
                    
                    # Проверяем data URL для base64
                    if 'data' in result:
                        data_result = result['data']
                        if isinstance(data_result, list) and len(data_result) > 0:
                            if 'url' in data_result[0]:
                                url = data_result[0]['url']
                                if url.startswith('data:image/'):
                                    logger.info("Изображение получено в data[].url в формате base64, декодирую...")
                                    match = re.match(r'data:image/(\w+);base64,(.+)', url)
                                    if match:
                                        image_format = match.group(1)
                                        base64_data = match.group(2)
                                        image_bytes = base64.b64decode(base64_data)
                                        logger.info(f"Изображение успешно декодировано, формат: {image_format}, размер: {len(image_bytes)} байт")
                                        return {
                                            'data': image_bytes,
                                            'format': image_format,
                                            'generation_id': generation_id
                                        }
                                else:
                                    logger.info("Изображение успешно изменено через OpenRouter (URL в data)")
                                    return {'url': url, 'generation_id': generation_id}
                
                logger.error(f"Неожиданный формат ответа от OpenRouter API: {self._format_api_result_for_log(result)}")
                return None
            else:
                logger.error(
                    f"Ошибка OpenRouter API: {response.status_code} - {self._truncate_http_error_body(response.text)}"
                )
                return None
                
        except Exception as e:
            logger.error(f"Ошибка при изменении изображения через OpenRouter: {e}")
            return None
    
    async def process_multiple_images_with_ai(self, images_list: list, prompt: str, api_name: str = "mergeimage_api"):
        """Обрабатывает несколько изображений через настроенный API
        
        Args:
            images_list: Список байтов изображений
            prompt: Текстовый запрос для обработки изображений
            api_name: Имя API конфигурации (например, 'mergeimage_api')
        
        Возвращает:
            - str: URL изображения, если API вернул URL
            - dict: {'data': bytes, 'format': str} если API вернул base64 данные
            - dict: {'description': str} если API вернул текстовое описание
            - dict: {'error': str} если произошла ошибка с описанием
            - None: если произошла критическая ошибка
        """
        try:
            api_config = self.get_api_config(api_name)
            
            headers = {
                "Authorization": f"Bearer {api_config['key']}",
                "Content-Type": "application/json"
            }
            
            # Подготавливаем content с текстом и всеми изображениями
            content_parts = [
                {
                    "type": "text",
                    "text": prompt
                }
            ]
            
            # Добавляем все изображения
            for idx, image_data in enumerate(images_list):
                # Кодируем изображение в base64
                image_base64 = base64.b64encode(image_data).decode('utf-8')
                
                # Определяем MIME тип
                mime_type = "image/jpeg"  # По умолчанию
                if image_data.startswith(b'\x89PNG'):
                    mime_type = "image/png"
                elif image_data.startswith(b'GIF'):
                    mime_type = "image/gif"
                elif image_data.startswith(b'RIFF') and b'WEBP' in image_data[:20]:
                    mime_type = "image/webp"
                
                content_parts.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime_type};base64,{image_base64}"
                    }
                })
                logger.info(f"Добавлено изображение {idx + 1}/{len(images_list)} ({mime_type})")
            
            data = {
                "model": api_config["model"],
                "messages": [
                    {
                        "role": "user",
                        "content": content_parts
                    }
                ],
                "modalities": ["image"]  # Запрашиваем генерацию изображения
            }
            
            logger.info(f"Отправляю запрос на обработку {len(images_list)} изображений в API с моделью {api_config['model']}")
            response = requests.post(
                api_config["url"],
                headers=headers,
                json=data,
                timeout=300
            )
            
            if response.status_code == 200:
                result = response.json()
                logger.info(f"Получен ответ от API, обрабатываю...")
                logger.info(f"Ответ API (mergeimage): {self._format_api_result_for_log(result)}")
                
                # Извлекаем generation_id для отслеживания стоимости
                generation_id = self.get_generation_id_from_response(result)
                
                # Проверяем наличие ошибок в ответе
                has_error, error_type, should_retry = self._check_api_response_error(result)
                
                if has_error:
                    error_msg = f"Ошибка обработки изображений (native_finish_reason: {error_type})"
                    logger.error(error_msg)
                    # Возвращаем ошибку с generation_id для отслеживания стоимости
                    return {'error': error_msg, 'generation_id': generation_id}
                
                # Проверяем различные форматы ответа
                if 'choices' in result and len(result['choices']) > 0:
                    choice = result['choices'][0]
                    
                    # Проверяем формат с images в message
                    if 'message' in choice:
                        message = choice['message']
                        
                        # Формат: choices[0].message.images[0].image_url.url (сгенерированное изображение)
                        if 'images' in message and isinstance(message['images'], list) and len(message['images']) > 0:
                            image_obj = message['images'][0]
                            if 'image_url' in image_obj and 'url' in image_obj['image_url']:
                                image_url = image_obj['image_url']['url']
                                
                                # Проверяем, это base64 data URL или обычный URL
                                if image_url.startswith('data:image/'):
                                    logger.info("Изображение получено в формате base64, декодирую...")
                                    match = re.match(r'data:image/(\w+);base64,(.+)', image_url)
                                    if match:
                                        image_format = match.group(1)
                                        base64_data = match.group(2)
                                        image_bytes = base64.b64decode(base64_data)
                                        logger.info(f"Изображение успешно декодировано, формат: {image_format}, размер: {len(image_bytes)} байт")
                                        return {
                                            'data': image_bytes,
                                            'format': image_format,
                                            'generation_id': generation_id
                                        }
                                else:
                                    # Обычный HTTP URL
                                    logger.info("Изображение успешно обработано через OpenRouter (URL)")
                                    return {'url': image_url, 'generation_id': generation_id}
                        
                        # Проверяем message.content (текстовый ответ или base64)
                        if 'content' in message:
                            content = message['content']
                            
                            # Если content - это data URL с base64
                            if isinstance(content, str) and content.startswith('data:image/'):
                                logger.info("Изображение получено в message.content в формате base64, декодирую...")
                                match = re.match(r'data:image/(\w+);base64,(.+)', content)
                                if match:
                                    image_format = match.group(1)
                                    base64_data = match.group(2)
                                    image_bytes = base64.b64decode(base64_data)
                                    logger.info(f"Изображение успешно декодировано, формат: {image_format}, размер: {len(image_bytes)} байт")
                                    return {
                                        'data': image_bytes,
                                        'format': image_format,
                                        'generation_id': generation_id
                                    }
                            
                            # Если content - это обычный HTTP URL
                            if isinstance(content, str) and (content.startswith('http://') or content.startswith('https://')):
                                logger.info("Изображение успешно обработано через OpenRouter (URL в content)")
                                return {'url': content, 'generation_id': generation_id}
                            
                            # Если content - это текст с встроенным URL
                            url_match = re.search(r'(https?://[^\s]+)', content)
                            if url_match:
                                image_url = url_match.group(1)
                                logger.info("Изображение успешно обработано через OpenRouter (URL извлечен из текста)")
                                return {'url': image_url, 'generation_id': generation_id}
                            
                            # Если это просто текстовое описание/ответ
                            if isinstance(content, str) and len(content) > 0:
                                logger.info("Получен текстовый ответ от API")
                                return {'description': content, 'generation_id': generation_id}
                
                logger.error(
                    f"Неожиданный формат ответа от OpenRouter API (mergeimage): {self._format_api_result_for_log(result)}"
                )
                return {'error': 'Неожиданный формат ответа от API'}
            else:
                logger.error(
                    f"Ошибка OpenRouter API: {response.status_code} - {self._truncate_http_error_body(response.text)}"
                )
                return {'error': f'Ошибка API: {response.status_code}'}
                
        except Exception as e:
            logger.error(f"Ошибка при обработке нескольких изображений через OpenRouter: {e}")
            return {'error': str(e)}
    
    async def transcribe_audio_with_progress(self, audio_file: Path, progress_message) -> Optional[str]:
        """Транскрибирует аудио с отображением прогресса"""
        try:
            # Проверяем, что файл существует и не пустой
            if not audio_file.exists() or audio_file.stat().st_size == 0:
                logger.error("Аудио файл не существует или пустой")
                return None
            
            # Получаем длительность аудио
            total_duration = self.get_audio_duration(audio_file)
            
            # Команда whisper
            cmd = [
                str(Path(self.config["whisper_path"]) / "whisper.exe"),
                str(audio_file),
                "--model", "turbo"
            ]
            
            logger.info(f"Выполняю команду: {' '.join(cmd)}")
            
            # Выполняем команду с установкой кодировки UTF-8
            env = os.environ.copy()
            env['PYTHONIOENCODING'] = 'utf-8'
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.temp_dir),
                env=env
            )
            
            # Читаем вывод построчно для отслеживания прогресса
            transcript_lines = []
            last_progress = 0.0
            
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                
                line_str = line.decode('utf-8', errors='replace').strip()
                if line_str:
                    transcript_lines.append(line_str)
                    
                    # Если есть длительность и строка содержит таймкод, обновляем прогресс
                    if total_duration > 0 and '[' in line_str and '-->' in line_str:
                        current_time = self.parse_whisper_timestamp(line_str)
                        if current_time > 0:
                            progress = min(current_time / total_duration, 1.0)
                            
                            # Обновляем прогресс только если он изменился значительно
                            if progress - last_progress > 0.05:  # Обновляем каждые 5%
                                last_progress = progress
                                progress_bar = self.create_progress_bar(progress)
                                status_text = f"🎤 Создаю транскрипт... {progress_bar}"
                                await self.update_status(progress_message, status_text)
                                logger.info(f"Прогресс транскрипции: {progress:.1f}%")
            
            # Ждем завершения процесса
            await process.wait()
            
            # Читаем stderr для ошибок
            stderr_data = await process.stderr.read()
            if stderr_data:
                stderr_text = stderr_data.decode('utf-8', errors='replace')
                logger.info(f"Whisper stderr: {stderr_text}")
            
            if process.returncode != 0:
                logger.error(f"Ошибка whisper: {stderr_text if 'stderr_text' in locals() else 'Неизвестная ошибка'}")
                return None
            
            # Проверяем, что Whisper не пропустил файл
            if "Skipping" in ' '.join(transcript_lines) or "Failed to load audio" in (stderr_text if 'stderr_text' in locals() else ""):
                logger.error("Whisper не смог обработать аудио файл")
                return None
            
            # Ищем созданный файл с транскрипцией
            transcript_file = audio_file.with_suffix('.txt')
            
            # Если файл не найден, ищем все .txt файлы в папке
            if not transcript_file.exists():
                txt_files = list(self.temp_dir.glob("*.txt"))
                if txt_files:
                    transcript_file = txt_files[0]
                    logger.info(f"Найден файл транскрипции: {transcript_file}")
            
            if transcript_file.exists():
                try:
                    # Пробуем разные кодировки
                    for encoding in ['utf-8', 'utf-8-sig', 'cp1251', 'latin1']:
                        try:
                            with open(transcript_file, 'r', encoding=encoding) as f:
                                transcript = f.read()
                            logger.info(f"Транскрипция успешно создана (кодировка: {encoding})")
                            return transcript
                        except UnicodeDecodeError:
                            continue
                    
                    # Если все кодировки не подошли, читаем как байты и декодируем с ошибками
                    with open(transcript_file, 'rb') as f:
                        content = f.read()
                    transcript = content.decode('utf-8', errors='replace')
                    logger.info("Транскрипция создана с заменой нечитаемых символов")
                    return transcript
                    
                except Exception as e:
                    logger.error(f"Ошибка при чтении файла транскрипции: {e}")
                    return None
            else:
                logger.error("Файл транскрипции не был создан")
                files_in_dir = list(self.temp_dir.glob("*"))
                logger.error(f"Файлы в папке: {[f.name for f in files_in_dir]}")
                return None
                
        except Exception as e:
            logger.error(f"Ошибка при транскрипции: {e}")
            return None
    
    async def transcribe_audio(self, audio_file: Path) -> Optional[str]:
        """Транскрибирует аудио используя OpenAI Whisper (без прогресса)"""
        try:
            # Проверяем, что файл существует и не пустой
            if not audio_file.exists() or audio_file.stat().st_size == 0:
                logger.error("Аудио файл не существует или пустой")
                return None
            
            # Команда whisper
            cmd = [
                str(Path(self.config["whisper_path"]) / "whisper.exe"),
                str(audio_file),
                "--model", "turbo"
            ]
            
            logger.info(f"Выполняю команду: {' '.join(cmd)}")
            
            # Выполняем команду с установкой кодировки UTF-8
            env = os.environ.copy()
            env['PYTHONIOENCODING'] = 'utf-8'
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.temp_dir),
                env=env
            )
            
            stdout, stderr = await process.communicate()
            
            # Логируем вывод для отладки
            if stdout:
                logger.info(f"Whisper stdout: {stdout.decode()}")
            if stderr:
                logger.info(f"Whisper stderr: {stderr.decode()}")
            
            if process.returncode != 0:
                logger.error(f"Ошибка whisper: {stderr.decode()}")
                return None
            
            # Проверяем, что Whisper не пропустил файл
            if "Skipping" in stdout.decode() or "Failed to load audio" in stderr.decode():
                logger.error("Whisper не смог обработать аудио файл")
                return None
            
            # Ищем созданный файл с транскрипцией
            # Whisper создает файл с расширением .txt в той же папке
            transcript_file = audio_file.with_suffix('.txt')
            
            # Если файл не найден, ищем все .txt файлы в папке
            if not transcript_file.exists():
                txt_files = list(self.temp_dir.glob("*.txt"))
                if txt_files:
                    transcript_file = txt_files[0]  # Берем первый найденный .txt файл
                    logger.info(f"Найден файл транскрипции: {transcript_file}")
            
            if transcript_file.exists():
                try:
                    # Пробуем разные кодировки
                    for encoding in ['utf-8', 'utf-8-sig', 'cp1251', 'latin1']:
                        try:
                            with open(transcript_file, 'r', encoding=encoding) as f:
                                transcript = f.read()
                            logger.info(f"Транскрипция успешно создана (кодировка: {encoding})")
                            return transcript
                        except UnicodeDecodeError:
                            continue
                    
                    # Если все кодировки не подошли, читаем как байты и декодируем с ошибками
                    with open(transcript_file, 'rb') as f:
                        content = f.read()
                    transcript = content.decode('utf-8', errors='replace')
                    logger.info("Транскрипция создана с заменой нечитаемых символов")
                    return transcript
                    
                except Exception as e:
                    logger.error(f"Ошибка при чтении файла транскрипции: {e}")
                    return None
            else:
                logger.error("Файл транскрипции не был создан")
                # Выводим содержимое папки для отладки
                files_in_dir = list(self.temp_dir.glob("*"))
                logger.error(f"Файлы в папке: {[f.name for f in files_in_dir]}")
                return None
                
        except Exception as e:
            logger.error(f"Ошибка при транскрипции: {e}")
            return None
    
    async def create_summary(self, transcript: str) -> Optional[str]:
        """Создает summary используя настроенный API"""
        try:
            api_config = self.get_api_config("summary_api")
            url = api_config["url"]
            headers = {
                "Authorization": f"Bearer {api_config['key']}",
                "Content-Type": "application/json"
            }
            
            data = {
                "model": api_config["model"],
                "messages": [
                    {
                        "role": "system",
                        "content": "Ты - помощник, который создает развернутые и информативные summary для YouTube видео. Создай структурированное краткое содержание на русском языке, выделив основные темы и ключевые моменты. Опускай рекламу, если обнаружишь ее в содержании. Если в транскрипте на твой взгляд есть ошибки или неточности, исправляй их, но не упоминай об этом в summary."
                    },
                    {
                        "role": "user",
                        "content": f"Создай краткое содержание следующего видео:\n\n{transcript}"
                    }
                ],
                "temperature": 0.7
            }
            
            logger.info(f"Отправляю запрос к API (модель: {api_config['model']})")
            
            response = requests.post(url, headers=headers, json=data, timeout=300)  # 5 минут
            response.raise_for_status()
            
            result = response.json()
            summary = result["choices"][0]["message"]["content"]
            
            logger.info("Summary успешно создан")
            return summary
            
        except Exception as e:
            logger.error(f"Ошибка при создании summary: {e}")
            return None
    
    async def create_summary_with_gemini(self, youtube_url: str, api_config: dict) -> Optional[str]:
        """Создаёт summary YouTube-видео напрямую через Google Gemini API.
        
        Gemini принимает YouTube URL через file_data.file_uri и самостоятельно
        анализирует аудио и видеоряд — без необходимости скачивать и транскрибировать.
        
        Args:
            youtube_url: Ссылка на YouTube-видео
            api_config: Конфигурация Google Gemini API (url, key, model)
        
        Returns:
            str: Текст summary или None в случае ошибки
        """
        try:
            model = api_config["model"]
            api_key = api_config["key"]
            base_url = api_config.get("url", "https://generativelanguage.googleapis.com/v1beta")
            
            url = f"{base_url}/models/{model}:generateContent"
            
            headers = {
                "Content-Type": "application/json",
                "x-goog-api-key": api_key
            }
            
            data = {
                "contents": [
                    {
                        "parts": [
                            {
                                "file_data": {
                                    "file_uri": youtube_url
                                }
                            },
                            {
                                "text": (
                                    "Ты — помощник, который создаёт развёрнутые и информативные summary "
                                    "для YouTube-видео. Создай структурированное краткое содержание на "
                                    "русском языке, выделив основные темы и ключевые моменты. "
                                    "Опускай рекламу, если обнаружишь её в содержании. "
                                    "Если в речи есть ошибки или неточности, исправляй их, "
                                    "но не упоминай об этом в summary."
                                )
                            }
                        ]
                    }
                ],
                "generationConfig": {
                    "temperature": 0.7
                }
            }
            
            logger.info(f"Отправляю YouTube URL в Google Gemini API (модель: {model}): {youtube_url}")
            response = requests.post(url, headers=headers, json=data, timeout=600)  # 10 минут — видео может быть длинным
            
            if response.status_code == 200:
                result = response.json()
                logger.info(f"Ответ Gemini API получен")
                
                candidates = result.get("candidates", [])
                if not candidates:
                    # Проверяем promptFeedback на блокировку
                    feedback = result.get("promptFeedback", {})
                    block_reason = feedback.get("blockReason", "")
                    if block_reason:
                        logger.error(f"Запрос заблокирован Gemini: {block_reason}")
                        return None
                    logger.error(f"Пустой ответ от Gemini API: {result}")
                    return None
                
                parts = candidates[0].get("content", {}).get("parts", [])
                text_parts = [p["text"] for p in parts if "text" in p]
                summary = "\n".join(text_parts)
                
                if summary:
                    logger.info(f"Summary успешно создан через Google Gemini ({len(summary)} символов)")
                    return summary
                else:
                    logger.error("Gemini вернул ответ без текста")
                    return None
            else:
                error_text = response.text
                logger.error(f"Ошибка Google Gemini API: {response.status_code} - {error_text}")
                return None
                
        except requests.exceptions.Timeout:
            logger.error("Таймаут при запросе к Google Gemini API (видео слишком длинное?)")
            return None
        except Exception as e:
            logger.error(f"Ошибка при создании summary через Gemini: {e}")
            return None
    
    async def cleanup_temp_files(self):
        """Очищает временные файлы"""
        try:
            for file_path in self.temp_dir.glob("*"):
                if file_path.is_file():
                    file_path.unlink()
            logger.info("Временные файлы очищены")
        except Exception as e:
            logger.error(f"Ошибка при очистке временных файлов: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # TOURNAMENT SYSTEM
    # ─────────────────────────────────────────────────────────────────────────

    def _get_current_tournament_id(self) -> str:
        """Возвращает ID текущего/последнего активного турнира (YYYY-MM-DD понедельника)."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT tournament_id FROM tournaments WHERE status IN ('registration','active') ORDER BY id DESC LIMIT 1"
        )
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

    def _get_tournament_status(self, tournament_id: str) -> str:
        """Возвращает статус турнира или None если турнира нет."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM tournaments WHERE tournament_id = ?", (tournament_id,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

    async def open_tournament_registration(self, context):
        """Открывает регистрацию на турнир (каждый понедельник в 13:00 KSK)."""
        try:
            from datetime import date
            today = date.today()
            # Нормализуем до понедельника текущей недели
            monday = today - __import__('datetime').timedelta(days=today.weekday())
            tournament_id = monday.strftime('%Y-%m-%d')

            channel_id = self.config.get('tournament_channel_id')
            if not channel_id:
                logger.error("tournament_channel_id не задан в config.json")
                return

            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            # Check existing tournament state:
            # - No row         → create new 'registration' row
            # - 'completed'    → reset to 'registration' (covers debug re-runs and same-week restarts)
            # - 'registration' → already open, re-send announcement but don't touch DB
            # - 'active'       → tournament in progress, skip silently
            cursor.execute("SELECT status FROM tournaments WHERE tournament_id = ?", (tournament_id,))
            existing = cursor.fetchone()
            existing_status = existing[0] if existing else None

            if existing_status == 'active':
                logger.warning(f"Турнир {tournament_id} уже активен — пропускаю открытие регистрации")
                conn.close()
                return
            elif existing_status == 'completed':
                # Reset so participants can register again (debug re-run or rare re-use of a week slot)
                cursor.execute(
                    "UPDATE tournaments SET status='registration', bracket_json=NULL, completed_at=NULL, created_at=? WHERE tournament_id=?",
                    (datetime.now().isoformat(), tournament_id)
                )
                # Clear stale registrations from the previous run
                cursor.execute("DELETE FROM tournament_registrations WHERE tournament_id=?", (tournament_id,))
                logger.info(f"Турнир {tournament_id} сброшен в 'registration' (повторный запуск)")
            elif existing_status is None:
                cursor.execute(
                    "INSERT INTO tournaments (tournament_id, status, created_at) VALUES (?, 'registration', ?)",
                    (tournament_id, datetime.now().isoformat())
                )
            # else: existing_status == 'registration' — already open, just re-send the announcement

            conn.commit()
            conn.close()

            # Читаем время и день окончания регистрации из конфига
            _day_name_ru = {
                "sunday": "воскресенье", "вс": "воскресенье", "воскресенье": "воскресенье",
                "monday": "понедельник", "пн": "понедельник", "понедельник": "понедельник",
                "tuesday": "вторник",    "вт": "вторник",    "вторник": "вторник",
                "wednesday": "среду",    "ср": "среду",      "среда": "среду",
                "thursday": "четверг",   "чт": "четверг",    "четверг": "четверг",
                "friday": "пятницу",     "пт": "пятницу",    "пятница": "пятницу",
                "saturday": "субботу",   "сб": "субботу",    "суббота": "субботу",
            }
            _cron_day_ru = ["воскресенье", "понедельник", "вторник", "среду", "четверг", "пятницу", "субботу"]
            raw_day  = self.config.get("tournament_start_day", "sunday")
            raw_time = self.config.get("tournament_start_time", "13:00")
            if str(raw_day).isdigit():
                close_day_str = _cron_day_ru[int(raw_day) % 7]
            else:
                close_day_str = _day_name_ru.get(str(raw_day).lower().strip(), str(raw_day))

            text = (
                "⚔️ <b>ТУРНИР БОЙЦОВ — РЕГИСТРАЦИЯ ОТКРЫТА!</b>\n\n"
                f"🗓 Неделя: <b>{tournament_id}</b>\n\n"
                "Напишите боту в <b>личные сообщения</b>:\n"
                "<code>/reg Имя вашего бойца</code>\n\n"
                "👾 Можно выбрать любого реального или выдуманного персонажа!\n"
                "⛔ Персонажи из банлиста не допускаются. <code>/banlist</code>\n\n"
                f"⏰ Регистрация закрывается в <b>{close_day_str} в {raw_time}</b> по красноярскому времени."
            )
            await context.bot.send_message(chat_id=channel_id, text=text, parse_mode='HTML')
            logger.info(f"Регистрация турнира {tournament_id} открыта")
        except Exception as e:
            logger.error(f"Ошибка при открытии регистрации турнира: {e}", exc_info=True)

    async def reg_command(self, update, context):
        """Обработчик команды /reg — регистрация на турнир (только в личке)."""
        # Только в личных сообщениях
        if update.effective_chat.type != 'private':
            await update.message.reply_text("⚔️ Регистрация на турнир доступна только в личных сообщениях боту.")
            return

        if not context.args:
            await update.message.reply_text(
                "❌ Укажите имя бойца:\n<code>/reg Имя вашего бойца</code>",
                parse_mode='HTML'
            )
            return

        fighter_name = ' '.join(context.args).strip()
        if len(fighter_name) < 2:
            await update.message.reply_text("❌ Имя бойца слишком короткое.")
            return
        if len(fighter_name) > 100:
            await update.message.reply_text("❌ Имя бойца слишком длинное (максимум 100 символов).")
            return

        user = update.effective_user
        user_id = user.id
        username = user.username or user.first_name or str(user_id)

        # ── Спам-защита: проверяем бан до любых запросов к БД/ИИ ──────────────
        spam_state = self._reg_spam.get(user_id)
        if spam_state and spam_state.get("banned_until"):
            if datetime.now() < spam_state["banned_until"]:
                ban_until_str = spam_state["banned_until"].strftime("%H:%M")
                await update.message.reply_text(
                    f"🚫 Вы заблокированы за повторные попытки зарегистрировать занятого персонажа.\n"
                    f"Бан снимется в <b>{ban_until_str}</b>.",
                    parse_mode='HTML'
                )
                return
            else:
                # Бан истёк — сбрасываем
                self._reg_spam[user_id] = {"count": 0, "banned_until": None}

        tournament_id = self._get_current_tournament_id()
        if not tournament_id:
            await update.message.reply_text("⚔️ Регистрация сейчас закрыта. Следите за объявлениями в канале!")
            return

        status = self._get_tournament_status(tournament_id)
        if status != 'registration':
            await update.message.reply_text("⚔️ Регистрация на этот турнир уже закрыта.")
            return

        # ── Проверка дубля через ИИ ────────────────────────────────────────────
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT fighter_name FROM tournament_registrations WHERE tournament_id=? AND disqualified=0",
            (tournament_id,)
        )
        existing_fighters = [row[0] for row in cursor.fetchall()]
        conn.close()

        if existing_fighters:
            is_taken = await self._check_fighter_duplicate(fighter_name, existing_fighters)
            if is_taken:
                state = self._reg_spam.setdefault(user_id, {"count": 0, "banned_until": None})
                state["count"] += 1
                if state["count"] >= 3:
                    from datetime import timedelta
                    state["banned_until"] = datetime.now() + timedelta(hours=1)
                    ban_until_str = state["banned_until"].strftime("%H:%M")
                    await update.message.reply_text(
                        f"🚫 Персонаж <b>{fighter_name}</b> уже зарегистрирован другим участником.\n\n"
                        f"Слишком много попыток зарегистрировать занятого персонажа. "
                        f"Вы заблокированы на 1 час (до <b>{ban_until_str}</b>).",
                        parse_mode='HTML'
                    )
                else:
                    attempts_left = 3 - state["count"]
                    await update.message.reply_text(
                        f"⚠️ Персонаж <b>{fighter_name}</b> уже зарегистрирован другим участником.\n"
                        f"Выберите другого бойца.\n\n"
                        f"<i>Осталось попыток до бана: {attempts_left}</i>",
                        parse_mode='HTML'
                    )
                return

        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO tournament_registrations (tournament_id, user_id, username, fighter_name, registered_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (tournament_id, user_id, username, fighter_name, datetime.now().isoformat())
            )
            conn.commit()
            conn.close()
            # Успешная регистрация — сбрасываем счётчик спама
            self._reg_spam[user_id] = {"count": 0, "banned_until": None}
            _start_day_name_ru = {
                "sunday": "воскресенье", "вс": "воскресенье", "воскресенье": "воскресенье",
                "monday": "понедельник", "пн": "понедельник", "понедельник": "понедельник",
                "tuesday": "вторник",    "вт": "вторник",    "вторник": "вторник",
                "wednesday": "среду",    "ср": "среду",      "среда": "среду",
                "thursday": "четверг",   "чт": "четверг",    "четверг": "четверг",
                "friday": "пятницу",     "пт": "пятницу",    "пятница": "пятницу",
                "saturday": "субботу",   "сб": "субботу",    "суббота": "субботу",
            }
            _cron_day_ru_start = ["воскресенье", "понедельник", "вторник", "среду", "четверг", "пятницу", "субботу"]
            _raw_start_day  = self.config.get("tournament_start_day", "sunday")
            _raw_start_time = self.config.get("tournament_start_time", "13:00")
            if str(_raw_start_day).isdigit():
                _start_day_str = _cron_day_ru_start[int(_raw_start_day) % 7]
            else:
                _start_day_str = _start_day_name_ru.get(str(_raw_start_day).lower().strip(), str(_raw_start_day))

            await update.message.reply_text(
                f"✅ <b>Регистрация принята!</b>\n\n"
                f"⚔️ Боец: <b>{fighter_name}</b>\n"
                f"🗓 Турнир: <b>{tournament_id}</b>\n\n"
                f"Ждите начала турнира в <b>{_start_day_str} в {_raw_start_time}</b> KSK!",
                parse_mode='HTML'
            )
            logger.info(f"Пользователь {username} ({user_id}) зарегистрировал бойца '{fighter_name}' на турнир {tournament_id}")
        except sqlite3.IntegrityError:
            # Уже зарегистрирован — показываем текущего бойца
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT fighter_name FROM tournament_registrations WHERE tournament_id=? AND user_id=?",
                (tournament_id, user_id)
            )
            existing = cursor.fetchone()
            conn.close()
            existing_name = existing[0] if existing else "неизвестно"
            await update.message.reply_text(
                f"⚠️ Вы уже зарегистрированы на этот турнир с бойцом <b>{existing_name}</b>.\n"
                "Изменить выбор нельзя.",
                parse_mode='HTML'
            )
        except Exception as e:
            logger.error(f"Ошибка при регистрации на турнир: {e}", exc_info=True)
            await update.message.reply_text("❌ Ошибка при регистрации. Попробуйте позже.")

    async def _check_fighter_duplicate(self, new_fighter: str, existing_fighters: list) -> bool:
        """Проверяет через ИИ, не занят ли персонаж (нечёткое совпадение).
        Возвращает True если занято, False если свободно.
        При ошибке API возвращает False, чтобы не блокировать регистрацию."""
        try:
            api_config = self.get_api_config("reg_check_api")
            fighters_list = "\n".join(f"- {f}" for f in existing_fighters)
            prompt = (
                "Тебе дан список уже зарегистрированных персонажей/личностей и имя нового участника.\n"
                "Определи, является ли новый участник тем же персонажем, что и кто-то из списка "
                "(учитывай разные языки, написания, сокращения, возможные опечатки).\n"
                "Ответь СТРОГО одним словом: ЗАНЯТО — если такой персонаж уже есть в списке, "
                "НЕ ЗАНЯТО — если не найден.\n\n"
                f"Зарегистрированные:\n{fighters_list}\n\n"
                f"Новая заявка: {new_fighter}"
            )

            url = api_config.get("url", "https://openrouter.ai/api/v1/chat/completions")
            key = api_config.get("key", "")
            model = api_config.get("model", "mistralai/mistral-small-2503")

            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
            data = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 10,
                "temperature": 0,
            }

            response = requests.post(url, headers=headers, json=data, timeout=15)
            if response.status_code != 200:
                logger.warning(f"_check_fighter_duplicate: API вернул {response.status_code}, пропускаем проверку")
                return False
            result = response.json()

            answer = result["choices"][0]["message"]["content"].strip().upper()
            logger.info(f"_check_fighter_duplicate: '{new_fighter}' vs {existing_fighters} → {answer}")
            # "НЕ ЗАНЯТО" contains "ЗАНЯТО" as a substring, so check the negative case first
            return "ЗАНЯТО" in answer and "НЕ ЗАНЯТО" not in answer

        except Exception as e:
            logger.warning(f"_check_fighter_duplicate: ошибка ({e}), пропускаем проверку дубля")
            return False

    async def validate_participants_with_ai(self, participants: list, bans: list) -> list:
        """Проверяет участников через ИИ: банлист + реальность персонажей.
        
        Args:
            participants: list of dict {user_id, username, fighter_name}
            bans: list of str (забаненные имена)
        
        Returns:
            list of int (user_id дисквалифицированных)
        """
        try:
            api_config = self.get_api_config("tournament_api")

            bans_str = ", ".join(bans) if bans else "нет"
            participants_str = "\n".join(
                f"  user_id={p['user_id']} (@{p['username']}): {p['fighter_name']}"
                for p in participants
            )

            prompt = (
                "Ты — судья турнира. Выполни две проверки для каждого участника.\n\n"
                f"БАНЛИСТ (забаненные бойцы, победившие в прошлых турнирах):\n{bans_str}\n\n"
                f"УЧАСТНИКИ ТУРНИРА:\n{participants_str}\n\n"
                "ПРОВЕРКА 1: Есть ли среди участников бойцы из банлиста? "
                "Сравнивай нечётко — один и тот же персонаж может быть написан по-разному.\n"
                "ПРОВЕРКА 2: Все ли заявленные персонажи — реально существующие или существовавшие люди, "
                "либо широко известные выдуманные персонажи из книг, игр, фильмов, аниме, мифологии и т.д.? "
                "Если персонаж абсолютно неизвестен или является полной выдумкой без источника — дисквалифицируй.\n\n"
                "Укажи user_id всех нарушителей. Если нарушений нет — пустой список.\n"
                "В КОНЦЕ ответа обязательно добавь строку в точном формате:\n"
                "##DISQUALIFIED:user_id1,user_id2##\n"
                "Если нарушений нет: ##DISQUALIFIED:[]##\n\n"
                "Перед меткой напиши краткое объяснение своих решений на русском языке."
            )

            result = await self.ask_with_openrouter(prompt, api_config)
            if not result:
                logger.warning("ИИ не ответил на запрос валидации — пропускаем проверку")
                return []

            response_text, _ = result
            logger.info(f"Ответ ИИ на валидацию: {response_text}")

            match = re.search(r'##DISQUALIFIED:\[([^\]]*)\]##|##DISQUALIFIED:([^#\s]*)##', response_text)
            if not match:
                logger.warning("Не удалось найти метку ##DISQUALIFIED## в ответе ИИ")
                return []

            raw = (match.group(1) or match.group(2) or "").strip()
            if not raw:
                return []

            dq_ids = []
            for part in raw.split(','):
                part = part.strip()
                if part.isdigit():
                    dq_ids.append(int(part))
            return dq_ids

        except Exception as e:
            logger.error(f"Ошибка при валидации участников: {e}", exc_info=True)
            return []

    def build_bracket(self, participants: list) -> dict:
        """Строит сетку single-elimination из списка участников.
        
        Args:
            participants: list of dict {user_id, username, fighter_name}
        
        Returns:
            dict — структура турнирной сетки
        """
        import math
        import random

        n = len(participants)
        # Ближайшая степень двойки
        size = 1
        while size < n:
            size *= 2

        # Добавляем BYE-слоты ДО перемешивания, чтобы они оказались в случайных позициях сетки
        bye_slot = {"user_id": None, "username": None, "fighter_name": "BYE"}
        seeded = list(participants)
        while len(seeded) < size:
            seeded.append(dict(bye_slot))
        random.shuffle(seeded)

        num_rounds = int(math.log2(size))
        rounds = []
        match_num = 1

        # Первый раунд — из начального списка
        first_round_matches = []
        for i in range(0, size, 2):
            first_round_matches.append({
                "match_id": f"1-{match_num}",
                "player1": seeded[i],
                "player2": seeded[i + 1],
                "winner_user_id": None,
                "winner_fighter": None,
                "story": None,
                "processed": False
            })
            match_num += 1

        rounds.append({"round_number": 1, "matches": first_round_matches})

        # Остальные раунды — пустые слоты TBD
        tbd = {"user_id": None, "username": None, "fighter_name": "TBD"}
        for r in range(2, num_rounds + 1):
            count = size // (2 ** r)
            matches = []
            for i in range(count):
                matches.append({
                    "match_id": f"{r}-{i+1}",
                    "player1": dict(tbd),
                    "player2": dict(tbd),
                    "winner_user_id": None,
                    "winner_fighter": None,
                    "story": None,
                    "processed": False
                })
            rounds.append({"round_number": r, "matches": matches})

        return {
            "participants": participants,
            "rounds": rounds,
            "current_round": 0,
            "current_match": 0,
            "champion_user_id": None,
            "champion_fighter": None
        }

    def generate_bracket_image(self, bracket: dict) -> 'BytesIO':
        """Генерирует PNG-картинку турнирной сетки с помощью Pillow."""
        try:
            from PIL import Image, ImageDraw, ImageFont

            rounds = bracket["rounds"]
            num_rounds = len(rounds)
            max_slots = len(rounds[0]["matches"]) * 2  # участников в первом раунде

            BOX_W = 240
            BOX_H = 44
            H_GAP = 60      # горизонтальный промежуток между раундами
            V_PAD = 14      # вертикальный отступ внутри матча

            def slot_y(round_idx: int, slot_idx: int) -> int:
                """Y-координата центра слота (участника) в раунде."""
                slots_in_round = len(rounds[round_idx]["matches"]) * 2
                spacing = max_slots / slots_in_round
                return int((slot_idx + 0.5) * spacing * (BOX_H + V_PAD * 2))

            canvas_h = max_slots * (BOX_H + V_PAD * 2) + 60
            canvas_w = num_rounds * (BOX_W + H_GAP) + H_GAP + 20

            img = Image.new("RGB", (canvas_w, canvas_h), color=(24, 24, 32))
            draw = ImageDraw.Draw(img)

            # Font candidates — ordered by preference, all support Cyrillic.
            # Covers Linux (DejaVu/Liberation/Ubuntu/FreeSans) and Windows (Arial).
            _FONT_CANDIDATES = [
                # DejaVu — ships with most Linux distros
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/dejavu/DejaVuSans.ttf",
                # Liberation (RHEL/CentOS/Fedora)
                "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
                "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
                # Ubuntu fonts
                "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
                "/usr/share/fonts/truetype/ubuntu-font-family/Ubuntu-R.ttf",
                # FreeSans (older Debian/Ubuntu)
                "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
                # Windows fallback
                "arial.ttf",
                "C:/Windows/Fonts/arial.ttf",
            ]
            _FONT_BOLD_CANDIDATES = [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
                "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
                "/usr/share/fonts/truetype/ubuntu-font-family/Ubuntu-B.ttf",
                "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
                "arialbd.ttf",
                "C:/Windows/Fonts/arialbd.ttf",
            ]

            def _load_font(candidates, size):
                for path in candidates:
                    try:
                        return ImageFont.truetype(path, size)
                    except Exception:
                        continue
                # Last resort: Pillow's built-in default (no Cyrillic but never crashes)
                return ImageFont.load_default()

            font       = _load_font(_FONT_CANDIDATES,      14)
            font_bold  = _load_font(_FONT_BOLD_CANDIDATES, 15)
            font_small = _load_font(_FONT_CANDIDATES,      12)

            # Цвета
            C_BOX_DEFAULT = (45, 45, 60)
            C_BOX_WINNER = (30, 100, 50)
            C_BOX_LOSER = (60, 30, 30)
            C_BOX_BYE = (35, 35, 45)
            C_TEXT = (220, 220, 220)
            C_TEXT_DIM = (110, 110, 110)
            C_LINE = (80, 80, 100)
            C_HEADER = (160, 130, 255)

            round_labels = ["1/8", "1/4", "Полуфинал", "Финал", "Финал", "Финал"]

            for r_idx, rnd in enumerate(rounds):
                x = H_GAP + r_idx * (BOX_W + H_GAP)
                label = round_labels[r_idx] if r_idx < len(round_labels) else f"Раунд {r_idx+1}"
                draw.text((x + BOX_W // 2 - 30, 8), label, fill=C_HEADER, font=font_bold)

                for m_idx, match in enumerate(rnd["matches"]):
                    slot_indices = [m_idx * 2, m_idx * 2 + 1]
                    players = [match["player1"], match["player2"]]
                    winner_uid = match.get("winner_user_id")

                    for p_idx, (player, slot_i) in enumerate(zip(players, slot_indices)):
                        cy = slot_y(r_idx, slot_i) + 30  # смещение под заголовок
                        y = cy - BOX_H // 2
                        fighter = player.get("fighter_name", "TBD") or "TBD"
                        username = player.get("username")
                        is_bye = fighter == "BYE"
                        is_tbd = fighter == "TBD"

                        if is_bye or is_tbd:
                            box_color = C_BOX_BYE
                            text_color = C_TEXT_DIM
                        elif winner_uid is not None and player.get("user_id") == winner_uid:
                            box_color = C_BOX_WINNER
                            text_color = C_TEXT
                        elif winner_uid is not None:
                            box_color = C_BOX_LOSER
                            text_color = C_TEXT_DIM
                        else:
                            box_color = C_BOX_DEFAULT
                            text_color = C_TEXT

                        draw.rounded_rectangle(
                            [x, y, x + BOX_W, y + BOX_H],
                            radius=6, fill=box_color, outline=(70, 70, 90)
                        )

                        label_txt = fighter[:28] if len(fighter) <= 28 else fighter[:25] + "..."
                        draw.text((x + 8, y + 5), label_txt, fill=text_color, font=font_bold)
                        if username and not is_bye and not is_tbd:
                            draw.text((x + 8, y + 24), f"@{username}", fill=C_TEXT_DIM, font=font_small)

                    # Соединительные линии к следующему раунду
                    if r_idx < num_rounds - 1:
                        cy1 = slot_y(r_idx, slot_indices[0]) + 30
                        cy2 = slot_y(r_idx, slot_indices[1]) + 30
                        cx_right = x + BOX_W
                        cy_mid = (cy1 + cy2) // 2
                        next_x = cx_right + H_GAP

                        draw.line([(cx_right, cy1), (cx_right + H_GAP // 2, cy1)], fill=C_LINE, width=2)
                        draw.line([(cx_right, cy2), (cx_right + H_GAP // 2, cy2)], fill=C_LINE, width=2)
                        draw.line([(cx_right + H_GAP // 2, cy1), (cx_right + H_GAP // 2, cy2)], fill=C_LINE, width=2)
                        draw.line([(cx_right + H_GAP // 2, cy_mid), (next_x, cy_mid)], fill=C_LINE, width=2)

            buf = BytesIO()
            img.save(buf, format='PNG')
            buf.seek(0)
            return buf

        except Exception as e:
            logger.error(f"Ошибка при генерации изображения сетки: {e}", exc_info=True)
            return None

    def _save_bracket(self, tournament_id: str, bracket: dict):
        """Сохраняет сетку в БД."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE tournaments SET bracket_json=? WHERE tournament_id=?",
            (json.dumps(bracket, ensure_ascii=False), tournament_id)
        )
        conn.commit()
        conn.close()

    def _load_bracket(self, tournament_id: str) -> dict:
        """Загружает сетку из БД."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT bracket_json FROM tournaments WHERE tournament_id=?", (tournament_id,))
        row = cursor.fetchone()
        conn.close()
        if row and row[0]:
            return json.loads(row[0])
        return None

    async def close_and_start_tournament(self, context):
        """Закрывает регистрацию и запускает турнир (каждое воскресенье в 13:00 KSK)."""
        try:
            tournament_id = self._get_current_tournament_id()
            channel_id = self.config.get('tournament_channel_id')

            if not tournament_id or not channel_id:
                logger.info("Нет активного турнира для закрытия.")
                return

            if self._get_tournament_status(tournament_id) != 'registration':
                logger.info(f"Турнир {tournament_id} уже не в фазе регистрации.")
                return

            # Закрываем регистрацию
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE tournaments SET status='validation' WHERE tournament_id=?",
                (tournament_id,)
            )
            cursor.execute(
                "SELECT user_id, username, fighter_name FROM tournament_registrations "
                "WHERE tournament_id=? AND disqualified=0",
                (tournament_id,)
            )
            rows = cursor.fetchall()
            conn.commit()
            conn.close()

            participants = [{"user_id": r[0], "username": r[1], "fighter_name": r[2]} for r in rows]

            await context.bot.send_message(
                chat_id=channel_id,
                text=(
                    f"⚔️ <b>Регистрация на турнир {tournament_id} закрыта!</b>\n\n"
                    f"Зарегистрировалось участников: <b>{len(participants)}</b>\n\n"
                    "🔍 Проверяю участников..."
                ),
                parse_mode='HTML'
            )

            if len(participants) < 2:
                await context.bot.send_message(
                    chat_id=channel_id,
                    text="❌ <b>Турнир отменён</b> — недостаточно участников (нужно минимум 2).",
                    parse_mode='HTML'
                )
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE tournaments SET status='cancelled' WHERE tournament_id=?",
                    (tournament_id,)
                )
                conn.commit()
                conn.close()
                return

            # Получаем банлист
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT fighter_name FROM tournament_bans")
            bans = [r[0] for r in cursor.fetchall()]
            conn.close()

            # Валидация участников через ИИ
            dq_ids = await self.validate_participants_with_ai(participants, bans)

            if dq_ids:
                dq_participants = [p for p in participants if p["user_id"] in dq_ids]
                valid_participants = [p for p in participants if p["user_id"] not in dq_ids]

                # Помечаем дисквалифицированных в БД
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                for uid in dq_ids:
                    cursor.execute(
                        "UPDATE tournament_registrations SET disqualified=1 "
                        "WHERE tournament_id=? AND user_id=?",
                        (tournament_id, uid)
                    )
                conn.commit()
                conn.close()

                dq_lines = "\n".join(
                    f"• @{p['username']} → <b>{p['fighter_name']}</b>" for p in dq_participants
                )
                await context.bot.send_message(
                    chat_id=channel_id,
                    text=(
                        "🚫 <b>Дисквалифицированы:</b>\n\n"
                        f"{dq_lines}\n\n"
                        "(бан или неизвестный персонаж)"
                    ),
                    parse_mode='HTML'
                )
                participants = valid_participants

            if len(participants) < 2:
                await context.bot.send_message(
                    chat_id=channel_id,
                    text="❌ <b>Турнир отменён</b> — после дисквалификаций осталось меньше 2 участников.",
                    parse_mode='HTML'
                )
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE tournaments SET status='cancelled' WHERE tournament_id=?",
                    (tournament_id,)
                )
                conn.commit()
                conn.close()
                return

            # Строим сетку
            bracket = self.build_bracket(participants)
            self._save_bracket(tournament_id, bracket)

            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE tournaments SET status='active' WHERE tournament_id=?",
                (tournament_id,)
            )
            conn.commit()
            conn.close()

            participants_list = "\n".join(
                f"• <b>{p['fighter_name']}</b> (@{p['username']})" for p in participants
            )
            await context.bot.send_message(
                chat_id=channel_id,
                text=(
                    f"🏆 <b>ТУРНИР {tournament_id} НАЧИНАЕТСЯ!</b>\n\n"
                    f"Участники ({len(participants)}):\n{participants_list}\n\n"
                    "⚔️ Генерирую турнирную сетку..."
                ),
                parse_mode='HTML'
            )

            # Отправляем сетку
            image_buf = self.generate_bracket_image(bracket)
            if image_buf:
                await context.bot.send_photo(
                    chat_id=channel_id,
                    photo=image_buf,
                    caption=f"🏆 Турнирная сетка — {tournament_id}"
                )

            # Запускаем первый бой немедленно
            from datetime import timedelta
            context.job_queue.run_once(self.process_next_match, when=timedelta(seconds=10))

        except Exception as e:
            logger.error(f"Ошибка при закрытии/запуске турнира: {e}", exc_info=True)

    async def process_next_match(self, context):
        """Обрабатывает следующий бой в турнире."""
        try:
            tournament_id = self._get_current_tournament_id()
            channel_id = self.config.get('tournament_channel_id')
            if not tournament_id or not channel_id:
                return

            bracket = self._load_bracket(tournament_id)
            if not bracket:
                logger.error("Не удалось загрузить сетку турнира")
                return

            api_config = self.get_api_config("tournament_api")
            from datetime import timedelta

            # Ищем следующий необработанный матч
            found = False
            for r_idx, rnd in enumerate(bracket["rounds"]):
                for m_idx, match in enumerate(rnd["matches"]):
                    if match["processed"]:
                        continue

                    p1 = match["player1"]
                    p2 = match["player2"]

                    # Пропускаем если слот ещё не заполнен (TBD)
                    if p1.get("fighter_name") == "TBD" or p2.get("fighter_name") == "TBD":
                        continue

                    found = True
                    fighter1 = p1["fighter_name"]
                    fighter2 = p2["fighter_name"]
                    round_label = f"Раунд {rnd['round_number']}, матч {m_idx + 1}"

                    # BYE — автопобеда
                    was_bye = False
                    if p2.get("fighter_name") == "BYE":
                        winner = p1
                        story = f"⚡ {fighter1} получает автоматическую победу (BYE)."
                        was_bye = True
                    elif p1.get("fighter_name") == "BYE":
                        winner = p2
                        story = f"⚡ {fighter2} получает автоматическую победу (BYE)."
                        was_bye = True
                    else:
                        # Отправляем в канал анонс боя
                        await context.bot.send_message(
                            chat_id=channel_id,
                            text=(
                                f"⚔️ <b>{round_label}</b>\n\n"
                                f"🥊 <b>{fighter1}</b> (@{p1.get('username','?')})\n"
                                f"   VS\n"
                                f"🥊 <b>{fighter2}</b> (@{p2.get('username','?')})"
                            ),
                            parse_mode='HTML'
                        )

                        prompt = (
                            f"Кто победит в битве 1 на 1: {fighter1} или {fighter2}?\n\n"
                            "Богов из реального мира расценивай так же, как вымышленных персонажей: "
                            "доводы вроде «это творец всего сущего, в том числе и этого произведения, "
                            "значит он сильнее» считай невалидными и не используй их при выборе победителя.\n\n"
                            "ВАЖНО: в самом конце добавь строку точно в таком формате (без изменений):\n"
                            f"##WINNER:{fighter1}## или ##WINNER:{fighter2}##"
                        )

                        result = await self.ask_with_openrouter(prompt, api_config)
                        winner = None
                        story = None

                        if result:
                            response_text, _ = result
                            # Извлекаем победителя
                            winner_match = re.search(r'##WINNER:(.+?)##', response_text)
                            # Убираем техническую метку из текста
                            story = re.sub(r'##WINNER:.+?##', '', response_text).strip()

                            if winner_match:
                                winner_name = winner_match.group(1).strip()
                                # Определяем кто победил по близости имени
                                f1_low = fighter1.lower()
                                f2_low = fighter2.lower()
                                w_low = winner_name.lower()
                                if f1_low in w_low or w_low in f1_low:
                                    winner = p1
                                elif f2_low in w_low or w_low in f2_low:
                                    winner = p2
                                else:
                                    # Fallback — p1 побеждает
                                    winner = p1
                                    story = (story or "") + f"\n\n_(ИИ не смог однозначно определить победителя, засчитана победа {fighter1})_"
                            else:
                                # Повторная попытка не удалась — p1 побеждает
                                winner = p1
                                story = (story or f"Битва {fighter1} vs {fighter2} завершилась.") + f"\n\n_(Победа присуждена {fighter1} по умолчанию)_"
                        else:
                            winner = p1
                            story = f"Битва {fighter1} vs {fighter2}. ИИ не ответил — победа присуждена {fighter1}."

                        # Отправляем историю боя в канал
                        await self.send_ai_response(
                            context.bot,
                            story,
                            header=f"📖 <b>{round_label}: {fighter1} vs {fighter2}</b>",
                            continuation_header="Продолжение",
                            chat_id=channel_id
                        )

                    # Обновляем матч
                    match["winner_user_id"] = winner.get("user_id")
                    match["winner_fighter"] = winner.get("fighter_name")
                    match["story"] = story
                    match["processed"] = True

                    # Продвигаем победителя в следующий раунд
                    next_r_idx = r_idx + 1
                    if next_r_idx < len(bracket["rounds"]):
                        next_match_idx = m_idx // 2
                        next_match = bracket["rounds"][next_r_idx]["matches"][next_match_idx]
                        if m_idx % 2 == 0:
                            next_match["player1"] = winner
                        else:
                            next_match["player2"] = winner

                    self._save_bracket(tournament_id, bracket)

                    # Отправляем обновлённую сетку (не критично — таймаут не должен ломать турнир)
                    try:
                        image_buf = self.generate_bracket_image(bracket)
                        if image_buf:
                            await context.bot.send_photo(
                                chat_id=channel_id,
                                photo=image_buf,
                                caption=f"🏆 Обновлённая сетка ({round_label})"
                            )
                    except Exception as img_err:
                        logger.warning(f"Не удалось отправить картинку сетки: {img_err}")

                    break  # Обрабатываем по одному матчу
                if found:
                    break

            if found:
                has_remaining = any(
                    not m["processed"]
                    for rnd in bracket["rounds"]
                    for m in rnd["matches"]
                )

                if has_remaining:
                    if was_bye:
                        # Фрислот не занимает турнирное время — следующий матч немедленно
                        next_delay = timedelta(seconds=1)
                    else:
                        interval_min = int(self.config.get("tournament_match_interval_minutes", 30))
                        next_delay = timedelta(minutes=interval_min)
                    context.job_queue.run_once(self.process_next_match, when=next_delay)
                else:
                    await self.announce_tournament_winner(context, bracket, tournament_id)
            else:
                # Все матчи обработаны
                await self.announce_tournament_winner(context, bracket, tournament_id)

        except Exception as e:
            logger.error(f"Ошибка при обработке матча турнира: {e}", exc_info=True)
            # Пытаемся запланировать следующий матч даже после ошибки,
            # чтобы турнир не зависал навсегда
            try:
                from datetime import timedelta
                context.job_queue.run_once(self.process_next_match, when=timedelta(seconds=30))
                logger.info("Запланирована повторная попытка process_next_match через 30 секунд")
            except Exception:
                logger.error("Не удалось запланировать повторную попытку", exc_info=True)

    async def announce_tournament_winner(self, context, bracket: dict, tournament_id: str):
        """Объявляет победителя турнира и обновляет очки/банлист."""
        try:
            channel_id = self.config.get('tournament_channel_id')

            # Находим чемпиона — победитель последнего матча
            last_round = bracket["rounds"][-1]
            champion = None
            finalist = None

            if last_round["matches"]:
                final_match = last_round["matches"][0]
                winner_uid = final_match.get("winner_user_id")
                winner_fighter = final_match.get("winner_fighter")

                p1 = final_match["player1"]
                p2 = final_match["player2"]

                if winner_uid == p1.get("user_id"):
                    champion = p1
                    finalist = p2
                else:
                    champion = p2
                    finalist = p1

            if not champion:
                # Если финал не сыгран, берём из participants кто остался
                logger.warning("Не удалось определить чемпиона из финального матча")
                return

            # Находим полуфиналистов (проигравшие в предпоследнем раунде)
            semifinalists = []
            if len(bracket["rounds"]) >= 2:
                semi_round = bracket["rounds"][-2]
                for match in semi_round["matches"]:
                    w_uid = match.get("winner_user_id")
                    for player in [match["player1"], match["player2"]]:
                        if player.get("user_id") and player.get("user_id") != w_uid:
                            semifinalists.append(player)

            # Обновляем очки в БД
            def update_score(user_id, username, points, field):
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO tournament_scores (user_id, username, total_points, first_places, second_places, semifinal_places) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET "
                    "username=excluded.username, total_points=total_points+excluded.total_points, "
                    f"{field}={field}+1",
                    (user_id, username, points,
                     1 if field == "first_places" else 0,
                     1 if field == "second_places" else 0,
                     1 if field == "semifinal_places" else 0)
                )
                conn.commit()
                conn.close()

            if champion.get("user_id"):
                update_score(champion["user_id"], champion.get("username", ""), 5, "first_places")
            if finalist and finalist.get("user_id"):
                update_score(finalist["user_id"], finalist.get("username", ""), 3, "second_places")
            for sf in semifinalists:
                if sf.get("user_id"):
                    update_score(sf["user_id"], sf.get("username", ""), 1, "semifinal_places")

            # Добавляем все призовые места в банлист (1-е, 2-е и оба 3-х)
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            ban_entries = []
            if champion.get("fighter_name"):
                ban_entries.append(champion["fighter_name"])
            if finalist and finalist.get("fighter_name") and finalist.get("user_id"):
                ban_entries.append(finalist["fighter_name"])
            for sf in semifinalists:
                if sf.get("fighter_name") and sf.get("user_id"):
                    ban_entries.append(sf["fighter_name"])
            now_iso = datetime.now().isoformat()
            for name in ban_entries:
                cursor.execute(
                    "INSERT INTO tournament_bans (fighter_name, banned_at, tournament_id) VALUES (?, ?, ?)",
                    (name, now_iso, tournament_id)
                )
            cursor.execute(
                "UPDATE tournaments SET status='completed', completed_at=? WHERE tournament_id=?",
                (datetime.now().isoformat(), tournament_id)
            )
            conn.commit()
            conn.close()

            bracket["champion_user_id"] = champion.get("user_id")
            bracket["champion_fighter"] = champion.get("fighter_name")
            self._save_bracket(tournament_id, bracket)

            # Финальная картинка сетки
            image_buf = self.generate_bracket_image(bracket)

            semi_text = ""
            if semifinalists:
                semi_lines = "\n".join(
                    f"🥉 <b>{s.get('fighter_name')}</b> (@{s.get('username','?')}) — 1 очко"
                    for s in semifinalists
                )
                semi_text = f"\n\n{semi_lines}"

            announcement = (
                f"🏆 <b>ТУРНИР {tournament_id} ЗАВЕРШЁН!</b>\n\n"
                f"🥇 <b>ЧЕМПИОН: {champion.get('fighter_name')}</b>\n"
                f"   (@{champion.get('username','?')}) — 5 очков\n\n"
                f"🥈 Финалист: <b>{finalist.get('fighter_name') if finalist else '—'}</b>"
                f" (@{finalist.get('username','?') if finalist else '?'}) — 3 очка"
                f"{semi_text}\n\n"
                f"⛔ <b>{champion.get('fighter_name')}</b> занесён в банлист!\n\n"
                "Используйте /leaderboard чтобы посмотреть таблицу лидеров."
            )

            if image_buf:
                await context.bot.send_photo(
                    chat_id=channel_id,
                    photo=image_buf,
                    caption=announcement,
                    parse_mode='HTML'
                )
            else:
                await context.bot.send_message(
                    chat_id=channel_id,
                    text=announcement,
                    parse_mode='HTML'
                )

            logger.info(f"Турнир {tournament_id} завершён. Чемпион: {champion.get('fighter_name')}")

        except Exception as e:
            logger.error(f"Ошибка при объявлении победителя турнира: {e}", exc_info=True)

    async def reglist_command(self, update, context):
        """Пронумерованный список никнеймов зарегистрировавшихся на текущий турнир (без @ и без имён бойцов)."""
        try:
            tournament_id = self._get_current_tournament_id()
            if not tournament_id:
                await update.message.reply_text(
                    "Сейчас нет турнира в фазе регистрации или проведения — список недоступен."
                )
                return

            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT user_id, username FROM tournament_registrations "
                "WHERE tournament_id=? AND disqualified=0 ORDER BY id ASC",
                (tournament_id,)
            )
            rows = cursor.fetchall()
            conn.close()

            if not rows:
                await update.message.reply_text(
                    f"На турнир {tournament_id} пока никто не зарегистрировался."
                )
                return

            lines = []
            for i, (uid, username) in enumerate(rows, 1):
                name = (username or "").strip()
                if name.startswith("@"):
                    name = name[1:].strip()
                if not name:
                    name = f"id{uid}"
                lines.append(f"{i}. {name}")

            text = f"Участники турнира {tournament_id}:\n\n" + "\n".join(lines)
            await update.message.reply_text(text)

        except Exception as e:
            logger.error(f"Ошибка при показе списка регистраций: {e}", exc_info=True)
            await update.message.reply_text("❌ Ошибка при получении списка регистраций.")

    async def banlist_command(self, update, context):
        """Показывает список забаненных бойцов."""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT fighter_name, tournament_id, banned_at FROM tournament_bans ORDER BY banned_at DESC"
            )
            rows = cursor.fetchall()
            conn.close()

            if not rows:
                await update.message.reply_text(
                    "⛔ <b>Банлист пуст</b> — турниры ещё не проводились или победители не определены.",
                    parse_mode='HTML'
                )
                return

            lines = []
            for i, (name, tid, banned_at) in enumerate(rows, 1):
                date_str = banned_at[:10] if banned_at else "?"
                lines.append(f"{i}. <b>{name}</b> (турнир {tid}, {date_str})")

            text = "⛔ <b>Банлист:</b>\n\n" + "\n".join(lines)
            await update.message.reply_text(text, parse_mode='HTML')

        except Exception as e:
            logger.error(f"Ошибка при показе банлиста: {e}", exc_info=True)
            await update.message.reply_text("❌ Ошибка при получении банлиста.")

    async def leaderboard_command(self, update, context):
        """Показывает таблицу лидеров по турнирным очкам."""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT user_id, username, total_points, first_places, second_places, semifinal_places "
                "FROM tournament_scores ORDER BY total_points DESC, first_places DESC LIMIT 30"
            )
            rows = cursor.fetchall()
            conn.close()

            if not rows:
                await update.message.reply_text(
                    "🏆 <b>Таблица лидеров пуста</b> — турниры ещё не завершались.",
                    parse_mode='HTML'
                )
                return

            medals = {1: "🥇", 2: "🥈", 3: "🥉"}
            lines = []
            for i, (uid, username, pts, first, second, semi) in enumerate(rows, 1):
                medal = medals.get(i, f"{i}.")
                name = f"@{username}" if username else f"id{uid}"
                details = f"({first}🥇 {second}🥈 {semi}🥉)"
                lines.append(f"{medal} <b>{name}</b> — {pts} очков {details}")

            text = "🏆 <b>Таблица лидеров турниров:</b>\n\n" + "\n".join(lines)
            await update.message.reply_text(text, parse_mode='HTML')

        except Exception as e:
            logger.error(f"Ошибка при показе таблицы лидеров: {e}", exc_info=True)
            await update.message.reply_text("❌ Ошибка при получении таблицы лидеров.")

    async def resume_tournament_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /resume_tournament — возобновляет зависший турнир, планируя следующий матч."""
        if not self.is_authorized_channel(update):
            await update.message.reply_text("Доступ запрещен.")
            return

        try:
            tournament_id = self._get_current_tournament_id()
            if not tournament_id:
                await update.message.reply_text("❌ Нет активного турнира.")
                return

            status = self._get_tournament_status(tournament_id)
            if status != 'active':
                await update.message.reply_text(f"❌ Турнир {tournament_id} не в активной фазе (статус: {status}).")
                return

            bracket = self._load_bracket(tournament_id)
            if not bracket:
                await update.message.reply_text("❌ Не удалось загрузить сетку.")
                return

            has_remaining = any(
                not m["processed"]
                for rnd in bracket["rounds"]
                for m in rnd["matches"]
                if m["player1"].get("fighter_name") != "TBD" and m["player2"].get("fighter_name") != "TBD"
            )

            if not has_remaining:
                await update.message.reply_text("ℹ️ Все доступные матчи уже обработаны. Объявляю победителя...")
                await self.announce_tournament_winner(context, bracket, tournament_id)
                return

            from datetime import timedelta
            context.job_queue.run_once(self.process_next_match, when=timedelta(seconds=5))
            await update.message.reply_text(
                f"✅ Турнир <b>{tournament_id}</b> возобновлён!\n"
                "Следующий матч начнётся через 5 секунд.",
                parse_mode='HTML'
            )
            logger.info(f"Турнир {tournament_id} возобновлён вручную через /resume_tournament")

        except Exception as e:
            logger.error(f"Ошибка при возобновлении турнира: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Ошибка: {e}")

    def setup_handlers(self):
        """Настраивает обработчики команд"""
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("summary", self.summary_command))
        self.application.add_handler(CommandHandler("describe", self.describe_command))
        self.application.add_handler(CommandHandler("ask", self.ask_command))
        self.application.add_handler(CommandHandler("model", self.model_command))
        self.application.add_handler(CommandHandler("askmodel", self.askmodel_command))
        self.application.add_handler(CommandHandler("imagegen", self.imagegen_command))
        self.application.add_handler(CommandHandler("abcgen", self.abcgen_command))
        self.application.add_handler(CommandHandler("imagechange", self.imagechange_command))
        self.application.add_handler(CommandHandler("changelast", self.changelast_command))
        self.application.add_handler(CommandHandler("mergeimage", self.mergeimage_command))
        self.application.add_handler(CommandHandler("balance", self.balance_command))
        self.application.add_handler(CommandHandler("statistics", self.statistics_command))
        self.application.add_handler(CommandHandler("reload", self.reload_command))
        # Турнирные команды
        self.application.add_handler(CommandHandler("reg", self.reg_command))
        self.application.add_handler(CommandHandler("reglist", self.reglist_command))
        self.application.add_handler(CommandHandler("banlist", self.banlist_command))
        self.application.add_handler(CommandHandler("leaderboard", self.leaderboard_command))
        self.application.add_handler(CommandHandler("resume_tournament", self.resume_tournament_command))
        # Добавляем обработчик для callback кнопок (выбор модели и навигация)
        # Без pattern, чтобы не пропускать неожиданные callback_data и упростить отладку
        self.application.add_handler(CallbackQueryHandler(self.model_callback))
        # Добавляем обработчик для всех сообщений (включая изображения)
        self.application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, self.handle_message))
        
        # Добавляем обработчик ошибок
        self.application.add_error_handler(self.error_handler)
    
    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик ошибок"""
        error = context.error
        logger.error(f"Ошибка при обработке обновления: {error}", exc_info=error)
        
        # Проверяем, является ли это ошибкой загрузки файла или таймаутом
        is_timeout_error = isinstance(error, (asyncio.TimeoutError, TimeoutError))
        error_str = str(error).lower() if error else ""
        is_file_download_error = (
            is_timeout_error or
            'timeout' in error_str or
            'download' in error_str or
            'connection' in error_str
        )
        
        # Проверяем, является ли сообщение просто изображением без команды
        is_image_only = False
        if update and update.effective_message:
            has_image = (
                update.effective_message.photo or 
                (update.effective_message.document and 
                 update.effective_message.document.mime_type and 
                 update.effective_message.document.mime_type.startswith('image/'))
            )
            has_command = (
                update.effective_message.text and 
                update.effective_message.text.startswith('/')
            )
            is_image_only = has_image and not has_command
        
        # Не отправляем сообщение об ошибке, если это просто загрузка изображения с ошибкой загрузки
        if is_file_download_error and is_image_only:
            logger.info("Пропускаем отправку сообщения об ошибке для простой загрузки изображения (таймаут или ошибка загрузки)")
            return
        
        # Отправляем сообщение об ошибке пользователю, если это возможно
        if update and update.effective_message:
            try:
                await update.effective_message.reply_text(
                    "❌ Произошла ошибка при обработке запроса. Попробуйте еще раз."
                )
            except:
                pass  # Игнорируем ошибки при отправке сообщения об ошибке
    
    def run(self):
        """Запускает бота"""
        try:
            # Создаем приложение
            self.application = Application.builder().token(self.config["telegram_token"]).build()
            
            # Удаляем webhook, если он установлен
            try:
                import requests
                webhook_url = f"https://api.telegram.org/bot{self.config['telegram_token']}/deleteWebhook"
                requests.post(webhook_url, timeout=10)
                logger.info("Webhook удален")
            except Exception as e:
                logger.warning(f"Не удалось удалить webhook: {e}")
            
            # Загружаем список моделей при старте
            logger.info("Загружаю список моделей OpenRouter...")
            self.fetch_openrouter_models()
            
            # Настраиваем обработчики
            self.setup_handlers()
            
            # Добавляем периодическую задачу обновления моделей (каждые 6 часов)
            # job_queue может быть None, если не установлен пакет python-telegram-bot[job-queue]
            job_queue = self.application.job_queue
            if job_queue is not None:
                job_queue.run_repeating(
                    self.update_models_periodically,
                    interval=6 * 60 * 60,  # 6 часов в секундах
                    first=6 * 60 * 60  # Первый запуск через 6 часов (уже загрузили при старте)
                )
                logger.info("Периодическое обновление моделей настроено (каждые 6 часов)")

                # Турнирные расписания (Красноярское время UTC+7)
                import pytz
                from datetime import time as dtime
                ksk = pytz.timezone('Asia/Krasnoyarsk')

                # PTB v20+ run_daily uses CRON weekday numbering: 0=Sun, 1=Mon, …, 6=Sat
                _day_names = {
                    "sunday": 0,    "вс": 0, "воскресенье": 0,
                    "monday": 1,    "пн": 1, "понедельник": 1,
                    "tuesday": 2,   "вт": 2, "вторник": 2,
                    "wednesday": 3, "ср": 3, "среда": 3,
                    "thursday": 4,  "чт": 4, "четверг": 4,
                    "friday": 5,    "пт": 5, "пятница": 5,
                    "saturday": 6,  "сб": 6, "суббота": 6,
                }
                # Labels indexed by cron day number (0=Sun … 6=Sat)
                _weekday_labels = ["вс", "пн", "вт", "ср", "чт", "пт", "сб"]

                def _parse_tournament_time(s, default="13:00"):
                    try:
                        h, m = map(int, (s or default).split(":"))
                    except (ValueError, AttributeError):
                        logger.warning(f"Неверный формат времени турнира '{s}', использую {default}")
                        h, m = map(int, default.split(":"))
                    return dtime(h, m, 0, tzinfo=ksk)

                def _parse_tournament_day(s, default_cron):
                    if s is None:
                        return default_cron
                    # Numeric string "0".."6" — treat as cron day directly
                    if str(s).isdigit():
                        v = int(s)
                        if 0 <= v <= 6:
                            return v
                    # Named day
                    v = _day_names.get(str(s).lower().strip())
                    if v is not None:
                        return v
                    logger.warning(f"Неверный день недели '{s}', использую значение по умолчанию {default_cron}")
                    return default_cron

                reg_time   = _parse_tournament_time(self.config.get("tournament_registration_time"))
                reg_day    = _parse_tournament_day(self.config.get("tournament_registration_day"), 1)   # Mon=1
                start_time = _parse_tournament_time(self.config.get("tournament_start_time"))
                start_day  = _parse_tournament_day(self.config.get("tournament_start_day"), 0)          # Sun=0

                job_queue.run_daily(
                    self.open_tournament_registration,
                    time=reg_time,
                    days=(reg_day,)
                )
                job_queue.run_daily(
                    self.close_and_start_tournament,
                    time=start_time,
                    days=(start_day,)
                )
                logger.info(
                    f"Турнирное расписание настроено: регистрация {_weekday_labels[reg_day]} в "
                    f"{reg_time.strftime('%H:%M')} KSK, "
                    f"старт {_weekday_labels[start_day]} в {start_time.strftime('%H:%M')} KSK"
                )
            else:
                logger.warning("JobQueue не доступен. Для периодического обновления моделей установите: pip install 'python-telegram-bot[job-queue]'")
            
            # Запускаем бота
            logger.info("Запускаю Telegram бота...")
            self.application.run_polling(
                stop_signals=None,
                drop_pending_updates=True,
                allowed_updates=Update.ALL_TYPES
            )
        except KeyboardInterrupt:
            logger.info("Получен сигнал остановки")
        except Exception as e:
            logger.error(f"Ошибка при запуске бота: {e}")
            raise

def main():
    """Главная функция"""
    bot = None
    try:
        bot = TelegramWhisperBot()
        # Запускаем бота (run_polling сам управляет event loop)
        bot.run()
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
    finally:
        # Очищаем временные файлы при завершении
        if bot:
            try:
                # Синхронная очистка временных файлов
                for file_path in bot.temp_dir.glob("*"):
                    if file_path.is_file():
                        file_path.unlink()
                logger.info("Временные файлы очищены")
            except Exception as e:
                logger.error(f"Ошибка при очистке временных файлов: {e}")

if __name__ == "__main__":
    main()

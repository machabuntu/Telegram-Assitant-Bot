# Настройка Google Drive для бота

Бот сохраняет все сгенерированные изображения (`/imagegen`, `/abcgen`, `/imagechange`, `/changelast`, `/mergeimage`, `/mcg`) в двух местах:

1. Локально — папка `generated_images/` на VPS
2. Google Drive — папка **Telegram_Assistant_Gallery**

Загрузка в Drive выполняется через **Service Account** (без браузерной авторизации на сервере).

---

## Шаг 1. Google Cloud Console

1. Откройте [Google Cloud Console](https://console.cloud.google.com/).
2. Создайте проект или выберите существующий.
3. Перейдите в **APIs & Services → Library**.
4. Найдите **Google Drive API** и нажмите **Enable**.

---

## Шаг 2. Service Account

1. **APIs & Services → Credentials**.
2. **Create Credentials → Service account**.
3. Укажите имя (например, `telegram-assistant-bot`) и создайте аккаунт.
4. На вкладке **Keys** нажмите **Add key → Create new key → JSON**.
5. Скачанный файл положите в корень проекта:

   ```
   /media/games/OpenAI-Whisper/google_service_account.json
   ```

6. Запомните **email** сервис-аккаунта — он выглядит так:

   ```
   telegram-assistant-bot@your-project.iam.gserviceaccount.com
   ```

Файл `google_service_account.json` уже добавлен в `.gitignore` — не коммитьте его в git.

---

## Шаг 3. Папка на Google Drive

1. Откройте [Google Drive](https://drive.google.com/) в браузере (ваш личный аккаунт).
2. Создайте папку **Telegram_Assistant_Gallery**.
3. ПКМ по папке → **Share** / **Поделиться**.
4. Добавьте email сервис-аккаунта из шага 2.
5. Роль: **Editor** / **Редактор**.
6. Сохраните.

---

## Шаг 4. Folder ID

1. Откройте папку **Telegram_Assistant_Gallery** в браузере.
2. Скопируйте ID из URL:

   ```
   https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz
                                         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                                         это folder_id
   ```

3. Вставьте в `config.json`:

   ```json
   "google_drive": {
       "enabled": true,
       "credentials_file": "google_service_account.json",
       "folder_name": "Telegram_Assistant_Gallery",
       "folder_id": "1AbCdEfGhIjKlMnOpQrStUvWxYz"
   }
   ```

---

## Шаг 5. Зависимости и перезапуск

```bash
cd /media/games/OpenAI-Whisper
pip install -r requirements.txt
# перезапустите бота
```

---

## Шаг 6. Проверка

1. В Telegram отправьте боту команду, которая генерирует картинку (например `/imagegen тестовая картинка`).
2. Убедитесь, что файл появился:
   - локально в `generated_images/`
   - в папке **Telegram_Assistant_Gallery** на Drive
3. В логах бота должна быть строка вида:

   ```
   Uploaded to Google Drive: imagegen_...png (file_id=...)
   ```

---

## Отключение Drive

Если нужно временно сохранять только локально:

```json
"google_drive": {
    "enabled": false,
    ...
}
```

---

## Troubleshooting

| Ошибка | Причина | Решение |
|--------|---------|---------|
| `403 Forbidden` | Папка не расшарена на SA | Share → добавить email SA как Editor |
| `404 Not Found` | Неверный `folder_id` | Скопируйте ID из URL папки заново |
| `credentials not found` | Нет JSON-ключа | Положите `google_service_account.json` в корень проекта |
| `folder_id is empty` | Не задан ID в config | Заполните `google_drive.folder_id` |
| Файл локально есть, в Drive нет | Ошибка upload | Смотрите лог бота; Telegram-ответ не блокируется |

Ошибка Drive **не мешает** отправке картинки в Telegram — локальная копия всегда сохраняется.

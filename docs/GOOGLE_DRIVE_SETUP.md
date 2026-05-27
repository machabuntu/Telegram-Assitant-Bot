# Настройка Google Drive для бота (OAuth, личный Gmail)

Бот сохраняет все сгенерированные изображения (`/imagegen`, `/abcgen`, `/imagechange`, `/changelast`, `/mergeimage`, `/mcg`) в двух местах:

1. Локально — папка `generated_images/` на VPS
2. Google Drive — папка **Telegram_Assistant_Gallery** на вашем личном аккаунте

Загрузка в Drive выполняется через **OAuth 2.0** (ваш `@gmail.com`), не через Service Account.

---

## Почему не Service Account

Service Account **не имеет квоты** на Google Drive. При upload файл становится владельцем SA → ошибка `403 storageQuotaExceeded`. Для личного Gmail нужен OAuth: файлы создаются от вашего имени и считаются в вашу квоту.

---

## Часть A. Google Cloud Console

### 1. Включить Drive API

1. [Google Cloud Console](https://console.cloud.google.com/) → ваш проект
2. **APIs & Services → Library**
3. Найти **Google Drive API** → **Enable**

### 2. Google Auth platform (OAuth consent)

Google переименовал и перестроил этот раздел. Старый мастер «OAuth consent screen» с выбором **External** на первом экране больше не всегда показывается. Сейчас всё живёт в **Google Auth platform**.

**Как открыть:**

1. [Google Cloud Console](https://console.cloud.google.com/) → выберите проект
2. Меню ☰ → **Google Auth platform**  
   (если пункта нет: **APIs & Services → OAuth consent screen** — откроется тот же раздел)

---

#### Вариант A: первый раз («Google Auth platform not configured yet»)

1. Нажмите **Get started**
2. **App Information** — App name, User support email → **Next**
3. **Audience** — здесь выбор типа аудитории:
   - для личного `@gmail.com` выберите **External** (доступ любому Google-аккаунту)
   - **Internal** бывает только у Google Workspace — у обычного Gmail его нет
4. **Contact Information** — email разработчика → **Next**
5. **Finish** — согласие с User Data Policy → **Continue** → **Create**

После создания откроются вкладки/дашборды: **Branding**, **Audience**, **Data Access**, **Clients**.

---

#### Вариант B: уже настроено (видны только дашборды)

Если мастера нет — платформа уже создана. Настройки по вкладкам:

| Вкладка | Что сделать |
|---------|-------------|
| **Audience** | Статус публикации: **Publish app** → **In production** (см. ниже). В Testing refresh token живёт ~7 дней |
| **Data Access** | **Add or remove scopes** → добавить `https://www.googleapis.com/auth/drive.file` → **Save** (можно и позже — scope запросит setup-скрипт) |
| **Branding** | По желанию: имя приложения (не обязательно для личного бота) |

**Publish app:** вкладка **Audience** → **Publishing status** → **Publish app**.  
Для scope `drive.file` верификация Google **не нужна** — можно пользоваться с экраном «Unverified app» (Advanced → Continue).

---

### 3. OAuth Client (Desktop app)

**Новый путь (предпочтительно):**

1. **Google Auth platform → Clients**
2. **Create client**
3. Application type: **Desktop app**
4. Скачать JSON → сохранить как `google_oauth_client.json` в корень проекта

**Старый путь (если вкладки Clients нет):**

1. **APIs & Services → Credentials**
2. **Create Credentials → OAuth client ID**
3. Application type: **Desktop app** → скачать JSON

---

## Часть B. Первичная авторизация (на локальном ПК)

OAuth нужно пройти **один раз в браузере** — удобнее на вашем компьютере, не на headless VPS.

### 1. Подготовка

```bash
cd /path/to/OpenAI-Whisper
pip install -r requirements.txt
```

Положите в корень проекта:
- `google_oauth_client.json` (из шага A.3)
- `config.json` с секцией `google_drive` (см. ниже)

### 2. config.json

```json
"google_drive": {
    "enabled": true,
    "oauth_client_file": "google_oauth_client.json",
    "token_file": "google_drive_token.json",
    "folder_name": "Telegram_Assistant_Gallery",
    "folder_id": ""
}
```

### 3. Запуск setup-скрипта

```bash
python scripts/setup_google_drive_oauth.py
```

1. Откроется браузер → войдите в Google → **Разрешить** доступ
2. Если появится «Google hasn't verified this app» → **Advanced** → **Go to … (unsafe)** — нормально для личного использования
3. Скрипт сохранит `google_drive_token.json`
4. Создаст папку **Telegram_Assistant_Gallery** через API (если `folder_id` пустой)
5. Выведет `folder_id` — вставьте в `config.json`:

   ```json
   "folder_id": "1AbCdEfGhIjKlMnOpQrStUvWxYz"
   ```

**Важно:** scope `drive.file` даёт доступ только к папкам/файлам, **созданным приложением**. Папку, созданную вручную в Drive до setup, бот может не увидеть — используйте папку, созданную setup-скриптом.

---

## Часть C. Деплой на VPS

Скопируйте на сервер с ботом:

| Файл | Назначение |
|------|------------|
| `google_oauth_client.json` | OAuth client credentials |
| `google_drive_token.json` | Refresh token (долгоживущий) |
| `config.json` | С заполненным `folder_id` |

На VPS:

```bash
pip install -r requirements.txt
# перезапустите бота
```

Бот **сам обновляет** access token по refresh token — повторный login в браузере не нужен при рестарте.

---

## Часть D. Проверка

1. Отправьте боту `/imagegen тестовая картинка` (или `/mcg`)
2. Проверьте:
   - файл в `generated_images/` на VPS
   - файл в **Telegram_Assistant_Gallery** на [Google Drive](https://drive.google.com/)
3. В логах:

   ```
   Uploaded to Google Drive: imagegen_...png (file_id=...)
   ```

---

## Отключение Drive

```json
"google_drive": {
    "enabled": false,
    ...
}
```

Локальное сохранение продолжит работать.

---

## Troubleshooting

| Ошибка | Причина | Решение |
|--------|---------|---------|
| `token not found` | Нет `google_drive_token.json` на VPS | Запустите setup на ПК, скопируйте token на сервер |
| `invalid_grant` / refresh failed | Token отозван или app в Testing >7 дней | Повторите `setup_google_drive_oauth.py`, app → Production |
| `403 storageQuotaExceeded` | Старый Service Account | Убедитесь, что используете OAuth (не SA) |
| `404` / folder not found | Неверный `folder_id` | Перезапустите setup, обновите `folder_id` |
| `403` при upload в папку | Папка создана вручную, не приложением | Setup создаст новую папку — используйте её `folder_id` |
| Unverified app | App не верифицирован Google | Advanced → Continue (для личного бота OK) |

Ошибка Drive **не блокирует** отправку картинки в Telegram — локальная копия всегда сохраняется.

---

## Повторная авторизация

Нужна если:
- отозвали доступ в [Google Account → Third-party apps](https://myaccount.google.com/permissions)
- refresh token истёк (Testing mode)
- сменили OAuth Client ID

```bash
python scripts/setup_google_drive_oauth.py
```

Скопируйте новый `google_drive_token.json` на VPS.

---

## Безопасность

- `google_oauth_client.json` и `google_drive_token.json` — в `.gitignore`, не коммитьте
- На VPS храните файлы с ограниченными правами (`chmod 600`)
- Refresh token = полный доступ к Drive в рамках scope `drive.file`

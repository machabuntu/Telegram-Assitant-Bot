# Руководство по получению YouTube Cookies

## Проблема
Некоторые видео на YouTube требуют подтверждения возраста или имеют другие ограничения доступа. yt-dlp может не скачать такие видео без аутентификации.

## Решение: Использование Cookies

### Способ 1: Автоматическое извлечение cookies из браузера

1. **Установите расширение для браузера:**
   - Chrome: "Get cookies.txt" или "cookies.txt"
   - Firefox: "cookies.txt"

2. **Экспортируйте cookies:**
   - Перейдите на youtube.com
   - Войдите в свой аккаунт
   - Используйте расширение для экспорта cookies в файл `cookies.txt`

3. **Поместите файл в папку с ботом:**
   - Скопируйте `cookies.txt` в папку `F:\OpenAI-Whisper\`

4. **Обновите конфигурацию:**
   ```json
   {
       "youtube_cookies": "cookies.txt"
   }
   ```

### Способ 2: Ручное создание файла cookies

1. **Создайте файл `cookies.txt` в папке с ботом**

2. **Добавьте в файл следующие строки:**
   ```
   # Netscape HTTP Cookie File
   .youtube.com	TRUE	/	FALSE	0	VISITOR_INFO1_LIVE	YOUR_VISITOR_INFO
   .youtube.com	TRUE	/	FALSE	0	YSC	YOUR_YSC_VALUE
   .youtube.com	TRUE	/	FALSE	0	PREF	YOUR_PREF_VALUE
   ```

3. **Получите значения cookies:**
   - Откройте YouTube в браузере
   - Нажмите F12 (Developer Tools)
   - Перейдите в Application/Storage → Cookies → https://youtube.com
   - Скопируйте значения нужных cookies

### Способ 3: Использование yt-dlp для извлечения cookies

1. **Установите yt-dlp с поддержкой cookies:**
   ```bash
   pip install yt-dlp[all]
   ```

2. **Извлеките cookies из браузера:**
   ```bash
   yt-dlp --cookies-from-browser chrome --cookies cookies.txt "https://youtube.com"
   ```

## Обновление конфигурации

После создания файла cookies обновите `config.json`:

```json
{
    "telegram_token": "YOUR_TOKEN",
    "allowed_channel_id": "YOUR_CHANNEL_ID",
    "grok_api_url": "https://api.x.ai/v1/chat/completions",
    "grok_api_key": "YOUR_GROK_KEY",
    "grok_model": "grok-4-latest",
    "yt_dlp_path": "venv/Scripts",
    "whisper_path": "venv/Scripts",
    "youtube_cookies": "cookies.txt"
}
```

## Примечания

- Cookies имеют срок действия, их нужно периодически обновлять
- Не делитесь файлом cookies с другими людьми
- Если cookies не работают, попробуйте обновить их или использовать другой браузер
- Бот автоматически попробует несколько методов скачивания, если первый не сработает

## Альтернативные методы

Если cookies не помогают, бот автоматически попробует:
1. Android клиент YouTube
2. Web клиент YouTube
3. Разные User-Agent строки
4. Обход SSL проверок



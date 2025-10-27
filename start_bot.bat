@echo off
chcp 65001 > nul
title AI Assistant Bot
echo ====================================
echo    AI Assistant Bot - Запуск
echo ====================================
echo.

REM Переходим в директорию со скриптом
cd /d "%~dp0"

REM Проверяем наличие виртуального окружения
if not exist "venv\Scripts\activate.bat" (
    echo [ОШИБКА] Виртуальное окружение не найдено!
    echo Пожалуйста, сначала установите виртуальное окружение:
    echo python -m venv venv
    echo venv\Scripts\activate
    echo pip install -r requirements.txt
    pause
    exit /b 1
)

REM Проверяем наличие config.json
if not exist "config.json" (
    echo [ОШИБКА] Файл config.json не найден!
    echo Пожалуйста, создайте файл конфигурации.
    pause
    exit /b 1
)

echo [INFO] Активирую виртуальное окружение...
call venv\Scripts\activate.bat

echo [INFO] Запускаю бота...
echo.
echo Для остановки бота нажмите Ctrl+C
echo ====================================
echo.

python ai_assistant_bot.py

REM Если бот завершился с ошибкой
if errorlevel 1 (
    echo.
    echo ====================================
    echo [ОШИБКА] Бот завершился с ошибкой!
    echo ====================================
    pause
)



@echo off
chcp 65001 > nul
title Статус бота

echo ====================================
echo    Проверка статуса бота
echo ====================================
echo.

REM Проверяем, запущен ли бот
set "BOT_RUNNING=0"

for /f "tokens=2" %%i in ('tasklist /FI "IMAGENAME eq python.exe" /FO LIST ^| find "PID:"') do (
    wmic process where "ProcessId=%%i" get CommandLine 2>nul | find /i "ai_assistant_bot.py" >nul
    if not errorlevel 1 (
        echo [СТАТУС] Бот ЗАПУЩЕН
        echo [PID] %%i
        set "BOT_RUNNING=1"
        goto :status_found
    )
)

:status_found
if "%BOT_RUNNING%"=="0" (
    echo [СТАТУС] Бот НЕ ЗАПУЩЕН
)

echo.
echo ====================================
echo.

REM Проверяем автозагрузку
set "STARTUP_FOLDER=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "SHORTCUT=%STARTUP_FOLDER%\AI Assistant Bot.lnk"

if exist "%SHORTCUT%" (
    echo [АВТОЗАГРУЗКА] ВКЛЮЧЕНА
) else (
    echo [АВТОЗАГРУЗКА] ОТКЛЮЧЕНА
)

echo.
echo ====================================
echo.
pause


